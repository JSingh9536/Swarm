"""Offline tests: the neuron model, the event tail, the replay clock, and a short GUI smoke run."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from swarm_desk.app import bezier, blend
from swarm_desk.brain import REST_ACTIVE, Brain
from swarm_desk.feed import MAX_GAP, MIN_GAP, Player, RunRef, Tail, folder, is_live, newest_since

T0 = 1_800_000_000.0
DEMO = [  # the shape `swarm demo --pace` writes: build, gate, review asks for a fix, developer fixes, gate again
    {"kind": "phase", "name": "implement", "detail": ""},
    {"kind": "agent_start", "role": "developer", "label": "W1 slugify", "model": "sonnet"},
    {"kind": "activity", "role": "developer", "what": "tool", "text": "Write slugify.py", "blocked": False},
    {"kind": "activity", "role": "developer", "what": "tool", "text": "Bash rm -rf /", "blocked": True},
    {"kind": "agent_end", "role": "developer", "ok": True, "cost": 0.12, "turns": 5, "seconds": 4.0, "denied": 1},
    {"kind": "gate", "label": "after W1", "state": "PASS"},
    {"kind": "phase", "name": "review", "detail": "(round 1)"},
    {"kind": "agent_start", "role": "reviewer", "label": "review, round 1", "model": "sonnet"},
    {"kind": "agent_end", "role": "reviewer", "ok": True, "cost": 0.05, "turns": 3, "seconds": 2.0},
    {"kind": "agent_start", "role": "developer", "label": "fix 1 finding(s)", "model": "sonnet"},
    {"kind": "agent_end", "role": "developer", "ok": True, "cost": 0.03, "turns": 3, "seconds": 1.0},
    {"kind": "gate", "label": "round 2", "state": "FAIL"},
]
for i, ev in enumerate(DEMO):
    ev["t"] = T0 + i * 2.0


def run_folder(root: Path, project: str = "proj", run: str = "20261029-120000") -> Path:
    path = root / project / ".swarm" / "runs" / run
    path.mkdir(parents=True)
    return path


# ------------------------------------------------------------------ brain


def test_brain_counts_calls_tools_cost_and_gates() -> None:
    b = Brain()
    for ev in DEMO:
        b.apply(ev)
    dev = b.neurons["developer"]
    assert (dev.fires, dev.tools, dev.blocked, dev.turns) == (2, 2, 1, 8)
    assert dev.cost == pytest.approx(0.15) and b.total_cost == pytest.approx(0.20)
    assert b.calls == 3 and b.gates == [("after W1", "PASS"), ("round 2", "FAIL")]
    assert b.neurons["gate"].state == "failed" and b.neurons["gate"].failures == 1
    assert b.phase == "review" and b.elapsed == pytest.approx(22.0)
    assert any(line.tone == "bad" and "rm -rf" in line.text for line in b.feed)


def test_synapses_learn_real_handoffs_including_the_feedback_loop() -> None:
    b = Brain()
    for ev in DEMO:
        b.apply(ev)
    weights = {k: s.weight for k, s in b.synapses.items()}
    # review sent the work back to the developer: a backward synapse; developer -> gate fired twice
    assert weights == {("developer", "gate"): 2, ("gate", "reviewer"): 1, ("reviewer", "developer"): 1}
    kinds = [fx.kind for fx in b.take_effects()]
    assert kinds.count("pulse") == 4 and "spark" in kinds and "shock" in kinds
    assert b.take_effects() == []


def test_potential_spikes_then_settles_to_working_level_and_decays_when_done() -> None:
    b = Brain()
    b.apply(DEMO[1])
    dev = b.neurons["developer"]
    assert dev.potential == 1.0 and dev.state == "active"
    for _ in range(100):
        b.tick(0.2)
    assert dev.potential == pytest.approx(REST_ACTIVE, abs=0.01)
    b.apply(DEMO[4])
    for _ in range(50):
        b.tick(0.2)
    assert dev.potential < 0.01 and len(dev.history) > 100


def test_a_new_start_ends_a_stale_active_agent_and_unknown_roles_get_a_neuron() -> None:
    b = Brain()
    b.apply({"kind": "agent_start", "role": "developer", "label": "x"})
    b.apply({"kind": "agent_start", "role": "docs-writer", "label": "y"})
    assert b.neurons["developer"].state == "done" and b.active is b.neurons["docs-writer"]
    assert "reverse-engineer" not in b.neurons, "optional roles appear only when they work"


def test_brain_ignores_junk_records() -> None:
    b = Brain()
    for junk in ({}, {"kind": "agent_start"}, {"kind": "mystery", "role": "x"}, {"kind": "gate", "t": "soon"}):
        b.apply(junk)
    assert b.calls == 0 and b.elapsed == 0.0


# ------------------------------------------------------------------ tail & player


def test_tail_reads_only_new_complete_lines(tmp_path: Path) -> None:
    path = run_folder(tmp_path)
    events = path / "events.jsonl"
    events.write_text(json.dumps(DEMO[0]) + "\n" + '{"kind": "agent_st', encoding="utf-8")
    tail = Tail(path)
    assert [e["kind"] for e in tail.read_new()] == ["phase"]
    with events.open("a", encoding="utf-8") as fh:
        fh.write('art", "role": "developer"}\nnot json\n')
    assert tail.read_new() == [{"kind": "agent_start", "role": "developer"}]
    assert tail.read_new() == []


def test_tail_rebuilds_old_runs_from_progress_once(tmp_path: Path) -> None:
    path = run_folder(tmp_path)
    (path / "progress.md").write_text("- 10:00:00 PHASE plan\n- 10:00:05 architect [plan] ok ($0.10)\n", "utf-8")
    tail = Tail(path)
    assert [e["kind"] for e in tail.read_new()] == ["phase", "agent_start", "agent_end"] and tail.synthetic
    assert tail.read_new() == []


def test_player_compresses_gaps_and_honours_speed() -> None:
    evs = [{"t": 0.0}, {"t": 0.0}, {"t": 100.0}]
    p = Player(evs, speed=1.0)
    assert len(p.advance(0.0)) == 1  # the first event plays at once
    assert p.advance(MIN_GAP / 2) == [] and len(p.advance(MIN_GAP)) == 1  # instant handoffs still get a beat
    assert p.advance(MAX_GAP - 0.1) == [] and len(p.advance(0.2)) == 1 and p.done  # a 100s wait becomes MAX_GAP
    p = Player(evs, speed=8.0)
    assert len(p.advance(MAX_GAP / 4)) == 3


def test_player_pause_seek_and_catch_up() -> None:
    p = Player(DEMO)
    p.paused = True
    assert p.advance(10) == []
    assert len(p.seek(5)) == 5 and p.index == 5
    assert len(p.seek(999)) == len(DEMO) and p.done
    p.seek(2)
    p.extend([{"kind": "phase", "name": "fix"}])
    assert len(p.catch_up()) == len(DEMO) - 1 and p.done


def test_run_lookup_stays_inside_the_workspace(tmp_path: Path) -> None:
    path = run_folder(tmp_path)
    (path / "progress.md").write_text("- 10:00:00 PHASE plan\n", "utf-8")
    assert folder(tmp_path, RunRef("proj", "20261029-120000")) == path
    assert folder(tmp_path, RunRef("..", "20261029-120000")) is None
    assert folder(tmp_path, RunRef("proj", "../../etc")) is None
    assert newest_since(tmp_path, time.time() - 60) == RunRef("proj", "20261029-120000")
    assert newest_since(tmp_path, time.time() + 600) is None
    assert is_live(path)
    (path / "summary.json").write_text("{}", "utf-8")
    assert not is_live(path)


# ------------------------------------------------------------------ drawing helpers & GUI


def test_blend_and_bezier() -> None:
    assert blend("#ffffff", "#000000", 0.5) == "#808080"
    assert blend("#ff0000", "#000000", 2) == "#ff0000" and blend("#ff0000", "#000000", -1) == "#000000"
    pts = bezier([(0, 0), (5, 0), (5, 10), (10, 10)], steps=4)
    assert pts[:2] == [0, 0] and pts[-2:] == [10, 10] and len(pts) == 10


def test_window_replays_a_run(tmp_path: Path) -> None:
    """Open the real window on a recorded run for a moment (skipped where there is no display)."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    from swarm_desk.app import Desk

    path = run_folder(tmp_path / "ws")
    (path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in DEMO), "utf-8")
    (path / "summary.json").write_text('{"status": "failed"}', "utf-8")
    desk = Desk(root, Path(__file__).resolve().parents[2], tmp_path / "ws", tmp_path / "data")
    try:
        desk.open(RunRef("proj", "20261029-120000"))
        assert desk.mode == "replay" and len(desk.player.events) == len(DEMO)
        desk._set_speed(8.0)
        end = time.monotonic() + 4
        while not desk.player.done and time.monotonic() < end:
            root.update()
        root.update()
        assert desk.player.done and desk.brain.calls == 3
        assert desk.canvas.find_withtag("dyn"), "the neuron field drew nothing"
        assert desk.runs.exists("proj/20261029-120000")
        desk.seek(3)
        assert desk.brain.count == 3 and desk.mode == "replay"
    finally:
        root.destroy()
