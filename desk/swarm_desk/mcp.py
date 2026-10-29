"""`swarm-desk` MCP server (stdio): lets the lead of a Claude Code session tell the desk what only it knows.

The hooks already record every prompt, tool call and subagent. These tools add the structure on top: which phase the
work is in (plan, build, review round 2), a gate's verdict, a note. They write into the live session run for the
folder this server was started in, and only ever append to that run's `events.jsonl`.
Standard library only: newline-delimited JSON-RPC 2.0 on stdin/stdout, as MCP's stdio transport specifies.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from swarm_desk import __version__, sessions

PROTOCOL = "2025-06-18"
_TEXT = {"type": "string", "maxLength": 200}
TOOLS = [
    {
        "name": "desk_phase",
        "description": "Show the phase the team is in on the Swarm Desk viewer (e.g. research, plan, implement, "
        "verify, review, fix). Call it when the work moves to a new phase or round.",
        "inputSchema": {"type": "object", "properties": {"name": _TEXT, "detail": _TEXT}, "required": ["name"]},
    },
    {
        "name": "desk_gate",
        "description": "Record a test/lint gate verdict on the Swarm Desk viewer, after you ran the checks.",
        "inputSchema": {
            "type": "object",
            "properties": {"label": _TEXT, "passed": {"type": "boolean"}},
            "required": ["label", "passed"],
        },
    },
    {
        "name": "desk_note",
        "description": "Post a short note from the lead to the Swarm Desk activity feed (a decision, a handoff).",
        "inputSchema": {"type": "object", "properties": {"text": _TEXT}, "required": ["text"]},
    },
]


def _clip(value: Any, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def call(name: str, args: dict[str, Any], cwd: str) -> str:
    folder = sessions.latest_live(cwd)
    if folder is None:
        return "No live Claude Code session is recorded for this folder (run `swarm-desk connect`); nothing shown."
    if name == "desk_phase":
        phase = _clip(args.get("name"), 40).lower() or "work"
        sessions.append(folder, [{"kind": "phase", "name": phase, "detail": _clip(args.get("detail"), 120)}])
        return f"Phase shown: {phase}"
    if name == "desk_gate":
        passed = bool(args.get("passed"))
        label = _clip(args.get("label"), 60) or "checks"
        sessions.append(folder, [{"kind": "gate", "label": label, "state": "PASS" if passed else "FAIL"}])
        return f"Gate recorded: {label} {'PASS' if passed else 'FAIL'}"
    if name == "desk_note":
        sessions.append(folder, [{"kind": "activity", "role": "lead", "what": "note", "text": _clip(args.get("text"))}])
        return "Note posted"
    raise KeyError(name)


def handle(msg: dict[str, Any], cwd: str) -> dict[str, Any] | None:
    """One JSON-RPC message in, its response out (None for notifications)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        params = msg.get("params") or {}
        result: Any = {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "swarm-desk", "version": __version__},
            "instructions": "Swarm Desk shows this session as a live neural view. Call desk_phase when the work "
            "changes phase or round, and desk_gate after running tests or lint.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            text = call(str(params.get("name")), args, cwd)
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        except OSError as exc:
            result = {"content": [{"type": "text", "text": f"Could not write: {exc}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> int:
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse"}}
        else:
            reply = handle(msg, cwd) if isinstance(msg, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
