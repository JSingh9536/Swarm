"""Offline tests for wiring Claude Code sessions in: hook mapping, the session store, the MCP server, connect."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

from swarm_desk import connect, hook, mcp, sessions
from swarm_desk.brain import Brain
from swarm_desk.feed import RunRef, newest_active


@pytest.fixture(autouse=True)
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "sessions"
    monkeypatch.setenv("SWARM_DESK_SESSIONS", str(root))
    return root


def payload(event: str, **fields: object) -> dict[str, object]:
    return {"session_id": "abc-123", "cwd": "/Users/me/My Project!", "hook_event_name": event, **fields}


def feed(*payloads: dict[str, object]) -> list[dict[str, object]]:
    for p in payloads:
        hook.record(p)
    folder = sessions.run_for("abc-123", "/Users/me/My Project!", create=False)
    assert folder is not None
    return [json.loads(x) for x in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------ hook mapping


def test_a_session_with_parallel_subagents_and_a_test_loop() -> None:
    evs = feed(
        payload("SessionStart", source="startup"),
        payload("UserPromptSubmit", prompt="/team-build add a health endpoint\nmore detail"),
        payload("PreToolUse", tool_name="Agent", tool_input={"subagent_type": "developer", "description": "W1"}),
        payload("SubagentStart", agent_id="a1", agent_type="developer"),
        payload("SubagentStart", agent_id="a2", agent_type="reviewer"),
        payload("PreToolUse", agent_id="a1", agent_type="developer", tool_name="Write",
                tool_input={"file_path": "app.py", "content": "SECRET CONTENT"}),
        payload("PostToolUseFailure", agent_id="a1", agent_type="developer", tool_name="Bash",
                tool_input={"command": "python -m pytest -q"}),
        payload("PostToolUse", agent_id="a1", agent_type="developer", tool_name="Bash",
                tool_input={"command": "cd x && npm run test"}),
        payload("PermissionDenied", agent_id="a2", agent_type="reviewer", tool_name="Bash",
                tool_input={"command": "git push"}),
        payload("SubagentStop", agent_id="a1", agent_type="developer", last_assistant_message="Done.\nsecond line"),
        payload("SubagentStop", agent_id="a2", agent_type="reviewer"),
        payload("Stop", last_assistant_message="All set."),
        payload("SessionEnd", reason="prompt_input_exit"),
    )  # fmt: skip
    kinds = [e["kind"] for e in evs]
    assert kinds[:3] == ["phase", "agent_start", "activity"] and kinds.count("resume") == 2
    assert evs[1]["label"] == "/team-build add a health endpoint" and evs[1]["exclusive"] is False
    assert evs[2]["text"] == "→ developer: W1"
    assert [e for e in evs if e["kind"] == "gate"] == [
        {"t": evs[6]["t"], "kind": "gate", "label": "pytest", "state": "FAIL", "role": "developer"},
        {"t": evs[7]["t"], "kind": "gate", "label": "npm run test", "state": "PASS", "role": "developer"},
    ]
    assert "SECRET CONTENT" not in json.dumps(evs), "tool contents are never recorded"
    assert any(e.get("blocked") and "git push" in e["text"] for e in evs)

    folder = sessions.run_for("abc-123", "/Users/me/My Project!", create=False)
    assert folder is not None and folder.parents[2].name == "My-Project"
    assert (folder / "task.md").read_text().startswith("/team-build") and (folder / "summary.json").exists()

    b = Brain()
    for e in evs:
        b.apply(e)
    assert {(s.a, s.b) for s in b.synapses.values()} >= {
        ("lead", "developer"), ("lead", "reviewer"), ("developer", "lead"), ("reviewer", "lead"), ("developer", "gate"),
    }  # fmt: skip
    assert b.neurons["lead"].state == "done" and b.neurons["developer"].running == 0
    assert b.neurons["reviewer"].blocked == 1 and b.gates == [("pytest", "FAIL"), ("npm run test", "PASS")]


def test_lead_waits_while_subagents_run_and_wakes_when_the_last_returns() -> None:
    b = Brain()
    for e in [
        {"kind": "agent_start", "role": "lead", "label": "go", "exclusive": False},
        {"kind": "agent_start", "role": "developer", "exclusive": False},
        {"kind": "agent_start", "role": "developer", "exclusive": False},  # two in parallel share one neuron
        {"kind": "agent_end", "role": "developer", "ok": True},
        {"kind": "resume", "role": "lead"},
    ]:
        b.apply(e)
    assert b.neurons["developer"].state == "active" and b.neurons["lead"].state == "waiting"
    b.apply({"kind": "agent_end", "role": "developer", "ok": True})
    b.apply({"kind": "resume", "role": "lead"})
    assert b.neurons["developer"].state == "done" and b.neurons["lead"].state == "active"


def test_hook_ignores_bad_input_and_never_raises(monkeypatch: pytest.MonkeyPatch, store: Path) -> None:
    for raw in ("", "not json", "[1, 2]", json.dumps({"session_id": "../../etc", "hook_event_name": "Stop"})):
        monkeypatch.setattr("sys.stdin", io.StringIO(raw))
        assert hook.main() == 0
    assert not store.exists() or not any(store.rglob("events.jsonl"))


def test_main_session_started_with_an_agent_is_that_agent() -> None:
    assert hook.role_of({"agent_type": "tech-lead"}) == "tech-lead" and hook.role_of({}) == "lead"
    assert hook.role_of({"agent_type": "../evil name"}) == "evil-name"


# ------------------------------------------------------------------ session store


def test_parallel_hooks_of_one_session_agree_on_one_folder() -> None:
    seen: list[Path | None] = []
    threads = [threading.Thread(target=lambda: seen.append(sessions.run_for("s1", "/x/proj"))) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(seen)) == 1 and seen[0] is not None and seen[0].is_dir()
    other = sessions.run_for("s2", "/x/proj")
    assert other is not None and other != seen[0], "two sessions in the same second get separate runs"


def test_newest_active_finds_the_working_session(store: Path) -> None:
    hook.record(payload("SessionStart", source="startup"))
    assert newest_active(store) is None, "a session nobody prompted yet (or a one-off CLI call) is not followed"
    hook.record(payload("UserPromptSubmit", prompt="hi"))
    ref = newest_active(store)
    assert ref is not None and ref.source == "claude" and ref.project == "My-Project"
    assert RunRef.from_iid(ref.iid) == ref and RunRef.from_iid("group:claude") is None
    hook.record(payload("SessionEnd"))
    assert newest_active(store) is None


# ------------------------------------------------------------------ MCP server


def rpc(method: str, mid: int | None = 1, **params: object) -> dict[str, object] | None:
    return mcp.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}, "/Users/me/My Project!")


def test_mcp_handshake_and_tools() -> None:
    init = rpc("initialize", protocolVersion="2025-06-18", capabilities={})
    assert init and init["result"]["serverInfo"]["name"] == "swarm-desk"  # type: ignore[index]
    assert rpc("notifications/initialized", None) is None
    names = [t["name"] for t in rpc("tools/list")["result"]["tools"]]  # type: ignore[index]
    assert names == ["desk_phase", "desk_gate", "desk_note"]
    assert "No live" in rpc("tools/call", name="desk_phase", arguments={"name": "plan"})["result"]["content"][0]["text"]  # type: ignore[index]

    hook.record(payload("UserPromptSubmit", prompt="hi"))
    rpc("tools/call", name="desk_phase", arguments={"name": "Review", "detail": "round 2"})
    rpc("tools/call", name="desk_gate", arguments={"label": "pytest", "passed": False})
    rpc("tools/call", name="desk_note", arguments={"text": "sending   it back\nto the developer"})
    assert rpc("tools/call", name="rm", arguments={})["error"]["code"] == -32602  # type: ignore[index]
    assert rpc("resources/list")["error"]["code"] == -32601  # type: ignore[index]
    folder = sessions.latest_live("/Users/me/My Project!")
    evs = [json.loads(x) for x in (folder / "events.jsonl").read_text().splitlines()]  # type: ignore[union-attr]
    assert [e["kind"] for e in evs[-3:]] == ["phase", "gate", "activity"]
    assert (
        evs[-3]["name"] == "review"
        and evs[-2]["state"] == "FAIL"
        and evs[-1]["text"] == "sending it back to the developer"
    )


def test_mcp_stdio_loop(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), "garbage", ""]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines)))
    assert mcp.main() == 0
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert out[0] == {"jsonrpc": "2.0", "id": 1, "result": {}} and out[1]["error"]["code"] == -32700


# ------------------------------------------------------------------ connect


def test_connect_merges_and_disconnect_restores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = {"type": "command", "command": "/usr/bin/say hi"}
    original = {
        "theme": "dark",
        "hooks": {"Stop": [{"hooks": [mine]}], "PreToolUse": [{"matcher": "Bash", "hooks": [mine]}]},
    }
    merged = connect.merge(json.loads(json.dumps(original)), "/py")
    assert connect.installed(merged) == set(connect.EVENTS)
    assert merged["theme"] == "dark" and mine in merged["hooks"]["Stop"][0]["hooks"]
    ours = merged["hooks"]["PostToolUse"][-1]
    assert ours == {"hooks": [{"type": "command", "command": "/py", "args": ["-m", "swarm_desk.hook"], "timeout": 10,
                               "async": True}], "matcher": "Bash"}  # fmt: skip
    assert "async" not in merged["hooks"]["SessionEnd"][-1]["hooks"][0]
    again = connect.merge(json.loads(json.dumps(merged)), "/py")
    assert again == merged, "connecting twice adds nothing"
    assert connect.strip(json.loads(json.dumps(merged))) == original

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(connect, "_claude", lambda *a: None)  # no real `claude mcp` calls in tests
    path = connect.settings_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(original), encoding="utf-8")
    assert connect.connect() == 0 and connect.installed(json.loads(path.read_text())) == set(connect.EVENTS)
    assert connect.disconnect() == 0 and json.loads(path.read_text()) == original
    assert len(list(path.parent.glob("settings.json.bak-*"))) >= 1
