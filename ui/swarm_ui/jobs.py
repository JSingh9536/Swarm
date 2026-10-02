"""Launching and supervising swarm commands.

Only a fixed set of subcommands can be started, every argument is validated, nothing goes through a shell,
and at most one job runs at a time. The UI never forwards free-form command lines.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swarm_ui.runs import NAME_RE, list_projects

KINDS = ("run", "review", "audit", "research")
MODELS = ("haiku", "sonnet", "opus")
MAX_TEXT = 4000
MAX_BUDGET = 10.0
MAX_ROUNDS = 5
LOG_CHUNK = 64 * 1024
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_JOB_ID = re.compile(r"^[0-9a-f]{12}$")


class JobError(ValueError):
    """The request was rejected; the message is safe to show to the user."""


def swarm_python(root: Path) -> str:
    venv = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv) if venv.exists() else sys.executable


def build_argv(root: Path, spec: dict[str, Any], default_budget: float = 3.0) -> list[str]:
    """Validate a UI request and turn it into an argv list for `python -m swarm`."""
    kind = spec.get("kind")
    if kind not in KINDS:
        raise JobError(f"kind must be one of {', '.join(KINDS)}")
    text = spec.get("text", "")
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise JobError(f"text must be a string of at most {MAX_TEXT} characters")
    text = text.strip()
    if kind in ("run", "research") and not text:
        raise JobError("describe the task first")

    argv = [swarm_python(root), "-m", "swarm", kind, "--yes", "--nice"]

    budget = spec.get("budget", default_budget)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 0 < budget <= MAX_BUDGET:
        raise JobError(f"budget must be between 0 and {MAX_BUDGET:g} USD")
    argv += ["--budget", f"{float(budget):g}"]
    model = spec.get("model")
    if model:
        if model not in MODELS:
            raise JobError(f"model must be one of {', '.join(MODELS)}")
        argv += ["--model", model]
    rounds = spec.get("rounds")
    if rounds is not None and kind in ("run", "review", "audit"):
        if isinstance(rounds, bool) or not isinstance(rounds, int) or not 0 <= rounds <= MAX_ROUNDS:
            raise JobError(f"rounds must be 0-{MAX_ROUNDS}")
        argv += ["--rounds", str(rounds)]

    project = spec.get("project")
    if project:
        known = list_projects(root / "workspace")
        if not isinstance(project, str) or not NAME_RE.fullmatch(project) or project not in known:
            raise JobError("unknown project")
        argv += ["--project", str(root / "workspace" / project)]
    elif kind in ("review", "audit"):
        raise JobError("choose a project")

    if kind == "run":
        if spec.get("no_research"):
            argv.append("--no-research")
        if spec.get("commit"):
            argv.append("--commit")
    if kind in ("review", "audit") and spec.get("fix"):
        argv.append("--fix")
    if text:
        argv += ["--", text]  # after "--": text can never be read as an option
    return argv


@dataclass
class Job:
    id: str
    kind: str
    title: str
    argv: list[str]
    started: float
    log: Path
    proc: Any = None
    status: str = "running"  # running | done | failed | stopped
    exit_code: int | None = None
    ended: float | None = None
    stopping: bool = field(default=False, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
            "exit_code": self.exit_code, "started": self.started, "ended": self.ended,
        }  # fmt: skip


def _kill_tree(proc: Any) -> None:
    if os.name == "nt":
        subprocess.run(  # noqa: S603, S607
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False, timeout=15
        )
    else:
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)


def _popen(argv: list[str], cwd: Path, log_fh: Any) -> subprocess.Popen:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"}
    env["PYTHONPATH"] = str(cwd / "src") + os.pathsep + env.get("PYTHONPATH", "")
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(  # noqa: S603
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT, **kwargs
    )


class JobManager:
    def __init__(
        self, root: Path, data_dir: Path, spawn: Callable[[list[str], Path, Any], Any] = _popen
    ) -> None:
        self.root, self.data_dir, self._spawn = root, data_dir, spawn
        self.logs = data_dir / "jobs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []

    def _refresh(self, job: Job) -> None:
        if job.status == "running" and job.proc is not None and (code := job.proc.poll()) is not None:
            job.exit_code, job.ended = code, time.time()
            job.status = "stopped" if job.stopping else ("done" if code == 0 else "failed")

    def current(self) -> Job | None:
        for jid in reversed(self._order):
            job = self._jobs[jid]
            self._refresh(job)
            if job.status == "running":
                return job
        return None

    def start(self, spec: dict[str, Any], default_budget: float = 3.0) -> Job:
        argv = build_argv(self.root, spec, default_budget)
        with self._lock:
            if self.current() is not None:
                raise JobError("a job is already running; stop it first")
            jid = uuid.uuid4().hex[:12]
            log = self.logs / f"{jid}.log"
            title = f"{spec['kind']}: {(spec.get('text') or spec.get('project') or '').strip()[:80]}"
            with log.open("wb") as fh:
                proc = self._spawn(argv, self.root, fh)
            job = Job(jid, spec["kind"], title, argv, time.time(), log, proc)
            self._jobs[jid] = job
            self._order.append(jid)
            self._order, dropped = self._order[-30:], self._order[:-30]
            for old in dropped:
                self._jobs.pop(old, None)
            return job

    def get(self, jid: str) -> Job | None:
        job = self._jobs.get(jid) if _JOB_ID.fullmatch(jid) else None
        if job:
            self._refresh(job)
        return job

    def stop(self, jid: str) -> Job | None:
        job = self.get(jid)
        if job and job.status == "running":
            job.stopping = True
            _kill_tree(job.proc)
            with contextlib.suppress(Exception):  # best effort; the status refresh handles the rest
                job.proc.wait(timeout=10)
            self._refresh(job)
        return job

    def history(self) -> list[dict[str, Any]]:
        out = []
        for jid in reversed(self._order):
            job = self._jobs[jid]
            self._refresh(job)
            out.append(job.public())
        return out

    def tail(self, job: Job, offset: int) -> dict[str, Any]:
        """Return new log text after `offset` bytes (ANSI stripped, capped per call)."""
        try:
            size = job.log.stat().st_size
            offset = max(0, min(offset, size))
            with job.log.open("rb") as fh:
                fh.seek(offset)
                data = fh.read(LOG_CHUNK)
        except OSError:
            return {"text": "", "offset": offset}
        text = _ANSI.sub("", data.decode("utf-8", errors="replace"))
        return {"text": text, "offset": offset + len(data)}

    def shutdown(self) -> None:
        for jid in list(self._order):
            self.stop(jid)

