"""Work the queue unattended: as few Claude-plan tokens as possible, hand over to the local model when the
plan is nearly used up, take Claude work back up after the plan window resets.

The lead session hands `queue.json` to `swarm autopilot`. This module is the policy: `choose()` decides,
per item, whether it runs local, on Claude, or has to wait; `run_item()`/`record()` turn one run's outcome
into queue and pause-state updates; `step()` does one iteration; `loop()` repeats it until the queue is
drained, a cap is hit, or (while only Claude work is left and Claude is paused) the plan resets.

`runner` is always injected. In tests it is a fake that never starts a real run. Outside tests `real_runner`
invokes the same in-process run path `swarm run` uses (never `--commit`, research off for local runs), at
low OS priority, and never touches anything outside the item's own project folder.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from swarm import load as load_mod
from swarm import workqueue
from swarm.load import Decision, route

PLAN_LIMIT_DEFAULT_PAUSE_S = 60 * 60.0  # unknown reset time: pause an hour and say so
PLAN_LIMIT_MAX_PAUSE_S = 8 * 24 * 3600.0  # the longest plan window is seven days: a later reset is not believed
SLEEP_CHUNK_S = 5 * 60.0  # loop() never sleeps longer than this before re-checking
DEFAULT_LOG = Path("docs/autonomous-build-log.md")
STATE_FIELDS = ("claude_paused_until", "reason", "last_item", "updated_at", "current")


@dataclass
class State:
    """Persisted next to the queue (autopilot-state.json): whether Claude is paused, and why."""

    claude_paused_until: float | None = None
    reason: str = ""
    last_item: int | None = None
    updated_at: float = field(default_factory=time.time)
    # The item being run right now: {"item", "backend", "why", "started_at"}; None between items. Read by the
    # dashboard (ui/) to show which item is on which engine while the run is still going.
    current: dict[str, Any] | None = None

    @classmethod
    def load(cls, path: Path) -> State:
        path = Path(path)
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(**{k: data[k] for k in STATE_FIELDS if k in data})

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def claude_available(state: State, now: float) -> bool:
    """False while Claude is paused (a plan limit was hit and the reset has not passed yet)."""
    return state.claude_paused_until is None or now >= state.claude_paused_until


@dataclass
class Outcome:
    """What one run of an item produced."""

    status: str  # "success" | "needs_attention" | "plan_limit"
    report: str = ""
    resets_at: float | None = None  # plan_limit only; None = unknown
    note: str = ""


LOCAL_ONLY_WHY = "local-only run: no Claude-plan tokens are spent"


def choose(
    item: dict, state: State, decision: Decision, now: float, *, local_only: bool = False
) -> tuple[str, str]:
    """Which backend should run `item` right now: ("local" | "claude" | "wait", why).

    Tier 0 and tier 3 are not run by the autopilot. Tier 1 goes through `swarm.load.route` (so it moves to
    Claude once the local model has failed it twice, or the PC is busy and the item is urgent). Tier 2 uses
    Claude when it is available; while Claude is paused, a tier 2 item waits unless it is marked
    `"local_fallback": true`, in which case it runs on the local model instead.

    `local_only` treats Claude as unavailable for the whole run: nothing is ever sent to Claude, and an item
    that needs it waits for a later run.
    """
    tier = item["tier"]
    if tier in (0, 3):
        return "wait", f"tier {tier} is not run by the autopilot"
    available = claude_available(state, now) and not local_only
    if local_only:
        paused = LOCAL_ONLY_WHY
    else:
        paused = f"Claude is paused ({state.reason})" if state.reason else "Claude is paused"
    if tier == 1:
        r = route(1, decision, local_failures=item.get("attempts_local", 0), urgent=bool(item.get("urgent")))
        if r.backend == "claude" and not available:
            return "wait", f"{r.why}; {paused}"
        return r.backend, r.why
    # tier 2
    if available:
        return "claude", "Claude plan is available"
    if item.get("local_fallback"):
        return "local", f"{paused}; this item allows a local fallback"
    return "wait", paused


Runner = Callable[[dict, str], Any]


def run_item(item: dict, backend: str, runner: Runner) -> Outcome:
    """Call `runner(item, backend)` and normalize whatever it returns into an `Outcome`."""
    result = runner(item, backend)
    if isinstance(result, Outcome):
        return result
    return Outcome(
        status=result.get("status", "needs_attention"),
        report=result.get("report", ""),
        resets_at=result.get("resets_at"),
        note=result.get("note", ""),
    )


def _believable_reset(resets_at: Any, now: float) -> bool:
    """True for a reset time that is in the future and no further away than the longest plan window.

    A time already past would un-pause Claude at once and send the same item straight back into the limit.
    """
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
        return False
    return now < resets_at <= now + PLAN_LIMIT_MAX_PAUSE_S


def record(item: dict, state: State, backend: str, outcome: Outcome, now: float) -> dict:
    """Apply an `Outcome` to `item` (in place) and to `state` (in place). Returns the fields that changed.

    success -> done. plan_limit -> stays todo, is not counted as an attempt, pauses Claude until the reset
    (60 minutes, stated as such, if the reset time is unknown, already past or further off than any plan
    window). needs_attention -> counts an attempt on
    `backend`; a tier 1 item that has now failed twice locally goes back to `todo` so the next `choose()`
    call escalates it to Claude via `route`; any item blocked after 2 failed attempts on Claude is marked
    `blocked` for a human or the lead.
    """
    attempts_key = "attempts_claude" if backend == "claude" else "attempts_local"
    updates: dict[str, Any] = {}

    if outcome.status == "success":
        updates = {"state": "done", "note": outcome.note or f"done on {backend}", "report": outcome.report}
    elif outcome.status == "plan_limit":
        resets_at, reason = outcome.resets_at, outcome.note or "Claude plan limit reached"
        if not _believable_reset(resets_at, now):
            resets_at = now + PLAN_LIMIT_DEFAULT_PAUSE_S
            reason += " (reset time unknown: pausing 60 minutes)"
        state.claude_paused_until = resets_at
        state.reason = reason
        state.updated_at = now
        # back to todo: step() marked it doing, and it has to be picked up again after the reset
        updates = {
            "state": "todo",
            "note": f"plan limit hit; Claude paused until {time.strftime('%a %H:%M', time.localtime(resets_at))}",
        }
    elif outcome.status == "needs_attention":
        attempts = item.get(attempts_key, 0) + 1
        updates[attempts_key] = attempts
        if outcome.report:
            updates["report"] = outcome.report
        if attempts >= 2:
            if item["tier"] == 1 and backend == "local":
                updates["state"] = "todo"
                updates["note"] = f"local model failed twice; escalating to Claude. {outcome.note}".strip()
            else:
                updates["state"] = "blocked"
                updates["note"] = f"blocked after 2 failed attempts on {backend}: needs a human or the lead. " \
                                   f"{outcome.note}".strip()
        else:
            updates["state"] = "todo"
            updates["note"] = outcome.note or f"attempt {attempts} on {backend} needs attention"
    else:
        updates = {"note": outcome.note or outcome.status}

    item.update(updates)
    state.last_item = item["id"]
    return updates


def _log(path: Path, line: str) -> None:
    """Append one line to today's heading in the build log, and print it."""
    print(line)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    heading = time.strftime("## %Y-%m-%d") + " autopilot"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    with path.open("a", encoding="utf-8") as f:
        if heading not in existing:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"{heading}\n")
        f.write(f"- {time.strftime('%H:%M:%S')} {line}\n")


def step(
    queue_path: Path, state_path: Path, runner: Runner, now: float, decision: Decision, *, local_only: bool = False
) -> dict:
    """One iteration: load, pick the first `todo` item that can run right now, run it, record, save.

    An item that has to wait (it needs Claude and Claude is paused, or the run is `local_only`) does not hold
    up the items behind it. When every remaining item has to wait the result is "wait", naming the first one
    (so `loop()` sleeps toward the reset) rather than "idle" (which would make it stop for good).
    """
    items = workqueue.load(queue_path)
    state = State.load(state_path)
    available = claude_available(state, now) and not local_only
    item, backend, why = None, "wait", ""
    waiting: tuple[dict, str] | None = None
    for candidate in items:
        if candidate["state"] != "todo" or candidate["tier"] in (0, 3):
            continue
        backend, why = choose(candidate, state, decision, now, local_only=local_only)
        if backend != "wait":
            item = candidate
            break
        waiting = waiting or (candidate, why)
    if item is None:
        if waiting is not None:
            return {"action": "wait", "item": waiting[0]["id"], "why": waiting[1], "claude_available": available}
        return {"action": "idle", "claude_available": available}

    item["state"] = "doing"
    workqueue.save(queue_path, items)
    state.current = {"item": item["id"], "backend": backend, "why": why, "started_at": now}
    state.save(state_path)
    outcome = run_item(item, backend, runner)
    updates = record(item, state, backend, outcome, now)
    state.current = None
    workqueue.save(queue_path, items)
    state.save(state_path)
    return {
        "action": "ran", "item": item["id"], "backend": backend, "why": why,
        "status": outcome.status, "updates": updates,
    }  # fmt: skip


def loop(
    queue_path: Path,
    state_path: Path,
    runner: Runner,
    *,
    max_items: int | None = None,
    max_hours: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
    decision_fn: Callable[[], Decision] | None = None,
    log_path: Path | str = DEFAULT_LOG,
    local_only: bool = False,
) -> list[dict]:
    """Repeat `step` until nothing is runnable, `max_items` is reached, or `max_hours` has elapsed.

    When the only remaining work needs Claude and Claude is paused, sleeps toward the reset in chunks of at
    most `SLEEP_CHUNK_S`, re-checking each time, then continues automatically once it passes (the hand-back).
    A `local_only` run never waits for Claude: it stops as soon as the only work left needs it.
    Every transition is appended to `log_path` under today's date heading, and printed.
    """
    decision_fn = decision_fn or load_mod.current
    log_path = Path(log_path)
    start = now_fn()
    results: list[dict] = []
    while True:
        if max_items is not None and len(results) >= max_items:
            break
        if max_hours is not None and now_fn() - start >= max_hours * 3600:
            break
        now = now_fn()
        result = step(queue_path, state_path, runner, now, decision_fn(), local_only=local_only)
        if result["action"] == "idle":
            break
        if result["action"] == "wait":
            if local_only:
                _log(log_path, f"stopping: the work that is left needs Claude (first: item {result['item']})")
                break
            state = State.load(state_path)
            remaining = (state.claude_paused_until or now) - now
            if remaining <= 0:
                continue  # the reset just passed: try again immediately (the hand-back)
            chunk = min(SLEEP_CHUNK_S, remaining)
            _log(log_path, f"waiting {chunk / 60:.0f}m for the Claude plan to reset ({result['why']})")
            sleep(chunk)
            continue
        results.append(result)
        _log(
            log_path,
            f"item {result['item']}: ran on {result['backend']} -> {result['status']} ({result['why']})",
        )
    return results


def dry_run_lines(
    items: list[dict], state: State, decision: Decision, now: float, *, local_only: bool = False
) -> list[str]:
    """What `--dry-run` prints: the backend each `todo` item would use right now, and why, plus pause state."""
    if local_only:
        head = "Claude: not used (local-only run)"
    elif not claude_available(state, now):
        head = "Claude: paused" + (f" ({state.reason})" if state.reason else "")
    else:
        head = "Claude: available"
    lines = [head]
    for item in items:
        if item["state"] != "todo":
            continue
        backend, why = choose(item, state, decision, now, local_only=local_only)
        lines.append(f"  #{item['id']} (tier {item['tier']}): {backend} - {why}")
    return lines


def real_runner(item: dict, backend: str) -> Outcome:
    """The runner used outside tests: runs the item through the real in-process pipeline, the same path
    `swarm run` uses, never with `--commit`; research is off for local runs. Low OS priority throughout.
    Never touches anything outside `item["project"]`; relies on the existing guard for that.
    """
    import asyncio

    from swarm.config import load_config
    from swarm.priority import lower_priority
    from swarm.report import NullReporter
    from swarm.roles import load_roles
    from swarm.workspace import slugify

    lower_priority(idle=True)
    project = Path(item["project"]).expanduser().resolve() if item.get("project") else \
        load_config(None, None).workspace_dir / slugify(item["task"])  # fmt: skip
    cfg = load_config(project, None)
    cfg.commit = False
    if backend == "local":
        from swarm.local import DEFAULT_MODEL, LocalBackend

        cfg.research = False
        engine = LocalBackend(model=DEFAULT_MODEL)
    else:
        from swarm.backend import ClaudeBackend

        engine = ClaudeBackend(plan_stop_at=cfg.plan_stop_at)
    roles = load_roles(cfg.roles_dir)

    from swarm.pipeline import Pipeline

    pipeline = Pipeline(cfg, roles, engine, NullReporter())
    summary = asyncio.run(pipeline.run(item["task"], project))
    note = "; ".join(summary.notes[:2]) if summary.notes else ""
    return Outcome(
        status=summary.status, report=str(summary.run_dir / "report.md"), note=note,
        resets_at=summary.plan_resets_at,
    )  # fmt: skip
