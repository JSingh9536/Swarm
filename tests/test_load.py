"""PC-load awareness: pure decision and routing logic, no hardware touched."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm import load, usage
from swarm.load import Signals, classify_processes, decide, route


def test_idle_machine_runs_full() -> None:
    d = decide(Signals(gpu_util_pct=4, vram_free_mb=6500, cpu_pct=8))
    assert d.mode == "full" and not d.busy and d.ollama_options() == {}


def test_game_keeps_the_model_off_the_gpu() -> None:
    d = decide(Signals(gpu_util_pct=80, vram_free_mb=1500, cpu_pct=30, games=["eldenring.exe"]))
    assert d.mode == "cpu_only"
    assert d.ollama_options() == {"num_gpu": 0, "num_thread": load.BACKGROUND_THREADS}
    assert any("game running" in r for r in d.reasons)


def test_call_keeps_gpu_but_goes_quiet() -> None:
    d = decide(Signals(gpu_util_pct=10, vram_free_mb=6500, cpu_pct=25, calls=["cpthost.exe"]))
    assert d.mode == "background" and "num_gpu" not in d.ollama_options()
    assert d.ollama_options()["num_thread"] == load.BACKGROUND_THREADS


def test_saturated_machine_defers() -> None:
    assert decide(Signals(cpu_pct=95)).mode == "defer"
    assert decide(Signals(gpu_util_pct=85, cpu_pct=70, games=["game.exe"])).mode == "defer"


def test_our_own_loaded_model_is_not_mistaken_for_low_video_memory() -> None:
    assert decide(Signals(gpu_util_pct=3, vram_free_mb=900, cpu_pct=5, model_loaded=True)).mode == "full"
    assert decide(Signals(gpu_util_pct=3, vram_free_mb=900, cpu_pct=5, model_loaded=False)).mode == "cpu_only"


def test_missing_signals_never_count_as_busy() -> None:
    assert decide(Signals()).mode == "full"


def test_an_open_conferencing_app_is_only_a_hint() -> None:
    d = decide(Signals(cpu_pct=5, call_hints=["zoom.exe"]))
    assert d.mode == "full" and any("no call detected" in r for r in d.reasons)


def test_classify_processes() -> None:
    gpu = [
        r"C:\Program Files (x86)\Steam\steamapps\common\wallpaper_engine\wallpaper32.exe",
        r"E:\SteamLibrary\steamapps\common\ELDEN RING\Game\eldenring.exe",
        r"E:\BDO\bin64\BlackDesert64.exe",
        r"C:\Windows\explorer.exe",
        r"C:\Users\x\AppData\Local\Programs\Ollama\ollama.exe",
    ]
    games, calls, hints = classify_processes(gpu, ["Zoom.exe", "CptHost.exe", "chrome.exe"])
    assert games == ["eldenring.exe", "blackdesert64.exe"]  # wallpaper engine, explorer and ollama are not games
    assert calls == ["cpthost.exe"] and hints == ["zoom.exe"]


# --------------------------------------------------------------------------- routing: when Claude tokens are justified


IDLE = decide(Signals(cpu_pct=5))
GAMING = decide(Signals(gpu_util_pct=80, cpu_pct=30, games=["game.exe"]))


def test_route_never_uses_a_model_for_tier_zero() -> None:
    assert route(0, IDLE).backend == "none"


def test_route_small_tasks_stay_local_even_when_the_pc_is_busy() -> None:
    assert route(1, IDLE).backend == "local" and route(1, IDLE).how == "full"
    busy = route(1, GAMING)
    assert busy.backend == "local" and busy.how == "cpu_only"  # busy alone never spends tokens


def test_route_spends_tokens_only_with_a_reason() -> None:
    assert route(2, IDLE).how == "needs-claude"
    assert route(1, IDLE, local_failures=2).how == "escalated"
    assert route(1, IDLE, local_failures=1).backend == "local"
    assert route(1, GAMING, urgent=True).how == "busy-and-urgent"
    assert route(1, IDLE, urgent=True).backend == "local"  # urgent but the GPU is free: still local
    assert route(1, IDLE, local_available=False).how == "no-local-model"


# --------------------------------------------------------------------------- prepare(): acting on the decision


def test_prepare_waits_out_a_saturated_machine_then_runs_on_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    states = iter([decide(Signals(cpu_pct=95)), decide(Signals(cpu_pct=95)), decide(Signals(cpu_pct=10))])
    slept: list[float] = []
    priorities: list[bool] = []
    monkeypatch.setattr(load, "current", lambda max_age_s=load.CACHE_S: next(states))
    monkeypatch.setattr(load, "set_ollama_priority", lambda idle: priorities.append(idle) or 0)
    monkeypatch.setattr(load, "unload_model", lambda: None)
    d = load.prepare(sleep=slept.append)
    assert d.mode == "full" and slept == [load.DEFER_POLL_S] * 2 and priorities == [False]


def test_prepare_gives_up_waiting_and_uses_spare_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(load, "DEFER_MAX_S", 60.0)
    monkeypatch.setattr(load, "current", lambda max_age_s=load.CACHE_S: decide(Signals(cpu_pct=95, model_loaded=True)))
    unloaded: list[int] = []
    priorities: list[bool] = []
    monkeypatch.setattr(load, "set_ollama_priority", lambda idle: priorities.append(idle) or 0)
    monkeypatch.setattr(load, "unload_model", lambda: unloaded.append(1))
    slept: list[float] = []
    d = load.prepare(sleep=slept.append)
    assert d.mode == "defer" and sum(slept) == 60.0  # bounded wait, never forever
    assert unloaded == [1] and priorities == [True]  # video memory freed, server at idle priority
    assert d.ollama_options()["num_gpu"] == 0


def test_render_explains_mode_and_routes() -> None:
    text = "\n".join(load.render(GAMING))
    assert "mode: cpu_only" in text and "game running" in text and "route tier 1" in text


# --------------------------------------------------------------------------- usage ledger


def test_local_ledger_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setenv(usage.LEDGER_ENV, str(ledger))
    usage.record_local({"ok": True, "prompt_tokens": 1000, "output_tokens": 200, "seconds": 12, "gen_seconds": 10,
                        "gpu_w": 180.0, "mode": "full"})  # fmt: skip
    usage.record_local({"ok": False, "prompt_tokens": 500, "output_tokens": 50, "seconds": 30, "gen_seconds": 20,
                        "gpu_w": 0.0, "mode": "cpu_only"})  # fmt: skip
    ledger.open("a", encoding="utf-8").write("not json\n")
    s = usage.local_summary()
    assert s["calls"] == 2 and s["ok"] == 1 and s["tokens_in"] == 1500 and s["tokens_out"] == 250
    assert s["modes"] == {"full": 1, "cpu_only": 1}
    assert s["kwh"] == pytest.approx(180.0 * 10 / 3600 / 1000, abs=1e-4)
    assert len(s["by_day"]) == 1


def test_claude_summary_counts_each_message_once(tmp_path: Path) -> None:
    proj = tmp_path / "D--Code-x"
    (proj / "sess" / "subagents").mkdir(parents=True)
    now = "2099-01-01T00:00:00Z"

    def line(mid: str, out: int) -> str:
        usage_block = {"input_tokens": 3, "output_tokens": out, "cache_read_input_tokens": 100,
                       "cache_creation_input_tokens": 10}  # fmt: skip
        return json.dumps({"timestamp": now, "message": {"id": mid, "model": "claude-x", "usage": usage_block}})

    # the same message id is logged once per content block; early lines carry a partial output count,
    # so it must count once, at its largest (final) value
    (proj / "main.jsonl").write_text("\n".join([line("m1", 4), line("m1", 50), line("m2", 70), "{bad"]), "utf-8")
    (proj / "sess" / "subagents" / "a.jsonl").write_text(line("s1", 30), "utf-8")
    s = usage.claude_summary(projects_dir=tmp_path, since_days=36500)
    assert s["total"]["output"] == 150 and s["total"]["messages"] == 3
    assert s["total"]["subagent_output"] == 30
    assert s["by_project"][0]["project"] == "D--Code-x" and s["by_model"][0]["model"] == "claude-x"
