"""Environment hygiene for gates and agents: pip is locked to virtualenvs; Python projects get a private venv."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import swarm.gates as gates_mod
from swarm.gates import (
    CmdResult,
    GateResult,
    bare_python_tools,
    clean_env,
    default_runner,
    ensure_python_env,
    safe_requirements,
)


def test_only_missing_tests() -> None:
    exit5 = CmdResult("pytest", 5)
    assert GateResult([exit5]).only_missing_tests
    assert GateResult([exit5, CmdResult("ruff check .", 0)]).only_missing_tests
    assert GateResult([exit5, CmdResult("x", None, skipped="n/a")]).only_missing_tests
    assert not GateResult([exit5, CmdResult("ruff check .", 1)]).only_missing_tests
    assert not GateResult([CmdResult("pytest", 0)]).only_missing_tests
    assert not GateResult([]).only_missing_tests


# --------------------------------------------------------------------------- clean_env


def test_clean_env_pip_lock_and_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIP_REQUIRE_VIRTUALENV", "0")
    env = clean_env({"CI": "1", "PYTHONIOENCODING": "latin-1"})
    assert env["PIP_REQUIRE_VIRTUALENV"] == "1"
    assert env["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
    assert env["CI"] == "1"
    assert env["PYTHONIOENCODING"] == "latin-1", "extra must win"
    assert os.environ["PIP_REQUIRE_VIRTUALENV"] == "0", "must not mutate the real environment"


def test_clean_env_hides_swarms_own_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    own = tmp_path / "swarm-venv"
    other = tmp_path / "other" / "bin"
    monkeypatch.setattr(gates_mod.sys, "prefix", str(own))
    monkeypatch.setattr(gates_mod.sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(own / "Scripts"), str(own / "bin"), str(other)]))
    monkeypatch.setenv("VIRTUAL_ENV", str(own))
    env = clean_env()
    assert env["PATH"] == str(other)
    assert env["VIRTUAL_ENV"] == ""


def test_clean_env_keeps_foreign_virtual_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gates_mod.sys, "prefix", str(tmp_path / "swarm-venv"))
    monkeypatch.setattr(gates_mod.sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "user-venv"))
    assert clean_env()["VIRTUAL_ENV"] == str(tmp_path / "user-venv")


def test_clean_env_outside_a_venv_keeps_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gates_mod.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(gates_mod.sys, "base_prefix", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "Scripts"))
    assert clean_env()["PATH"] == str(tmp_path / "Scripts")


def test_default_runner_locks_pip(tmp_path: Path) -> None:
    """Regression: a live debugger once pip-installed pytest into the user's global Python."""
    cmd = subprocess.list2cmdline([sys.executable, "-c", "import os; print(os.environ.get('PIP_REQUIRE_VIRTUALENV'))"])
    assert default_runner(cmd, tmp_path, 30).output.strip() == "1"


def test_agent_env_locks_pip(tmp_path: Path) -> None:
    pytest.importorskip("claude_agent_sdk")
    from swarm.backend import ClaudeBackend
    from test_backend import make_request

    env = ClaudeBackend().build_options(make_request(tmp_path)).env
    assert env["PIP_REQUIRE_VIRTUALENV"] == "1"


# --------------------------------------------------------------------------- bare tools / requirements


@pytest.mark.parametrize(
    ("commands", "expected"),
    [
        (["python -m pytest -q"], {"python", "pytest"}),
        (["pytest -q", "ruff check ."], {"pytest", "ruff"}),
        (["py -m mypy src"], {"py", "mypy"}),
        (["cd backend && pytest"], {"pytest"}),
        (['"C:/x/python.exe" -m pytest'], set()),
        ([".venv/Scripts/python -m pytest"], set()),
        (["npm test", "go test ./..."], set()),
        ([], set()),
    ],
)
def test_bare_python_tools(commands: list[str], expected: set[str]) -> None:
    assert bare_python_tools(commands) == expected


@pytest.mark.parametrize(
    ("text", "safe"),
    [
        ("requests\npydantic>=2,<3\n", True),
        ("# comment\n\nDjango==4.2  # lts\n", True),
        ("uvicorn[standard]~=0.30\n", True),
        ("numpy; python_version < '3.13'\n", True),
        ("", True),
        ("https://evil.example/pkg.whl\n", False),
        ("pkg @ git+https://github.com/a/b\n", False),
        ("-e .\n", False),
        ("--index-url https://evil.example/simple\nrequests\n", False),
        ("--extra-index-url https://x\n", False),
        ("-r other.txt\n", False),
        ("./local/path\n", False),
    ],
)
def test_safe_requirements(tmp_path: Path, text: str, safe: bool) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text(text, encoding="utf-8")
    assert safe_requirements(req) is safe


def test_safe_requirements_missing_file(tmp_path: Path) -> None:
    assert safe_requirements(tmp_path / "nope.txt") is False


# --------------------------------------------------------------------------- ensure_python_env (no real venv/pip)


class FakeQuiet:
    """Stands in for gates._run_quiet: no real venv or pip is ever run."""

    def __init__(
        self, project: Path, *, venv_ok: bool = True, installed: frozenset[str] = frozenset(), pip_ok: bool = True
    ) -> None:
        self.project, self.venv_ok, self.installed, self.pip_ok = project, venv_ok, set(installed), pip_ok
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
        self.calls.append(cmd)
        if cmd[1:3] == ["-m", "venv"]:
            if not self.venv_ok:
                return 1, "venv module broken"
            make_venv(self.project)
            return 0, ""
        if cmd[-1] == "--version":
            return (0, "ok") if cmd[2] in self.installed else (1, "No module named " + cmd[2])
        if cmd[1:4] == ["-m", "pip", "install"]:
            return (0, "") if self.pip_ok else (1, "network down")
        return 1, "unexpected"


def make_venv(project: Path) -> Path:
    py = project / ".venv" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", encoding="utf-8")
    return py


def installs(fake: FakeQuiet) -> list[list[str]]:
    return [c for c in fake.calls if c[1:4] == ["-m", "pip", "install"]]


@pytest.fixture
def py_project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def use(monkeypatch: pytest.MonkeyPatch, fake: FakeQuiet) -> FakeQuiet:
    monkeypatch.setattr(gates_mod, "_run_quiet", fake)
    return fake


def test_noop_for_non_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use(monkeypatch, FakeQuiet(tmp_path))
    (tmp_path / "index.js").write_text("", encoding="utf-8")
    assert ensure_python_env(tmp_path, ["npm test"]) == ""
    assert ensure_python_env(tmp_path, ["pytest"]) == "", "no python code in the project"
    assert fake.calls == []


def test_noop_for_explicit_interpreter(py_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use(monkeypatch, FakeQuiet(py_project))
    assert ensure_python_env(py_project, ['"C:/Python/python.exe" -m pytest -q']) == ""
    assert fake.calls == []


def test_creates_venv_and_installs(py_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use(monkeypatch, FakeQuiet(py_project))
    (py_project / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
    note = ensure_python_env(py_project, ["python -m pytest -q", "ruff check ."])
    assert fake.calls[0][0] == sys.executable and fake.calls[0][1:3] == ["-m", "venv"]
    (install,) = installs(fake)
    assert install[0] == str(py_project / ".venv" / "Scripts" / "python.exe"), "never the global interpreter"
    assert install[-4:] == ["pytest", "ruff", "-r", "requirements.txt"]
    assert "created .venv" in note and "installed pytest ruff -r requirements.txt" in note


def test_existing_venv_only_missing_tools(py_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_venv(py_project)
    (py_project / "requirements.txt").write_text("requests\n", encoding="utf-8")
    fake = use(monkeypatch, FakeQuiet(py_project, installed=frozenset({"pytest"})))
    note = ensure_python_env(py_project, ["pytest", "ruff check ."])
    assert not any(c[1:3] == ["-m", "venv"] for c in fake.calls)
    (install,) = installs(fake)
    assert install[-1] == "ruff" and "-r" not in install, "requirements are only installed into a fresh venv"
    assert note == "installed ruff into .venv"


def test_nothing_missing(py_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_venv(py_project)
    fake = use(monkeypatch, FakeQuiet(py_project, installed=frozenset({"pytest"})))
    assert ensure_python_env(py_project, ["pytest"]) == ""
    assert installs(fake) == []


def test_skips_unsafe_requirements(py_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (py_project / "requirements.txt").write_text("pkg @ git+https://github.com/a/b\n", encoding="utf-8")
    fake = use(monkeypatch, FakeQuiet(py_project))
    note = ensure_python_env(py_project, ["pytest"])
    (install,) = installs(fake)
    assert "-r" not in install
    assert "was not installed automatically" in note


def test_venv_failure(py_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use(monkeypatch, FakeQuiet(py_project, venv_ok=False))
    note = ensure_python_env(py_project, ["pytest"])
    assert note.startswith("could not create .venv") and "venv module broken" in note
    assert installs(fake) == []


def test_pip_failure(py_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, FakeQuiet(py_project, pip_ok=False))
    assert "pip install failed (network down)" in ensure_python_env(py_project, ["pytest"])
