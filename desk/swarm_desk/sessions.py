"""Where Claude Code terminal sessions are recorded for the desk.

Each session gets a run folder laid out like a swarm run, `<root>/<project>/.swarm/runs/<YYYYmmdd-HHMMSS>/`, so the
desk reads it with the same code: `events.jsonl` (the activity), `task.md` (the first prompt), `summary.json` (written
when the session ends). `<project>` is the session's working folder name. Nothing here leaves this machine.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

DESK_DIR = Path(__file__).resolve().parents[1]
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def root() -> Path:
    override = os.environ.get("SWARM_DESK_SESSIONS")
    return Path(override) if override else DESK_DIR / ".data" / "sessions"


def project_name(cwd: str | None) -> str:
    """A folder name the run reader accepts (`NAME_RE`): the session's working folder, made safe."""
    name = _UNSAFE.sub("-", Path(cwd or "").name).strip("-.")[:60]
    return name if name and name[0].isalnum() else "session"


def _runs(project: str) -> Path:
    return root() / project / ".swarm" / "runs"


def run_for(session_id: str, cwd: str | None, create: bool = True) -> Path | None:
    """The run folder of one session, created on first use. Safe when several hooks of a session race to create it."""
    if not SESSION_ID_RE.fullmatch(session_id or ""):
        return None
    project = project_name(cwd)
    marks = root() / project / ".swarm" / "by-session"
    mark = marks / session_id
    for _ in range(50):
        try:
            run = mark.read_text(encoding="utf-8").strip()
        except OSError:
            run = ""
        if run:
            path = _runs(project) / run
            if path.is_dir():
                return path
            return _ensure(path) if create else None
        if not create:
            return None
        marks.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(mark, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            time.sleep(0.02)  # another hook of this session is naming the folder right now
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            run = _claim(project)
            fh.write(run)
        return _runs(project) / run
    return None


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _claim(project: str) -> str:
    """A free `YYYYmmdd-HHMMSS` run name (two sessions started in the same second get the next free second)."""
    base = _runs(project)
    base.mkdir(parents=True, exist_ok=True)
    t = time.time()
    for k in range(600):
        name = datetime.fromtimestamp(t + k).strftime("%Y%m%d-%H%M%S")
        with contextlib.suppress(FileExistsError):
            (base / name).mkdir()
            return name
    raise OSError("no free run name")


def latest_live(cwd: str | None) -> Path | None:
    """The newest unfinished session run for a working folder (what the MCP server writes into)."""
    base = _runs(project_name(cwd))
    if not base.is_dir():
        return None
    live = [p for p in base.iterdir() if p.is_dir() and not (p / "summary.json").exists()]
    return max(live, key=lambda p: p.stat().st_mtime, default=None)


def append(folder: Path, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    now = round(time.time(), 3)
    lines = "".join(json.dumps({"t": now, **ev}, ensure_ascii=False) + "\n" for ev in events)
    with (folder / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(lines)  # one write per hook call: lines from parallel hooks never interleave mid-line


def write_once(folder: Path, name: str, text: str) -> None:
    with contextlib.suppress(FileExistsError, OSError), (folder / name).open("x", encoding="utf-8") as fh:
        fh.write(text)
