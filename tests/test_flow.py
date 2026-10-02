from __future__ import annotations

from typing import Any

from swarm.flow import build_flow_html

SAMPLE: dict[str, Any] = {
    "status": "needs_attention",
    "task": "build a widget",
    "seconds": 180,
    "calls": [
        {"role": "researcher", "label": "online research", "phase": "research", "model": "sonnet",
         "ok": True, "cost_usd": 0.20, "turns": 6, "seconds": 60, "denied": 0},
        {"role": "architect", "label": "design", "phase": "plan", "model": "sonnet",
         "ok": True, "cost_usd": 0.40, "turns": 14, "seconds": 70, "denied": 0},
        {"role": "developer", "label": "W1 storage", "phase": "implement", "model": "sonnet",
         "ok": False, "cost_usd": 0.30, "turns": 20, "seconds": 50, "error": "boom", "denied": 2},
    ],  # fmt: skip
    "gate": [
        {"command": "python -m pytest -q", "ok": False, "skipped": "", "exit_code": 1},
        {"command": "ruff check .", "ok": True, "skipped": "", "exit_code": 0},
    ],
}


def test_build_flow_html_has_every_agent_and_phase() -> None:
    h = build_flow_html(SAMPLE)
    assert h.strip().startswith("<!doctype html>")
    for role in ("researcher", "architect", "developer"):
        assert role in h
    for phase in ("RESEARCH", "PLAN", "IMPLEMENT"):
        assert phase in h


def test_totals_and_states() -> None:
    h = build_flow_html(SAMPLE)
    assert "$0.90" in h  # 0.20 + 0.40 + 0.30
    assert "needs_attention" in h and "3" in h  # status + call count tile
    assert "failed: boom" in h and "2 blocked" in h
    assert "done" in h  # the ok nodes


def test_gate_rendered() -> None:
    h = build_flow_html(SAMPLE)
    assert "python -m pytest -q" in h and "ruff check ." in h
    assert "fail" in h and "pass" in h


def test_phases_sorted_canonically() -> None:
    # calls given out of pipeline order still appear research -> plan -> implement
    out_of_order = {"calls": [
        {"role": "developer", "phase": "implement", "ok": True},
        {"role": "researcher", "phase": "research", "ok": True},
    ]}  # fmt: skip
    h = build_flow_html(out_of_order)
    assert h.index("RESEARCH") < h.index("IMPLEMENT")


def test_empty_run() -> None:
    h = build_flow_html({"status": "failed", "calls": []})
    assert "No agent activity" in h


def test_html_is_escaped() -> None:
    call = {"role": "dev", "label": "<script>alert(1)</script>", "phase": "implement", "ok": True}
    h = build_flow_html({"calls": [call]})
    assert "<script>alert" not in h
    assert "&lt;script&gt;" in h
