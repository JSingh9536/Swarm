"""Claude Code hook: turns one hook payload (JSON on stdin) into desk events for that session.

Installed by `swarm-desk connect` as an async hook on every event it maps, so it never slows a session down. It
records which agent did what (prompts, tool calls, subagents, blocked calls, test runs), keeps only short
descriptions (a command's first line, a file path, never file contents), always exits 0 and prints nothing.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from swarm_desk import sessions

LEAD = "lead"
DELEGATE_TOOLS = {"Task", "Agent"}  # subagent starts are reported by SubagentStart; the lead's side is the handoff
_TEST_CMD = re.compile(
    r"(?:^|[\s;&|/])(pytest|ruff|mypy|tox|unittest|jest|vitest|go test|cargo test|swift test|xcodebuild test"
    r"|(?:npm|pnpm|yarn|bun)(?: run)? (?:test|lint|check))\b"
)
_ROLE = re.compile(r"[^\w-]+")


def role_of(data: dict[str, Any]) -> str:
    """The agent behind a payload: the subagent's type, the `--agent` name of the main session, or the lead."""
    name = _ROLE.sub("-", str(data.get("agent_type") or "")).strip("-")[:40]
    return name or LEAD


def describe(tool: str, data: Any) -> str:
    data = data if isinstance(data, dict) else {}
    for key in ("file_path", "path", "pattern", "command", "url", "query", "notebook_path", "description"):
        value = data.get(key)
        if value:
            first = str(value).strip().splitlines()[0] if str(value).strip() else ""
            return f"{tool} {first[:100]}"
    return tool


def test_label(data: dict[str, Any]) -> str | None:
    """`pytest`, `npm test`, ... when a Bash call is a test or lint run (the session's gates)."""
    if data.get("tool_name") != "Bash":
        return None
    command = str((data.get("tool_input") or {}).get("command") or "")
    m = _TEST_CMD.search(command)
    return m.group(1) if m else None


def first_line(text: Any, limit: int = 120) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def events_for(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Desk events for one hook payload (see `swarm_desk.brain` for what each kind does)."""
    name = data.get("hook_event_name")
    role = role_of(data)
    tool = str(data.get("tool_name") or "")
    if name == "SessionStart":
        return [{"kind": "phase", "name": "session", "detail": str(data.get("source") or "")}]
    if name == "UserPromptSubmit":
        return [{"kind": "agent_start", "role": role, "label": first_line(data.get("prompt"), 100) or "prompt",
                 "model": str(data.get("model") or ""), "exclusive": False}]  # fmt: skip
    if name == "PreToolUse":
        if tool in DELEGATE_TOOLS:
            ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
            to = ti.get("subagent_type") or "agent"
            return [{"kind": "activity", "role": role, "what": "delegate",
                     "text": f"→ {to}: {first_line(ti.get('description'), 80)}".rstrip(": ")}]  # fmt: skip
        return [{"kind": "activity", "role": role, "what": "tool", "text": describe(tool, data.get("tool_input"))}]
    if name in ("PostToolUse", "PostToolUseFailure"):
        failed = name == "PostToolUseFailure"
        label = test_label(data)
        if label:
            return [{"kind": "gate", "label": label, "state": "FAIL" if failed else "PASS", "role": role}]
        if failed and tool not in DELEGATE_TOOLS:
            return [{"kind": "activity", "role": role, "what": "tool", "failed": True,
                     "text": f"failed: {describe(tool, data.get('tool_input'))}"}]  # fmt: skip
        return []
    if name == "PermissionDenied":
        return [{"kind": "activity", "role": role, "what": "tool", "blocked": True,
                 "text": f"BLOCKED {describe(tool, data.get('tool_input'))}"}]  # fmt: skip
    if name == "SubagentStart":
        return [{"kind": "agent_start", "role": role, "label": role, "exclusive": False}]
    if name == "SubagentStop":
        return [
            {"kind": "agent_end", "role": role, "ok": True, "text": first_line(data.get("last_assistant_message"))},
            {"kind": "resume", "role": LEAD},
        ]
    if name == "Stop":
        return [{"kind": "agent_end", "role": role, "ok": True, "text": first_line(data.get("last_assistant_message"))}]
    if name == "StopFailure":
        return [{"kind": "agent_end", "role": role, "ok": False, "error": first_line(data.get("error") or "stopped")}]
    if name == "SessionEnd":
        return [{"kind": "phase", "name": "ended", "detail": str(data.get("reason") or "")}]
    return []


def record(data: dict[str, Any]) -> None:
    folder = sessions.run_for(str(data.get("session_id") or ""), data.get("cwd"))
    if folder is None:
        return
    sessions.append(folder, events_for(data))
    if data.get("hook_event_name") == "UserPromptSubmit" and data.get("prompt"):
        sessions.write_once(folder, "task.md", str(data["prompt"])[:4000])
    if data.get("hook_event_name") == "SessionEnd":
        sessions.write_once(folder, "summary.json", json.dumps({"status": "success", "source": "claude-code"}))


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        if isinstance(data, dict):
            record(data)
    except Exception:  # noqa: BLE001 - a hook must never break the session it watches
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
