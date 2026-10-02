"""Read-only monitor data: the work queue, the autopilot's state and log, the live run, the hourly status
snapshot and whether Ollama is up.

Everything here only reads. The queue lives in `queue.json` next to the swarm checkout; an item's runs live in that
item's own project folder (`<project>/.swarm/runs/<id>/`), which is usually outside `workspace/`. The browser never
sends a path: it names a queue item by its integer id and the server resolves the folder from `queue.json`.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Any

from swarm_ui import runs

STATES = ("todo", "doing", "done", "blocked")
STALL_S = 15 * 60  # a "doing" item whose run has written nothing for this long is shown as quiet, not working
MB = 1024 * 1024
MAX_JSON = 2 * MB
LOG_LINES = 40
OLLAMA_PS = "http://127.0.0.1:11434/api/ps"  # loopback only; the one address this panel ever calls
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_AUTOPILOT_LOG = re.compile(r"^autopilot-\d{4}-\d\d-\d\d\.log$")
_HEADING = re.compile(r"^## \d{4}-\d\d-\d\d autopilot\s*$")
_MODEL = re.compile(r"\(([\w.:/-]+)\) \(\$[\d.]+\)$")


def _load(path: Path) -> Any:
    try:
        return json.loads(runs._read(path, MAX_JSON))
    except ValueError:
        return None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ---------------------------------------------------------------- queue
def _items(root: Path) -> list[dict[str, Any]]:
    """The raw queue items that are well-formed enough to show. A broken file is an empty queue, not an error."""
    raw = _load(root / "queue.json")
    out = []
    for it in raw if isinstance(raw, list) else []:
        if isinstance(it, dict) and type(it.get("id")) is int and it.get("state") in STATES:
            out.append(it)
    return out


def _public(it: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": it["id"], "state": it["state"], "tier": it.get("tier"), "urgent": bool(it.get("urgent")),
        "project": PureWindowsPath(str(it.get("project") or "")).name,
        "task": str(it.get("task") or "")[:600], "check": str(it.get("check") or "")[:300],
        "note": str(it.get("note") or "")[:400],
        "attempts_local": int(_num(it.get("attempts_local")) or 0),
        "attempts_claude": int(_num(it.get("attempts_claude")) or 0),
    }  # fmt: skip


def _project_dir(it: dict[str, Any]) -> Path | None:
    project = it.get("project")
    if not isinstance(project, str) or not project:
        return None
    path = Path(project)
    return path if path.is_absolute() and path.is_dir() else None


def _run_dirs(project: Path) -> list[Path]:
    base = project / ".swarm" / "runs"
    if not base.is_dir():
        return []
    return sorted(
        (d for d in base.iterdir() if runs.RUN_RE.fullmatch(d.name) and d.is_dir() and runs._inside(d, project)),
        key=lambda d: d.name,
    )


# ---------------------------------------------------------------- one run, as the monitor shows it
def run_view(path: Path, now: float) -> dict[str, Any]:
    """Who is working in this run, the phase, the gate results so far and when it last wrote anything."""
    items, _ = runs.load_events(path)
    phase, activity, last_t = "", "", 0.0
    active: dict[str, dict[str, str]] = {}
    gates: list[dict[str, Any]] = []
    for e in items:
        kind, role = e.get("kind"), str(e.get("role") or "")
        if kind == "phase":
            phase = f"{e.get('name', '')} {e.get('detail', '')}".strip()
        elif kind == "agent_start":
            active[role] = {"role": role, "label": str(e.get("label") or "")[:120], "model": str(e.get("model") or "")}
        elif kind == "agent_end":
            active.pop(role, None)
        elif kind == "gate":
            state = str(e.get("state") or "")
            gates.append({"label": str(e.get("label") or "")[:80], "state": state[:20], "ok": "pass" in state.lower()})
        elif kind == "activity":
            activity = f"{role} · {e.get('text') or e.get('what') or ''}"[:200]
        last_t = _num(e.get("t")) or last_t
    progress = runs.progress(path)
    prog = path / "progress.md"
    with contextlib.suppress(OSError):
        last_t = max(last_t, prog.stat().st_mtime if prog.exists() else path.stat().st_mtime)
    summary = runs._json(path / "summary.json")
    model = next((m.group(1) for e in reversed(progress) if (m := _MODEL.search(e["text"]))), "")
    if summary:
        status = str(summary.get("status", "unknown"))
    else:
        status = "running" if now - last_t < runs.LIVE_WINDOW_S else "interrupted"
    return {
        "id": path.name, "status": status, "phase": phase, "active": list(active.values()) if not summary else [],
        "gates": gates[-12:], "activity": activity, "last_t": last_t, "model": model,
    }  # fmt: skip


def item_events(root: Path, item_id: int, run: str | None, since: int = 0) -> dict[str, Any] | None:
    """Event stream for a queue item's run (the latest one unless `run` names another), paged like `runs.events`."""
    item = next((it for it in _items(root) if it["id"] == item_id), None)
    project = _project_dir(item) if item else None
    if project is None:
        return None
    dirs = _run_dirs(project)
    if run is not None:
        dirs = [d for d in dirs if d.name == run] if runs.RUN_RE.fullmatch(run) else []
    if not dirs:
        return None
    return {**runs.page(*runs.load_events(dirs[-1]), since), "run": dirs[-1].name}


# ---------------------------------------------------------------- autopilot
def autopilot(root: Path, now: float) -> dict[str, Any]:
    data = _load(root / "autopilot-state.json")
    data = data if isinstance(data, dict) else {}
    until = _num(data.get("claude_paused_until"))
    paused = until is not None and now < until
    cur = data.get("current")
    current = None
    if isinstance(cur, dict) and type(cur.get("item")) is int:
        current = {
            "item": cur["item"], "backend": str(cur.get("backend") or "")[:20],
            "why": str(cur.get("why") or "")[:200], "started_at": _num(cur.get("started_at")),
        }  # fmt: skip
    return {
        "claude_paused": paused, "paused_until": until if paused else None,
        "reason": str(data.get("reason") or "")[:300] if paused else "",
        "last_item": data.get("last_item") if type(data.get("last_item")) is int else None,
        "updated_at": _num(data.get("updated_at")), "current": current,
    }  # fmt: skip


def log(root: Path) -> dict[str, Any]:
    """The newest autopilot output: the redirected console log under `workspace/`, or the autopilot section of
    the build log, whichever was written last."""
    found: list[tuple[float, str, list[str]]] = []
    ws = root / "workspace"
    names = sorted(p.name for p in ws.iterdir() if _AUTOPILOT_LOG.fullmatch(p.name)) if ws.is_dir() else []
    if names:
        path = ws / names[-1]
        found.append((path.stat().st_mtime, names[-1], runs._read(path).splitlines()))
    build = root / "docs" / "autonomous-build-log.md"
    if build.is_file():
        lines = runs._read(build, MAX_JSON).splitlines()
        heads = [i for i, line in enumerate(lines) if _HEADING.match(line)]
        if heads:
            section = []
            for line in lines[heads[-1] + 1 :]:
                if line.startswith("## "):
                    break
                section.append(line)
            found.append((build.stat().st_mtime, f"{build.name}: {lines[heads[-1]][3:].strip()}", section))
    if not found:
        return {"source": "", "updated": None, "lines": []}
    mtime, source, lines = max(found, key=lambda f: f[0])
    clean = [_ANSI.sub("", line).rstrip()[:300] for line in lines if line.strip()]
    return {"source": source, "updated": mtime, "lines": clean[-LOG_LINES:]}


# ---------------------------------------------------------------- hourly status snapshot
def _pick(data: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: data.get(k) for k in keys} if isinstance(data, dict) else {}


def _rows(data: Any, keys: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    return [_pick(row, keys) for row in data if isinstance(row, dict)][:limit] if isinstance(data, list) else []


def status(root: Path) -> dict[str, Any] | None:
    """A trimmed copy of `docs/status.json` (written hourly with no model). None when it does not exist yet."""
    snap = _load(root / "docs" / "status.json")
    if not isinstance(snap, dict):
        return None
    load, machine = snap.get("load"), snap.get("machine")
    machine = machine if isinstance(machine, dict) else {}
    claude = snap.get("claude") if isinstance(snap.get("claude"), dict) else {}
    reasons = load.get("reasons") if isinstance(load, dict) else None
    return {
        "generated_at": _num(snap.get("generated_at")),
        "load": {
            "mode": str(load.get("mode") or "") if isinstance(load, dict) else "",
            "reasons": [str(r)[:200] for r in reasons][:6] if isinstance(reasons, list) else [],
        },
        "cpu_percent": _num(_pick(machine.get("cpu"), ("percent",)).get("percent")),
        "mem_used_mb": _num(machine.get("mem_used_mb")), "mem_total_mb": _num(machine.get("mem_total_mb")),
        "gpus": _rows(machine.get("gpus"), ("name", "util_pct", "mem_used_mb", "mem_total_mb", "temp_c", "power_w"), 4),
        "volumes": _rows(machine.get("volumes"), ("mount", "total_gb", "free_gb", "used_pct"), 8),
        "disks": _rows(machine.get("disks"), ("name", "health"), 8),
        "local": _pick(
            snap.get("local"), ("calls", "ok", "tokens_in", "tokens_out", "seconds", "kwh", "electricity_usd")
        ),
        "claude": {
            "since_days": claude.get("since_days"),
            "total": _pick(claude.get("total"), ("input", "output", "subagent_output", "messages")),
            "by_day": _rows((claude.get("by_day") or [])[-7:], ("day", "input", "output", "messages"), 7),
        },
    }  # fmt: skip


# ---------------------------------------------------------------- Ollama
def _fetch_ps() -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy
    with opener.open(OLLAMA_PS, timeout=0.7) as res:  # noqa: S310 - fixed loopback URL
        return res.read(256 * 1024)


class OllamaProbe:
    """Is the local model server up, and which models hold memory. Cached so page polls stay cheap."""

    def __init__(self, fetch: Callable[[], bytes] = _fetch_ps, ttl_up: float = 5.0, ttl_down: float = 15.0) -> None:
        self._fetch, self._ttl_up, self._ttl_down = fetch, ttl_up, ttl_down
        self._lock = threading.Lock()
        self._at, self._value = 0.0, {"up": False, "models": []}

    def __call__(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            ttl = self._ttl_up if self._value["up"] else self._ttl_down
            if self._at and now - self._at < ttl:
                return self._value
            try:
                data = json.loads(self._fetch())
                models = data.get("models") if isinstance(data, dict) else None
                loaded = [m for m in (models if isinstance(models, list) else []) if isinstance(m, dict)][:8]
                value = {
                    "up": True,
                    "models": [
                        {"name": str(m.get("name") or "")[:80], "vram_mb": round((_num(m.get("size_vram")) or 0) / MB)}
                        for m in loaded
                    ],
                }
            except (OSError, ValueError):
                value = {"up": False, "models": []}
            self._at, self._value = now, value
            return value


# ---------------------------------------------------------------- the whole page in one call
def _recent(items: list[dict[str, Any]], workspace: Path, limit: int = 8) -> list[dict[str, Any]]:
    """Latest runs in the queue items' own project folders (runs under `workspace/` are listed by `runs.overview`)."""
    seen: dict[Path, int] = {}
    for it in items:
        project = _project_dir(it)
        if project and project not in seen and not runs._inside(project, workspace):
            seen[project] = it["id"]
    out = []
    for project, item_id in seen.items():
        for d in _run_dirs(project)[-3:]:
            info = runs.brief(project.parent, project.name, d.name)
            if info:
                out.append({**info, "item": item_id})
    out.sort(key=lambda r: r["updated"], reverse=True)
    return out[:limit]


def snapshot(
    root: Path, workspace: Path, ollama: Callable[[], dict[str, Any]], now: float | None = None
) -> dict[str, Any]:
    """Everything the monitor shows. `state` is idle, working, quiet (a run that stopped writing) or waiting."""
    now = time.time() if now is None else now
    items = _items(root)
    auto = autopilot(root, now)
    counts = {s: sum(1 for it in items if it["state"] == s) for s in STATES}

    live = None
    doing = next((it for it in items if it["state"] == "doing"), None)
    if doing:
        cur = auto["current"] if auto["current"] and auto["current"]["item"] == doing["id"] else None
        project = _project_dir(doing)
        dirs = _run_dirs(project) if project else []
        run = run_view(dirs[-1], now) if dirs else None
        started = cur["started_at"] if cur else None
        if run and started and run["last_t"] < started - 5:
            run = None  # the newest run folder is from an earlier attempt: this one has not written anything yet
        last_t = max(run["last_t"] if run else 0.0, started or 0.0) or None
        live = {
            "item": _public(doing), "project": _public(doing)["project"], "run": run,
            "engine": cur["backend"] if cur else "", "why": cur["why"] if cur else "", "started_at": started,
            "last_t": last_t, "quiet": last_t is None or now - last_t > STALL_S,
        }  # fmt: skip
    else:
        panel = next((r for r in runs.overview(workspace)["runs"] if r["status"] == "running"), None)
        path = runs.run_dir(workspace, panel["project"], panel["run"]) if panel else None
        if panel and path:
            run = run_view(path, now)
            live = {
                "item": None, "project": panel["project"], "task": panel["task"], "run": run, "engine": "",
                "why": "", "started_at": None, "last_t": run["last_t"], "quiet": False,
            }  # fmt: skip

    todo_claude = any(it["state"] == "todo" and it.get("tier") == 2 for it in items)
    if live:
        state = "quiet" if live["quiet"] else "working"
    elif auto["claude_paused"] and todo_claude:
        state = "waiting"
    else:
        state = "idle"
    last = next((it for it in items if it["id"] == auto["last_item"]), None)
    upcoming = next((it for it in items if it["state"] == "todo"), None)
    return {
        "now": now, "state": state, "live": live, "queue": [_public(it) for it in items], "counts": counts,
        "autopilot": auto, "last": _public(last) if last else None, "next": _public(upcoming) if upcoming else None,
        "recent": _recent(items, workspace), "log": log(root), "status": status(root), "ollama": ollama(),
    }  # fmt: skip
