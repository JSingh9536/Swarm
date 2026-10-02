from __future__ import annotations

import pytest

import swarm.hardware as hw
from swarm.hardware import (
    CpuStat,
    Readout,
    gpu_power_limits,
    gpu_stats,
    parse_nvidia_smi,
    read,
    render,
    reset_gpu_power_limit,
    set_gpu_power_limit,
)

SAMPLE = (
    "0, NVIDIA GeForce RTX 4070, 37, 2048, 12282, 115.4, 200.0, 61\n"
    "1, NVIDIA GeForce RTX 3060, 0, 300, 12288, [N/A], 170.0, 44\n"
)


def test_parse_nvidia_smi() -> None:
    gpus = parse_nvidia_smi(SAMPLE)
    assert len(gpus) == 2
    a = gpus[0]
    assert a.index == 0 and a.name == "NVIDIA GeForce RTX 4070"
    assert a.util_pct == 37 and a.mem_used_mb == 2048 and a.mem_total_mb == 12282
    assert a.power_w == 115.4 and a.power_limit_w == 200.0 and a.temp_c == 61
    assert gpus[1].power_w is None  # [N/A] parses to None, not a crash


def test_parse_handles_units_and_percent_and_blank() -> None:
    assert hw._to_float("37 %") == 37
    assert hw._to_float("115.4 W") == 115.4
    assert hw._to_float("[N/A]") is None
    assert hw._to_float("Not Supported") is None
    assert hw._to_float("") is None
    assert hw._to_float("junk") is None


def test_parse_skips_short_lines() -> None:
    assert parse_nvidia_smi("garbage\n0, GPU\n") == []
    assert parse_nvidia_smi("") == []


def test_total_gpu_power() -> None:
    r = Readout(cpu=CpuStat(), gpus=parse_nvidia_smi(SAMPLE))
    assert r.total_gpu_power_w == 115.4  # the [N/A] one is excluded
    assert Readout(cpu=CpuStat()).total_gpu_power_w is None


def test_gpu_stats_no_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw.shutil, "which", lambda name: None)
    assert gpu_stats() == []


def test_gpu_stats_parses_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=8.0: SAMPLE)
    gpus = gpu_stats()
    assert len(gpus) == 2 and gpus[0].util_pct == 37


def fake_run(limits: str = "150, 245, 100, 260", pl_result: str | None = "Power limit set"):
    def _run(cmd, timeout=8.0):  # type: ignore[no-untyped-def]
        if any("query-gpu=power.limit" in str(c) for c in cmd):
            return limits + "\n"
        return pl_result

    return _run


def test_gpu_power_limits_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw.shutil, "which", lambda name: "nvidia-smi")
    monkeypatch.setattr(hw, "_run", fake_run())
    assert gpu_power_limits() == {"current": 150, "default": 245, "min": 100, "max": 260}

    monkeypatch.setattr(hw.shutil, "which", lambda name: None)
    assert gpu_power_limits() is None


def test_set_gpu_power_limit_reports_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw.shutil, "which", lambda name: None)
    assert "no nvidia-smi" in set_gpu_power_limit(150)

    monkeypatch.setattr(hw.shutil, "which", lambda name: "nvidia-smi")
    monkeypatch.setattr(hw, "_run", fake_run())
    note = set_gpu_power_limit(200)
    assert "200W" in note and "Was 150W" in note and "restore with" in note and "machine-wide" in note


def test_set_gpu_power_limit_rejects_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw.shutil, "which", lambda name: "nvidia-smi")
    monkeypatch.setattr(hw, "_run", fake_run())  # range 100-260
    assert "refused" in set_gpu_power_limit(300)
    assert "refused" in set_gpu_power_limit(50)


def test_set_gpu_power_limit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw.shutil, "which", lambda name: "nvidia-smi")
    monkeypatch.setattr(hw, "_run", fake_run(pl_result=None))  # the -pl call fails (no admin)
    assert "could not set" in set_gpu_power_limit(150)


def test_reset_gpu_power_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw.shutil, "which", lambda name: "nvidia-smi")
    monkeypatch.setattr(hw, "_run", fake_run())
    assert "245W" in reset_gpu_power_limit()  # restores the default (245)


def test_read_degrades_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    # no GPU, no psutil
    monkeypatch.setattr(hw, "gpu_stats", lambda: [])
    monkeypatch.setattr(hw, "cpu_stats", lambda: (CpuStat(percent=None, logical=8), None, None))
    r = read(priority="below-normal")
    assert r.priority == "below-normal"
    assert any("psutil" in n for n in r.notes)
    assert any("NVIDIA" in n for n in r.notes)
    lines = render(r)
    assert any(line.startswith("CPU") for line in lines)
    assert all("n/a" in line or "CPU" in line or "priority" in line for line in lines[:1])


def test_render_full() -> None:
    r = Readout(
        cpu=CpuStat(percent=42.5, logical=16, physical=8, freq_mhz=3800, load1=None),
        gpus=parse_nvidia_smi(SAMPLE),
        mem_used_mb=18000,
        mem_total_mb=32000,
        priority="idle",
    )
    text = "\n".join(render(r))
    assert "CPU" in text and "42" in text and "8c/16t" in text and "[priority: idle]" in text
    assert "RAM" in text and "18.0/32.0 GB" in text
    assert "RTX 4070" in text and "115W/200W" in text and "61C" in text
    assert "GPU power draw: 115 W total" in text


def test_render_no_gpu_no_mem() -> None:
    lines = render(Readout(cpu=CpuStat(percent=10, logical=4)))
    assert len(lines) == 1 and lines[0].startswith("CPU")
