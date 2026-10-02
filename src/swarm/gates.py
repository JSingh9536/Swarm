"""Deterministic quality gates: run the project's real tests and linters.

Agents' claims are never trusted on their own - the pipeline runs these commands itself. Commands
come from the architect's plan (LLM output), so only recognised test/lint runners are executed.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".swarm", "dist", "build", ".tox"}
_PY_TOOLS = {"pytest", "ruff", "mypy", "flake8", "pylint", "unittest", "compileall"}
_PY_LAUNCHERS = {"python", "python3", "py"}
_BARE_TOOLS = {"pytest", "ruff", "mypy", "flake8", "pylint", "tsc", "eslint", "vitest", "jest", "prettier", "mocha"}
_NODE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
_SCRIPT_RE = re.compile(r"^(?:test|tests|t|lint|typecheck|check|build|type-check|test:[\w:-]+|lint:[\w:-]+)$")
_SUBCOMMANDS = {
    "go": {"test", "vet", "build"},
    "cargo": {"test", "check", "clippy", "build", "fmt"},
    "dotnet": {"test", "build"},
    "mvn": {"test", "verify", "-q"},
    "mvnw": {"test", "verify", "-q"},
    "gradle": {"test", "check", "build"},
    "gradlew": {"test", "check", "build"},
    "make": {"test", "check", "lint"},
    "deno": {"test", "lint", "check"},
}
_NOT_INSTALLED = re.compile(r"No module named (?:ruff|mypy|flake8|pylint)\b|command not found|is not recognized", re.I)


@dataclass
class CmdResult:
    command: str
    exit_code: int | None
    output: str = ""
    seconds: float = 0.0
    skipped: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.skipped) or self.exit_code == 0


@dataclass
class GateResult:
    results: list[CmdResult] = field(default_factory=list)

    @property
    def ran(self) -> list[CmdResult]:
        return [r for r in self.results if not r.skipped]

    @property
    def nothing_to_run(self) -> bool:
        return not self.ran

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def only_missing_tests(self) -> bool:
        """True when the only failures are pytest's "no tests collected" (exit 5) - normal mid-plan."""
        failing = [r for r in self.ran if not r.ok]
        return bool(failing) and all(r.exit_code == 5 for r in failing)

    def summary(self, limit: int = 4000) -> str:
        """Human/LLM-readable digest; failures first, output trimmed to `limit` characters overall."""
        if not self.results:
            return "(no gate commands)"
        parts = []
        for r in sorted(self.results, key=lambda r: r.ok):
            if r.skipped:
                parts.append(f"$ {r.command}\n[skipped: {r.skipped}]")
            else:
                status = "TIMED OUT" if r.timed_out else f"exit {r.exit_code}"
                parts.append(f"$ {r.command}\n[{status}, {r.seconds:.1f}s]\n{r.output.strip()}")
        text = "\n\n".join(parts)
        return text if len(text) <= limit else "...(truncated)...\n" + text[-limit:]


Runner = Callable[[str, Path, float], CmdResult]


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the shell AND everything it started (a plain kill leaves grandchildren holding the pipes)."""
    try:
        if os.name == "nt":
            subprocess.run(  # noqa: S603, S607
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=15,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    with contextlib.suppress(OSError):
        proc.kill()


def _norm_dir(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path))) if str(path) else ""


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for agent and gate subprocesses.

    Hides swarm's own virtualenv (so agents never install into it) and makes pip refuse to install anywhere
    but inside a virtualenv, so no agent can change the user's global Python.
    """
    env = dict(os.environ)
    if sys.prefix != sys.base_prefix:
        own = {_norm_dir(Path(sys.prefix) / sub) for sub in ("Scripts", "bin")}
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if _norm_dir(p) not in own]
        env["PATH"] = os.pathsep.join(parts)
        if env.get("VIRTUAL_ENV") and _norm_dir(env["VIRTUAL_ENV"]) == _norm_dir(sys.prefix):
            env["VIRTUAL_ENV"] = ""
    env.update({"PIP_REQUIRE_VIRTUALENV": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONIOENCODING": "utf-8"})
    if extra:
        env.update(extra)
    return env


def default_runner(command: str, cwd: Path, timeout: float) -> CmdResult:
    started = time.monotonic()
    env = clean_env({"CI": "1", "NO_COLOR": "1"})
    extra: dict[str, object] = {"start_new_session": True} if os.name != "nt" else {}
    proc = subprocess.Popen(  # noqa: S602 - the command was allowlisted by is_gate_command
        command, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", env=env, **extra,
    )  # fmt: skip
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            output = ""
        return CmdResult(command, None, (output or "")[-6000:], time.monotonic() - started, timed_out=True)
    return CmdResult(command, proc.returncode, (output or "")[-6000:], time.monotonic() - started)


# ---------------------------------------------------------------------------------- allowlist


def _program(token: str) -> str:
    return os.path.basename(token.strip("\"'")).lower().removesuffix(".exe").removesuffix(".cmd")


def _simple_command_ok(tokens: list[str]) -> bool:
    if not tokens:
        return False
    prog, args = _program(tokens[0]), [a.strip("\"'") for a in tokens[1:]]
    if prog in _PY_LAUNCHERS:
        return len(args) >= 2 and args[0] == "-m" and args[1] in _PY_TOOLS
    if prog in _BARE_TOOLS:
        return True
    if prog in _NODE_MANAGERS:
        first = next((a for a in args if not a.startswith("-")), "")
        if first in ("test", "t"):
            return True
        if first == "run":
            script = next((a for a in args[args.index("run") + 1 :] if not a.startswith("-")), "")
            return bool(_SCRIPT_RE.match(script))
        return False
    if prog == "npx":
        first = next((a for a in args if not a.startswith("-")), "")
        return first in {"vitest", "jest", "eslint", "tsc", "prettier", "mocha"}
    if prog == "node":
        return bool(args) and args[0] == "--test"
    if prog in _SUBCOMMANDS:
        first = next((a for a in args if not a.startswith("-")), "")
        return first in _SUBCOMMANDS[prog]
    return False


def is_gate_command(command: str) -> bool:
    """True only for recognised test/lint runners, optionally chained with `&&` and a relative `cd`."""
    # No shell metacharacters beyond `&&`: `;` `|` redirects, substitution, cmd.exe's `^` escape and `%VAR%` expansion,
    # newlines, and any single `&` (which cmd.exe treats as a command separator).
    if not command.strip() or re.search(r"[;|<>`$^%\n\r]", command) or re.search(r"(?<!&)&(?!&)|&&&", command):
        return False
    for part in command.split("&&"):
        tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', part.strip())
        if not tokens:
            return False
        if _program(tokens[0]) == "cd":
            target = tokens[1].strip("\"'") if len(tokens) == 2 else ""
            if (
                not target
                or os.path.isabs(target)
                or target.startswith(("/", "\\", "~"))
                or ":" in target
                or ".." in re.split(r"[\\/]+", target)
            ):
                return False
            continue
        if not _simple_command_ok(tokens):
            return False
    return True


# ---------------------------------------------------------------------------------- detection


def _has_pytest(python: Path) -> bool:
    root = python.parent.parent
    return (root / "Lib/site-packages/pytest").is_dir() or any(root.glob("lib/python*/site-packages/pytest"))


def venv_python(project: Path) -> Path | None:
    """The project's own interpreter. When it has more than one virtualenv, the first with pytest installed
    wins: an empty leftover environment must not shadow the one the project really uses."""
    rels = (".venv/Scripts/python.exe", ".venv/bin/python", "venv/Scripts/python.exe", "venv/bin/python")
    found = [project / rel for rel in rels if (project / rel).is_file()]
    if len(found) > 1:
        for candidate in found:
            if _has_pytest(candidate):
                return candidate
    return found[0] if found else None


def resolve_command(command: str, project: Path) -> str:
    """Prefer the project's own virtualenv for python tooling."""
    py = venv_python(project)
    if py is None:
        return command
    quoted = subprocess.list2cmdline([str(py)])
    first, _, rest = command.partition(" ")
    prog = _program(first)
    if prog in {"python", "python3"}:
        return f"{quoted} {rest}".rstrip()
    if prog in {"pytest", "ruff", "mypy", "flake8", "pylint"}:
        return f"{quoted} -m {prog} {rest}".rstrip()
    return command


def _walk_files(project: Path, max_depth: int = 4, max_files: int = 4000):
    seen = 0
    root_depth = len(project.parts)
    for dirpath, dirnames, filenames in os.walk(project):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.endswith(".egg-info")]
        if len(Path(dirpath).parts) - root_depth >= max_depth:
            dirnames[:] = []
        for name in filenames:
            seen += 1
            if seen > max_files:
                return
            yield Path(dirpath) / name


@dataclass
class Detected:
    tests: list[str] = field(default_factory=list)
    lint: list[str] = field(default_factory=list)


def detect(project: Path) -> Detected:
    """Guess how this project is tested and linted from its files."""
    found = Detected()
    files = list(_walk_files(project))
    names = {p.name for p in files if p.parent == project}

    has_py_tests = any(
        p.suffix == ".py" and (p.name.startswith("test_") or p.name.endswith("_test.py") or p.parent.name == "tests")
        for p in files
    )
    if has_py_tests or {"pytest.ini", "tox.ini"} & names:
        found.tests.append("python -m pytest -q")
    pyproject = project / "pyproject.toml"
    if (
        "ruff.toml" in names
        or ".ruff.toml" in names
        or (pyproject.exists() and "[tool.ruff" in pyproject.read_text(encoding="utf-8", errors="replace"))
    ):
        found.lint.append("python -m ruff check .")

    pkg = project / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {}) or {}
        except (ValueError, OSError):
            scripts = {}
        test = str(scripts.get("test", ""))
        if test and "no test specified" not in test:
            found.tests.append("npm test --silent")
        if "lint" in scripts:
            found.lint.append("npm run lint --silent")
    if "go.mod" in names:
        found.tests.append("go test ./...")
    if "Cargo.toml" in names:
        found.tests.append("cargo test --quiet")
    if any(p.suffix in (".sln", ".csproj") for p in files):
        found.tests.append("dotnet test --nologo -v q")
    return found


# ---------------------------------------------------------------------------------- environment

_BARE_PY = frozenset({"python", "python3", "py", "pytest", "ruff", "mypy", "flake8", "pylint"})
_PY_PACKAGES = frozenset({"pytest", "ruff", "mypy", "flake8", "pylint"})
_SAFE_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?"  # name and extras
    r"(?:\s*(?:==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_-]+\s*,?)*"  # version specifiers
    r"(?:\s*;[^#]*)?(?:\s+#.*)?$"  # environment marker, trailing comment
)


def bare_python_tools(commands: list[str]) -> set[str]:
    """Python tools invoked by bare name, i.e. resolved from PATH rather than from an explicit interpreter path."""
    found: set[str] = set()
    for cmd in commands:
        for part in cmd.split("&&"):
            tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', part.strip())
            if not tokens or _program(tokens[0]) == "cd":
                continue
            first = tokens[0].strip("\"'")
            if os.path.dirname(first) or _program(first) not in _BARE_PY:
                continue
            found.add(_program(first))
            if _program(first) in _PY_LAUNCHERS and len(tokens) > 2 and tokens[1] == "-m":
                found.add(tokens[2].strip("\"'"))
    return found


def _has_python_code(project: Path) -> bool:
    if any((project / name).exists() for name in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg")):
        return True
    return any(p.suffix == ".py" for p in _walk_files(project, max_depth=3, max_files=500))


def _run_quiet(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argument lists built in this module
            cmd, cwd=cwd, env=clean_env(), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def safe_requirements(path: Path) -> bool:
    """True if every line of a requirements file is a plain `name[extras]<specifier>` (no URLs, VCS, or options)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for raw in lines:
        line = raw.strip()
        if line and not line.startswith("#") and not _SAFE_REQUIREMENT.match(line):
            return False
    return True


def ensure_python_env(project: Path, commands: list[str]) -> str:
    """Give a Python project a private virtualenv holding the tools the gate needs.

    Agents cannot install packages globally (pip is set to refuse), so the pipeline prepares the environment
    itself with a fixed, safe recipe. Returns a short note of what it did ('' = nothing needed).
    """
    tools = bare_python_tools(commands)
    if not tools or not _has_python_code(project):
        return ""
    notes: list[str] = []
    py = venv_python(project)
    fresh = py is None
    if py is None:
        code, out = _run_quiet([sys.executable, "-m", "venv", str(project / ".venv")], project, 180)
        if code != 0:
            return f"could not create .venv ({out[-200:]})"
        py = venv_python(project)
        if py is None:
            return "created .venv but found no interpreter inside it"
        notes.append("created .venv")
    wanted = sorted(tools & _PY_PACKAGES)
    missing = [pkg for pkg in wanted if _run_quiet([str(py), "-m", pkg, "--version"], project, 90)[0] != 0]
    install = list(missing)
    requirements = project / "requirements.txt"
    if fresh and requirements.is_file():
        if safe_requirements(requirements):
            install += ["-r", "requirements.txt"]
        else:
            notes.append("requirements.txt has URLs, VCS or options, so it was not installed automatically")
    if install:
        code, out = _run_quiet(
            [str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", *install], project, 400
        )
        notes.append(f"installed {' '.join(install)} into .venv" if code == 0 else f"pip install failed ({out[-200:]})")
    return "; ".join(notes)


# ---------------------------------------------------------------------------------- execution


def run_gate(commands: list[str], project: Path, timeout: float = 600.0, runner: Runner = default_runner) -> GateResult:
    gate = GateResult()
    seen: set[str] = set()
    for original in commands:
        if original in seen:
            continue
        seen.add(original)
        if not is_gate_command(original):
            gate.results.append(CmdResult(original, None, skipped="not a recognised test/lint runner"))
            continue
        result = runner(resolve_command(original, project), project, timeout)
        result.command = original
        if not result.ok and _NOT_INSTALLED.search(result.output) and "pytest" not in original:
            result = CmdResult(original, None, result.output, result.seconds, skipped="tool is not installed")
        elif result.exit_code == 5 and "pytest" in original:
            result.output = "pytest collected no tests - the project needs automated tests.\n" + result.output
            result.exit_code = 5
        gate.results.append(result)
    return gate
