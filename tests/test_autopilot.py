"""Unit tests for the autopilot: queue round-trip, routing, and pause/resume around the Claude plan limit.

Offline and free: `runner` is always a fake, no model is ever called, and `decide()` decisions are built
from literal `Signals` (see tests/test_load.py) rather than probing real hardware.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm import workqueue
from swarm.autopilot import Outcome, State, choose, claude_available, dry_run_lines, loop, record, step
from swarm.load import Signals, decide
from swarm.workqueue import QueueError

IDLE = decide(Signals(cpu_pct=5))
BUSY = decide(Signals(gpu_util_pct=80, cpu_pct=30, games=["game.exe"]))


def _item(**kw) -> dict:
    base = {
        "id": 1, "state": "todo", "tier": 1, "project": "", "task": "do the thing", "check": "tests pass",
        "urgent": False, "attempts_local": 0, "attempts_claude": 0, "note": "", "report": "",
    }  # fmt: skip
    base.update(kw)
    return base


def _runner_queue(outcomes: list[Outcome]):
    """A fake runner that returns the given outcomes in order; never starts a real run."""
    calls = iter(outcomes)
    return lambda item, backend: next(calls)


# --------------------------------------------------------------------------- workqueue: load/save/validate


def test_queue_round_trips_atomically(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    items = [_item(id=1), _item(id=2, tier=2)]
    workqueue.save(path, items)
    assert workqueue.load(path) == items
    assert not list(tmp_path.glob(".queue-*.tmp"))  # no leftover temp file


def test_load_missing_file_is_an_empty_queue(tmp_path: Path) -> None:
    assert workqueue.load(tmp_path / "nope.json") == []


def test_load_rejects_malformed_queue(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(QueueError):
        workqueue.load(path)

    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(QueueError):
        workqueue.load(path)

    path.write_text(json.dumps([{"id": 1}]), encoding="utf-8")  # missing required fields
    with pytest.raises(QueueError):
        workqueue.load(path)

    path.write_text(json.dumps([_item(id=1), _item(id=1)]), encoding="utf-8")  # duplicate id
    with pytest.raises(QueueError):
        workqueue.load(path)

    path.write_text(json.dumps([_item(id=1, tier=9)]), encoding="utf-8")  # bad tier
    with pytest.raises(QueueError):
        workqueue.load(path)


def test_next_runnable_order_and_tier_rules() -> None:
    items = [_item(id=1, tier=3), _item(id=2, tier=0), _item(id=3, tier=2), _item(id=4, tier=1)]
    assert workqueue.next_runnable(items, claude_available=True)["id"] == 3
    assert workqueue.next_runnable(items, claude_available=False)["id"] == 4  # tier 2 skipped, tier 1 is next


def test_next_runnable_tier2_local_fallback_ignores_claude_availability() -> None:
    items = [_item(id=1, tier=2, local_fallback=True)]
    assert workqueue.next_runnable(items, claude_available=False)["id"] == 1


def test_next_runnable_tier3_is_never_returned() -> None:
    assert workqueue.next_runnable([_item(id=1, tier=3)], claude_available=True) is None


def test_mark_updates_item_in_place() -> None:
    items = [_item(id=1)]
    workqueue.mark(items, 1, state="done", note="ok")
    assert items[0]["state"] == "done" and items[0]["note"] == "ok"


def test_mark_missing_id_raises() -> None:
    with pytest.raises(QueueError):
        workqueue.mark([_item(id=1)], 99, state="done")


# --------------------------------------------------------------------------- claude_available / choose


def test_claude_available() -> None:
    assert claude_available(State(), now=0.0)
    assert not claude_available(State(claude_paused_until=100.0), now=50.0)
    assert claude_available(State(claude_paused_until=100.0), now=100.0)  # the reset instant itself counts


def test_tier1_goes_local_when_the_pc_is_idle() -> None:
    backend, _ = choose(_item(tier=1), State(), IDLE, now=0.0)
    assert backend == "local"


def test_tier1_stays_local_when_the_pc_is_busy() -> None:
    backend, _ = choose(_item(tier=1, urgent=False), State(), BUSY, now=0.0)
    assert backend == "local"


def test_tier1_escalates_to_claude_after_two_local_failures() -> None:
    backend, why = choose(_item(tier=1, attempts_local=2), State(), IDLE, now=0.0)
    assert backend == "claude" and "failed" in why


def test_tier0_and_tier3_are_never_run() -> None:
    assert choose(_item(tier=0), State(), IDLE, now=0.0)[0] == "wait"
    assert choose(_item(tier=3), State(), IDLE, now=0.0)[0] == "wait"


def test_tier2_waits_while_claude_is_paused_without_local_fallback() -> None:
    state = State(claude_paused_until=1000.0, reason="plan limit reached")
    backend, why = choose(_item(tier=2), state, IDLE, now=0.0)
    assert backend == "wait" and "paused" in why


def test_tier2_falls_back_to_local_when_allowed() -> None:
    state = State(claude_paused_until=1000.0, reason="plan limit reached")
    backend, _ = choose(_item(tier=2, local_fallback=True), state, IDLE, now=0.0)
    assert backend == "local"


# --------------------------------------------------------------------------- record(): outcomes


def test_plan_limit_pauses_claude_and_is_not_counted_as_an_attempt() -> None:
    item = _item(tier=2, attempts_claude=0)
    state = State()
    record(item, state, "claude", Outcome(status="plan_limit", resets_at=1000.0), now=100.0)
    assert state.claude_paused_until == 1000.0
    assert item["attempts_claude"] == 0
    assert item["state"] == "todo"


def test_plan_limit_with_unknown_reset_pauses_sixty_minutes() -> None:
    item = _item(tier=2)
    state = State()
    record(item, state, "claude", Outcome(status="plan_limit"), now=1000.0)
    assert state.claude_paused_until == pytest.approx(1000.0 + 60 * 60)
    assert "unknown" in state.reason


def test_plan_limit_pauses_until_the_real_reset_however_far() -> None:
    now = 1_700_000_000.0
    for ahead in (5 * 60.0, 4.5 * 3600, 6 * 24 * 3600.0):  # minutes, a five-hour window, a weekly window
        state = State()
        record(_item(tier=2), state, "claude", Outcome(status="plan_limit", resets_at=now + ahead), now=now)
        assert state.claude_paused_until == now + ahead
        assert "unknown" not in state.reason


@pytest.mark.parametrize(
    "resets_at",
    [
        999.0,  # already past: would un-pause at once and re-run the item into the same limit
        1000.0,  # this very instant
        1000.0 + 30 * 24 * 3600,  # further away than any plan window
        float("nan"),
        "soon",
        True,
    ],
)
def test_plan_limit_with_an_unbelievable_reset_pauses_sixty_minutes(resets_at: object) -> None:
    item = _item(tier=2)
    state = State()
    record(item, state, "claude", Outcome(status="plan_limit", resets_at=resets_at), now=1000.0)  # type: ignore[arg-type]
    assert state.claude_paused_until == pytest.approx(1000.0 + 60 * 60)
    assert "unknown" in state.reason
    assert not claude_available(state, now=1000.0)


def test_two_needs_attention_on_claude_blocks_a_tier2_item() -> None:
    item = _item(tier=2)
    state = State()
    record(item, state, "claude", Outcome(status="needs_attention"), now=0.0)
    assert item["state"] == "todo" and item["attempts_claude"] == 1
    record(item, state, "claude", Outcome(status="needs_attention"), now=0.0)
    assert item["state"] == "blocked" and item["attempts_claude"] == 2


def test_two_needs_attention_locally_escalates_a_tier1_item_to_claude() -> None:
    item = _item(tier=1)
    state = State()
    record(item, state, "local", Outcome(status="needs_attention"), now=0.0)
    assert item["state"] == "todo" and item["attempts_local"] == 1
    record(item, state, "local", Outcome(status="needs_attention"), now=0.0)
    assert item["state"] == "todo" and item["attempts_local"] == 2
    backend, why = choose(item, state, IDLE, now=0.0)
    assert backend == "claude" and "failed" in why


def test_success_marks_item_done() -> None:
    item = _item(tier=1)
    state = State()
    record(item, state, "local", Outcome(status="success", report="r.md"), now=0.0)
    assert item["state"] == "done" and item["report"] == "r.md"


# --------------------------------------------------------------------------- step()


def test_step_runs_the_next_runnable_item(tmp_path: Path) -> None:
    queue_path, state_path = tmp_path / "queue.json", tmp_path / "autopilot-state.json"
    workqueue.save(queue_path, [_item(id=1, tier=1)])
    result = step(queue_path, state_path, _runner_queue([Outcome(status="success")]), now=0.0, decision=IDLE)
    assert result["action"] == "ran" and result["status"] == "success" and result["backend"] == "local"
    assert workqueue.load(queue_path)[0]["state"] == "done"


def test_step_records_the_running_item_and_engine_while_it_runs(tmp_path: Path) -> None:
    queue_path, state_path = tmp_path / "queue.json", tmp_path / "autopilot-state.json"
    workqueue.save(queue_path, [_item(id=7, tier=1)])
    seen: list[dict] = []

    def runner(item: dict, backend: str) -> Outcome:
        seen.append(json.loads(state_path.read_text(encoding="utf-8"))["current"])
        return Outcome(status="success")

    step(queue_path, state_path, runner, now=12.0, decision=IDLE)
    assert seen[0]["item"] == 7 and seen[0]["backend"] == "local" and seen[0]["started_at"] == 12.0
    assert State.load(state_path).current is None  # cleared once the item is recorded


def test_step_waits_when_only_a_claude_item_remains_and_claude_is_paused(tmp_path: Path) -> None:
    queue_path, state_path = tmp_path / "queue.json", tmp_path / "autopilot-state.json"
    workqueue.save(queue_path, [_item(id=1, tier=2)])
    State(claude_paused_until=1000.0, reason="plan limit").save(state_path)
    result = step(queue_path, state_path, _runner_queue([]), now=0.0, decision=IDLE)
    assert result["action"] == "wait" and result["item"] == 1


def test_tier1_still_runs_locally_while_a_tier2_item_waits_for_claude(tmp_path: Path) -> None:
    queue_path, state_path = tmp_path / "queue.json", tmp_path / "autopilot-state.json"
    workqueue.save(queue_path, [_item(id=1, tier=2), _item(id=2, tier=1)])
    State(claude_paused_until=1000.0, reason="plan limit").save(state_path)
    result = step(queue_path, state_path, _runner_queue([Outcome(status="success")]), now=0.0, decision=IDLE)
    assert result["action"] == "ran" and result["item"] == 2 and result["backend"] == "local"


def test_step_puts_the_item_back_to_todo_after_a_plan_limit(tmp_path: Path) -> None:
    queue_path, state_path = tmp_path / "queue.json", tmp_path / "autopilot-state.json"
    workqueue.save(queue_path, [_item(id=1, tier=2)])
    runner = _runner_queue([Outcome(status="plan_limit", resets_at=5000.0)])
    result = step(queue_path, state_path, runner, now=1000.0, decision=IDLE)
    assert result["status"] == "plan_limit"
    assert workqueue.load(queue_path)[0]["state"] == "todo"  # not stranded as "doing"
    assert State.load(state_path).claude_paused_until == 5000.0


def test_tier2_runs_on_claude_again_once_the_reset_passes(tmp_path: Path) -> None:
    queue_path, state_path = tmp_path / "queue.json", tmp_path / "autopilot-state.json"
    workqueue.save(queue_path, [_item(id=1, tier=2)])
    State(claude_paused_until=1000.0, reason="plan limit").save(state_path)
    result = step(queue_path, state_path, _runner_queue([Outcome(status="success")]), now=2000.0, decision=IDLE)
    assert result["action"] == "ran" and result["backend"] == "claude"


# --------------------------------------------------------------------------- loop()


def test_loop_sleeps_until_the_reset_in_chunks_then_resumes(tmp_path: Path) -> None:
    queue_path, state_path = tmp_path / "queue.json", tmp_path / "autopilot-state.json"
    workqueue.save(queue_path, [_item(id=1, tier=2)])
    State(claude_paused_until=600.0, reason="plan limit").save(state_path)
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    results = loop(
        queue_path, state_path, _runner_queue([Outcome(status="success")]),
        sleep=sleep, now_fn=lambda: now[0], decision_fn=lambda: IDLE, log_path=tmp_path / "log.md",
    )  # fmt: skip
    assert len(results) == 1 and results[0]["status"] == "success"
    assert slept and sum(slept) >= 600.0
    assert all(s <= 300.0 for s in slept)  # never sleeps more than 5-minute chunks


def test_loop_waits_for_the_real_reset_after_a_plan_limit_then_reruns_the_item(tmp_path: Path) -> None:
    queue_path, state_path = tmp_path / "queue.json", tmp_path / "autopilot-state.json"
    workqueue.save(queue_path, [_item(id=1, tier=2)])
    now = [1_700_000_000.0]
    reset = now[0] + 3 * 3600  # three hours away: well past the 60-minute default
    ran_at: list[float] = []
    outcomes = iter([Outcome(status="plan_limit", resets_at=reset), Outcome(status="success")])

    def runner(item: dict, backend: str) -> Outcome:
        ran_at.append(now[0])
        return next(outcomes)

    def sleep(s: float) -> None:
        now[0] += s

    results = loop(
        queue_path, state_path, runner,
        sleep=sleep, now_fn=lambda: now[0], decision_fn=lambda: IDLE, log_path=tmp_path / "log.md",
    )  # fmt: skip
    assert [r["status"] for r in results] == ["plan_limit", "success"]
    assert len(ran_at) == 2 and ran_at[1] >= reset  # not retried before the plan reset
    item = workqueue.load(queue_path)[0]
    assert item["state"] == "done" and item["attempts_claude"] == 0


def test_real_runner_passes_the_plan_reset_time_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import swarm.pipeline
    import swarm.priority
    from swarm.autopilot import real_runner
    from swarm.report import RunSummary

    class FakePipeline:
        def __init__(self, *args: object) -> None:
            pass

        async def run(self, task: str, project: Path) -> RunSummary:
            return RunSummary(
                status="plan_limit", task=task, project_dir=project, run_dir=project / "run",
                notes=["the five_hour plan limit is reached"], plan_resets_at=1_700_000_000.0,
            )  # fmt: skip

    monkeypatch.setattr(swarm.pipeline, "Pipeline", FakePipeline)
    monkeypatch.setattr(swarm.priority, "lower_priority", lambda **kw: None)
    outcome = real_runner(_item(tier=2, project=str(tmp_path)), "claude")
    assert outcome.status == "plan_limit" and outcome.resets_at == 1_700_000_000.0
    assert "five_hour" in outcome.note


def test_loop_stops_at_max_items(tmp_path: Path) -> None:
    queue_path, state_path = tmp_path / "queue.json", tmp_path / "autopilot-state.json"
    workqueue.save(queue_path, [_item(id=1, tier=1), _item(id=2, tier=1)])
    runner = _runner_queue([Outcome(status="success"), Outcome(status="success")])
    results = loop(
        queue_path, state_path, runner, max_items=1, now_fn=lambda: 0.0, decision_fn=lambda: IDLE,
        log_path=tmp_path / "log.md",
    )  # fmt: skip
    assert len(results) == 1


def test_loop_logs_transitions_under_an_autopilot_heading(tmp_path: Path) -> None:
    queue_path, state_path = tmp_path / "queue.json", tmp_path / "autopilot-state.json"
    workqueue.save(queue_path, [_item(id=1, tier=1)])
    log_path = tmp_path / "log.md"
    loop(
        queue_path, state_path, _runner_queue([Outcome(status="success")]),
        now_fn=lambda: 0.0, decision_fn=lambda: IDLE, log_path=log_path,
    )  # fmt: skip
    text = log_path.read_text(encoding="utf-8")
    assert "autopilot" in text and "item 1" in text


# --------------------------------------------------------------------------- dry run


def test_dry_run_names_a_backend_per_item_and_starts_nothing(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    items = [_item(id=1, tier=1), _item(id=2, tier=3), _item(id=3, tier=2, state="done")]
    workqueue.save(queue_path, items)
    lines = dry_run_lines(workqueue.load(queue_path), State(), IDLE, now=0.0)
    assert any("#1" in line and "local" in line for line in lines)
    assert any("#2" in line and "wait" in line for line in lines)  # tier 3: listed (todo) but never run
    assert not any("#3" in line for line in lines)  # done items are not listed
    assert workqueue.load(queue_path) == items  # nothing was started or changed


# --------------------------------------------------------------------------- local-only runs (no Claude tokens)


def test_local_only_never_chooses_claude() -> None:
    state = State()  # Claude is not paused: local-only must hold it back anyway
    assert choose(_item(tier=2), state, IDLE, 0.0, local_only=True)[0] == "wait"
    assert choose(_item(tier=2, local_fallback=True), state, IDLE, 0.0, local_only=True)[0] == "local"
    assert choose(_item(tier=1), state, IDLE, 0.0, local_only=True)[0] == "local"
    # a tier 1 item the local model failed twice would escalate to Claude: local-only makes it wait instead
    assert choose(_item(tier=1, attempts_local=2), state, IDLE, 0.0, local_only=True)[0] == "wait"
    assert choose(_item(tier=1, attempts_local=2), state, IDLE, 0.0)[0] == "claude"


def test_escalated_tier1_waits_while_claude_is_paused() -> None:
    state = State(claude_paused_until=100.0, reason="plan limit")
    backend, why = choose(_item(tier=1, attempts_local=2), state, IDLE, 50.0)
    assert backend == "wait"
    assert "plan limit" in why


def test_local_only_step_skips_claude_items_and_runs_the_local_one(tmp_path: Path) -> None:
    queue, state_path = tmp_path / "queue.json", tmp_path / "autopilot-state.json"
    workqueue.save(queue, [_item(id=1, tier=2), _item(id=2, tier=1)])
    seen: list[tuple[int, str]] = []

    def runner(item: dict, backend: str) -> Outcome:
        seen.append((item["id"], backend))
        return Outcome(status="success")

    result = step(queue, state_path, runner, 0.0, IDLE, local_only=True)
    assert (result["action"], result["item"], result["backend"]) == ("ran", 2, "local")
    assert seen == [(2, "local")]
    assert [i["state"] for i in workqueue.load(queue)] == ["todo", "done"]


def test_local_only_loop_stops_instead_of_waiting_for_claude(tmp_path: Path) -> None:
    queue, state_path = tmp_path / "queue.json", tmp_path / "autopilot-state.json"
    workqueue.save(queue, [_item(id=1, tier=2), _item(id=2, tier=1)])
    slept: list[float] = []
    results = loop(
        queue, state_path, _runner_queue([Outcome(status="success")]),
        sleep=slept.append, now_fn=lambda: 0.0, decision_fn=lambda: IDLE,
        log_path=tmp_path / "log.md", local_only=True,
    )  # fmt: skip
    assert [r["item"] for r in results] == [2]
    assert slept == []  # never sleeps toward a reset it will not use
    assert "needs Claude" in (tmp_path / "log.md").read_text(encoding="utf-8")
    assert workqueue.load(queue)[0]["state"] == "todo"  # the Claude item is left queued, untouched


def test_local_only_dry_run_says_claude_is_not_used() -> None:
    lines = dry_run_lines([_item(id=1, tier=2), _item(id=2, tier=1)], State(), IDLE, 0.0, local_only=True)
    assert lines[0] == "Claude: not used (local-only run)"
    assert "wait" in lines[1] and "local" in lines[2]
