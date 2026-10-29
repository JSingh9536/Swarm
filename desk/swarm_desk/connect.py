"""`swarm-desk connect | disconnect | status`: wire every Claude Code terminal session into the desk.

connect adds async hooks to the user's `~/.claude/settings.json` (backed up first, other settings untouched) and
registers the `swarm-desk` MCP server at user scope with `claude mcp add`. disconnect removes exactly what connect
added. Our hook entries are recognised by their `swarm_desk.hook` argument.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from swarm_desk import sessions

MARK = "swarm_desk.hook"
MCP_NAME = "swarm-desk"
# event -> matcher ("" = every call). Test gates only need Bash results; everything else is cheap to record.
EVENTS: dict[str, str] = {
    "SessionStart": "", "UserPromptSubmit": "", "PreToolUse": "", "PostToolUse": "Bash", "PostToolUseFailure": "",
    "PermissionDenied": "", "SubagentStart": "", "SubagentStop": "", "Stop": "", "StopFailure": "", "SessionEnd": "",
}  # fmt: skip
SYNC = {"SessionEnd"}  # the session is closing: write the summary before Claude Code exits


def settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def python() -> str:
    venv = sessions.DESK_DIR.parent / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv) if venv.exists() else sys.executable


def handler(event: str, py: str) -> dict[str, Any]:
    h: dict[str, Any] = {"type": "command", "command": py, "args": ["-m", MARK], "timeout": 10}
    if event not in SYNC:
        h["async"] = True
    return h


def _ours(h: Any) -> bool:
    return isinstance(h, dict) and MARK in (h.get("args") or [])


def strip(settings: dict[str, Any]) -> dict[str, Any]:
    """Settings with our hook handlers (and groups left empty by removing them) taken out."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        groups = hooks[event] if isinstance(hooks[event], list) else []
        kept = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                inner = [h for h in g["hooks"] if not _ours(h)]
                if not inner and g["hooks"]:
                    continue
                g = {**g, "hooks": inner}
            kept.append(g)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return settings


def merge(settings: dict[str, Any], py: str) -> dict[str, Any]:
    settings = strip(settings)
    hooks = settings.setdefault("hooks", {})
    for event, matcher in EVENTS.items():
        group: dict[str, Any] = {"hooks": [handler(event, py)]}
        if matcher:
            group["matcher"] = matcher
        hooks.setdefault(event, []).append(group)
    return settings


def installed(settings: dict[str, Any]) -> set[str]:
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    return {
        e
        for e, groups in hooks.items()
        for g in groups or []
        if isinstance(g, dict) and any(map(_ours, g.get("hooks") or []))
    }


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _save(path: Path, data: dict[str, Any]) -> Path | None:
    backup = None
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def _claude(*args: str) -> subprocess.CompletedProcess[str] | None:
    exe = shutil.which("claude")
    if not exe:
        return None
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=60, check=False)  # noqa: S603


def mcp_registered() -> bool | None:
    r = _claude("mcp", "get", MCP_NAME)
    return None if r is None else r.returncode == 0


def connect() -> int:
    py = python()
    path = settings_path()
    backup = _save(path, merge(_load(path), py))
    print(f"hooks: {len(EVENTS)} events -> {path}" + (f" (backup: {backup.name})" if backup else ""))
    if mcp_registered():
        _claude("mcp", "remove", "--scope", "user", MCP_NAME)
    r = _claude("mcp", "add", "--scope", "user", MCP_NAME, "--", py, "-m", "swarm_desk.mcp")
    if r is None:
        print("mcp: `claude` is not on PATH; add it yourself:  claude mcp add --scope user swarm-desk -- "
              f"{py} -m swarm_desk.mcp")  # fmt: skip
    else:
        print("mcp: " + ("registered swarm-desk (user scope)" if r.returncode == 0 else f"FAILED: {r.stderr.strip()}"))
    print(f"sessions are recorded under {sessions.root()}")
    print("New Claude Code sessions show up in Swarm Desk; restart sessions that are already open.")
    return 0 if r is None or r.returncode == 0 else 1


def disconnect() -> int:
    path = settings_path()
    data = _load(path)
    if installed(data):
        backup = _save(path, strip(data))
        print(f"hooks: removed from {path}" + (f" (backup: {backup.name})" if backup else ""))
    else:
        print("hooks: none installed")
    if mcp_registered():
        _claude("mcp", "remove", "--scope", "user", MCP_NAME)
        print("mcp: removed swarm-desk")
    return 0


def status() -> int:
    events = installed(_load(settings_path()))
    print(
        f"hooks: {len(events)}/{len(EVENTS)} events" + ("" if events >= set(EVENTS) else "  (run: swarm-desk connect)")
    )
    reg = mcp_registered()
    print("mcp: " + {True: "registered", False: "not registered", None: "unknown (`claude` not on PATH)"}[reg])
    base = sessions.root()
    count = sum(1 for _ in base.glob("*/.swarm/runs/*")) if base.is_dir() else 0
    print(f"sessions recorded: {count} under {base}")
    return 0
