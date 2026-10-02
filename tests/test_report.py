from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from swarm.gates import CmdResult, GateResult
from swarm.models import Finding, Plan
from swarm.report import (
    STATUS_TEXT,
    CallRecord,
    ConsoleReporter,
    RecordingReporter,
    RunSummary,
    render_report,
    summary_dict,
)


def call(role: str = "developer", ok: bool = True, cost: float = 0.25, **kw: object) -> CallRecord:
    base = dict(role=role, label="W1", phase="implement", model="sonnet", ok=ok, cost_usd=cost, turns=5, seconds=75)
    base.update(kw)
    return CallRecord(**base)  # type: ignore[arg-type]


def summary(status: str = "success", **kw: object) -> RunSummary:
    base: dict[str, object] = dict(status=status, task="Build a todo CLI\nwith details", project_dir=Path("proj"),
                                   run_dir=Path("proj/.swarm/runs/r1"))  # fmt: skip
    base.update(kw)
    return RunSummary(**base)  # type: ignore[arg-type]


PLAN = Plan.model_validate(
    {
        "title": "Todo CLI",
        "complexity": "small",
        "goal": "Manage todos",
        "acceptance_criteria": [{"id": "AC1", "text": "add works"}],
        "work_items": [],
        "run_instructions": "python todo.py",
    }
)


@pytest.mark.parametrize("status", sorted(STATUS_TEXT))
def test_every_status_renders(status: str) -> None:
    text = render_report(summary(status))
    assert f"**Status:** {STATUS_TEXT[status]}" in text
    assert text.startswith("# Run report: Build a todo CLI")


def test_plan_limit_status_explains_why() -> None:
    assert "plan" in STATUS_TEXT["plan_limit"].lower()
    assert "paid" in STATUS_TEXT["plan_limit"].lower()


def test_unknown_status_passthrough() -> None:
    assert "**Status:** weird" in render_report(summary("weird"))


def test_full_report() -> None:
    s = summary(
        plan=PLAN,
        calls=[call(), call("reviewer", ok=False, cost=0.1, error="timed | out\nbadly", denied=2)],
        open_findings=[Finding(severity="major", location="a.py:3", problem="off by one", fix="use <=")],
        gate=GateResult([CmdResult("pytest", 0), CmdResult("ruff check .", 1), CmdResult("mypy", None, skipped="n/a")]),
        changed_files=["a.py", "tests/test_a.py"],
        seconds=125,
        notes=["a note"],
        commit_note="committed on branch swarm/x",
    )
    text = render_report(s)
    assert text.startswith("# Run report: Todo CLI")
    assert "**Time:** 2m05s | **Estimated cost:** $0.35 | **Agent calls:** 2" in text
    assert "- AC1: add works" in text and "## How to run\npython todo.py" in text
    assert "- `a.py`" in text and "- `tests/test_a.py`" in text
    assert "- `pytest` -> PASS" in text and "- `ruff check .` -> FAIL" in text and "skipped (n/a)" in text
    assert "- [major] a.py:3: off by one - fix: use <=" in text
    assert "- a note" in text and "**Git:** committed on branch swarm/x" in text
    rows = [ln for ln in text.splitlines() if ln.startswith("| ") and ln[2].isdigit()]
    assert len(rows) == 2
    assert rows[0] == "| 1 | implement | developer | sonnet | 5 | $0.25 | 1m15s | ok |"
    assert "failed: timed \\| out badly" in rows[1] and "(2 blocked by guard)" in rows[1]
    assert rows[1].replace("\\|", "").count("|") == 9, "pipes in errors must be escaped so the table keeps its shape"


def test_no_gate_message_and_file_cap() -> None:
    text = render_report(summary(changed_files=[f"f{i}.py" for i in range(85)]))
    assert "No automated test or lint command" in text
    assert "- ... and 5 more" in text and "- `f79.py`" in text and "- `f80.py`" not in text


def test_summary_dict_is_json() -> None:
    s = summary(
        calls=[call()], gate=GateResult([CmdResult("pytest", 0)]), open_findings=[Finding(severity="nit", problem="x")]
    )
    data = json.loads(json.dumps(summary_dict(s)))
    assert data["status"] == "success" and data["cost_usd"] == 0.25
    assert data["gate"] == [{"command": "pytest", "ok": True, "skipped": "", "exit_code": 0}]
    assert data["calls"][0]["role"] == "developer"
    assert data["open_findings"][0]["severity"] == "nit"


def test_recording_reporter() -> None:
    r = RecordingReporter()
    r.phase("plan")
    r.agent_start("architect", "design", "sonnet")
    r.agent_event("architect", "tool", "Read a.py")
    r.agent_end(call("architect"))
    r.note("hi")
    assert r.phases == ["plan"] and r.starts == [("architect", "design", "sonnet")]
    assert r.events == [("architect", "tool", "Read a.py")] and r.ends[0].role == "architect" and r.notes == ["hi"]


# --------------------------------------------------------------------------- console output


def console_reporter(verbose: bool = False) -> tuple[ConsoleReporter, io.StringIO]:
    buf = io.StringIO()
    return ConsoleReporter(Console(file=buf, width=200, color_system=None), verbose=verbose), buf


def test_console_lines_are_ascii() -> None:
    rep, buf = console_reporter()
    rep.phase("implement", "(round 1)")
    rep.agent_start("developer", "W1 storage", None)
    rep.agent_event("developer", "tool", "Read a.py")
    rep.agent_event("developer", "limit", "plan usage warning: five_hour window at 75%")
    rep.agent_end(call(ok=False, error="boom", denied=1))
    rep.note("something")
    out = buf.getvalue()
    assert out.isascii()
    assert "== IMPLEMENT (round 1)" in out
    assert "> developer (default): W1 storage" in out
    assert "! plan usage warning" in out
    assert "< developer FAILED: boom" in out and "(1 blocked)" in out
    assert "* something" in out


def test_tool_events_are_throttled() -> None:
    rep, buf = console_reporter()
    rep.agent_start("developer", "W1", None)
    for i in range(1, 26):
        rep.agent_event("developer", "tool", f"Tool-{i:02d}")
    shown = [i for i in range(1, 26) if f"Tool-{i:02d}" in buf.getvalue()]
    assert shown == [1, 2, 3, 4, 10, 20]


def test_blocked_tools_always_shown() -> None:
    rep, buf = console_reporter()
    rep.agent_start("developer", "W1", None)
    for i in range(1, 8):
        rep.agent_event("developer", "tool", f"BLOCKED Bash cmd{i} (nope)")
    assert all(f"cmd{i}" in buf.getvalue() for i in range(1, 8))


def test_throttle_resets_per_agent_start() -> None:
    rep, buf = console_reporter()
    rep.agent_start("developer", "W1", None)
    for _ in range(6):
        rep.agent_event("developer", "tool", "x")
    rep.agent_start("developer", "W2", None)
    rep.agent_event("developer", "tool", "second-agent-first-tool")
    assert "second-agent-first-tool" in buf.getvalue()


def test_verbose_shows_everything() -> None:
    rep, buf = console_reporter(verbose=True)
    rep.agent_start("developer", "W1", None)
    for i in range(1, 8):
        rep.agent_event("developer", "tool", f"Tool-{i}")
    rep.agent_event("developer", "text", "first line\nsecond line")
    out = buf.getvalue()
    assert all(f"Tool-{i}" in out for i in range(1, 8))
    assert "| first line" in out and "second line" not in out


def test_text_hidden_when_not_verbose() -> None:
    rep, buf = console_reporter()
    rep.agent_event("developer", "text", "secret thoughts")
    assert "secret thoughts" not in buf.getvalue()
