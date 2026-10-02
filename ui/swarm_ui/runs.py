"""Read-only view of the swarm's on-disk run artifacts (`<project>/.swarm/runs/<id>/`)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
RUN_RE = re.compile(r"^\d{8}-\d{6}$")
ARTIFACT_RE = re.compile(
    r"^((task|plan|research|findings|report|progress)\.md|gate-\d{2}\.txt|plan\.json|summary\.json)$"
)
MAX_FILE = 256 * 1024
LIVE_WINDOW_S = 180
_COST = re.compile(r"\(\$([\d.]+)\)")
_LINE = re.compile(r"^- (\d\d:\d\d:\d\d) (.*)$")


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def run_dir(root: Path, project: str, run: str) -> Path | None:
    """Validated path to one run folder, or None. Names are whitelisted, then containment is re-checked."""
    if not (NAME_RE.fullmatch(project) and RUN_RE.fullmatch(run)):
        return None
    path = root / project / ".swarm" / "runs" / run
    return path if path.is_dir() and _inside(path, root) else None


def _read(path: Path, limit: int = MAX_FILE) -> str:
    try:
        with path.open("rb") as fh:
            data = fh.read(limit + 1)
    except OSError:
        return ""
    text = data[:limit].decode("utf-8", errors="replace")
    return text + "\n... [truncated]" if len(data) > limit else text


def progress(path: Path) -> list[dict[str, str]]:
    out = []
    for line in _read(path / "progress.md").splitlines():
        m = _LINE.match(line)
        if m:
            out.append({"time": m.group(1), "text": m.group(2)})
    return out


def _json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(_read(path))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def brief(root: Path, project: str, run: str) -> dict[str, Any] | None:
    path = run_dir(root, project, run)
    if path is None:
        return None
    summary = _json(path / "summary.json")
    events = progress(path)
    phase = next((e["text"][6:] for e in reversed(events) if e["text"].startswith("PHASE ")), "")
    prog = path / "progress.md"
    mtime = prog.stat().st_mtime if prog.exists() else path.stat().st_mtime
    if summary:
        status = str(summary.get("status", "unknown"))
    else:
        status = "running" if time.time() - mtime < LIVE_WINDOW_S else "interrupted"
    cost = summary.get("cost_usd")
    if cost is None:
        cost = round(sum(float(c) for e in events for c in _COST.findall(e["text"])), 4)
    task = str(summary.get("task") or _read(path / "task.md", 2000)).strip()
    return {
        "project": project, "run": run, "status": status, "phase": phase, "task": task[:400],
        "cost_usd": cost, "seconds": summary.get("seconds"), "updated": mtime,
        "open_findings": len(summary.get("open_findings") or []),
    }  # fmt: skip


def detail(root: Path, project: str, run: str) -> dict[str, Any] | None:
    info = brief(root, project, run)
    path = run_dir(root, project, run)
    if info is None or path is None:
        return None
    summary = _json(path / "summary.json")
    files = sorted(p.name for p in path.iterdir() if p.is_file() and ARTIFACT_RE.fullmatch(p.name))
    info.update(
        events=progress(path), files=files,
        gates=summary.get("gate") or [], calls=summary.get("calls") or [],
        findings=summary.get("open_findings") or [], notes=summary.get("notes") or [],
        changed_files=summary.get("changed_files") or [], commit=summary.get("commit") or "",
    )  # fmt: skip
    return info


def artifact(root: Path, project: str, run: str, name: str) -> str | None:
    path = run_dir(root, project, run)
    if path is None or not ARTIFACT_RE.fullmatch(name):
        return None
    target = path / name
    return _read(target) if target.is_file() and _inside(target, root) else None


_CALL = re.compile(r"^(?P<role>[\w-]+) \[(?P<label>.*)\] (?P<res>ok|FAILED.*?) \(\$(?P<cost>[\d.]+)\)$")
MAX_EVENTS = 400


def _synthesize(path: Path) -> list[dict[str, Any]]:
    """Older runs have no events.jsonl: rebuild a coarse timeline from progress.md."""
    out: list[dict[str, Any]] = []
    phase = ""
    for e in progress(path):
        text = e["text"]
        if text.startswith("PHASE "):
            phase = text[6:].split(" ")[0]
            out.append({"kind": "phase", "name": phase, "detail": ""})
        elif text.startswith("GATE "):
            out.append({"kind": "gate", "label": text[5:].split(":")[0], "state": text.rsplit(": ", 1)[-1]})
        elif m := _CALL.match(text):
            out.append({"kind": "agent_start", "role": m["role"], "label": m["label"], "phase": phase})
            out.append({"kind": "agent_end", "role": m["role"], "ok": m["res"] == "ok", "cost": float(m["cost"])})
    return out


def load_events(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """All activity records of one run folder, and whether they had to be rebuilt from progress.md."""
    raw = path / "events.jsonl"
    if not raw.is_file():
        return _synthesize(path), True
    items = []
    for line in _read(raw, 2 * 1024 * 1024).splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            items.append(rec)
    return items, False


def page(items: list[dict[str, Any]], synthetic: bool, since: int = 0) -> dict[str, Any]:
    """Index-based paging so the UI only fetches what is new."""
    since = max(0, since)
    chunk = items[since : since + MAX_EVENTS]
    return {"events": chunk, "next": since + len(chunk), "synthetic": synthetic}


def events(root: Path, project: str, run: str, since: int = 0) -> dict[str, Any] | None:
    """Live activity records from `events.jsonl` for a run under the workspace."""
    path = run_dir(root, project, run)
    if path is None:
        return None
    return page(*load_events(path), since)


def list_projects(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and NAME_RE.fullmatch(p.name) and _inside(p, root))


def overview(root: Path, limit: int = 60) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    projects = list_projects(root)
    for project in projects:
        base = root / project / ".swarm" / "runs"
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if RUN_RE.fullmatch(d.name):
                info = brief(root, project, d.name)
                if info:
                    runs.append(info)
    runs.sort(key=lambda r: r["updated"], reverse=True)
    total = round(sum(float(r["cost_usd"] or 0) for r in runs), 2)
    return {"projects": projects, "runs": runs[:limit], "total_cost_usd": total, "run_count": len(runs)}
