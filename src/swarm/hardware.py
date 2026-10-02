"""Hardware readouts and power controls for running swarm on the local machine.

Gives a live view of what a run is costing in *watts and heat* (as opposed to Claude-plan usage), and the
levers to cap it: process priority (CPU) and, on NVIDIA, a GPU power limit. Everything degrades gracefully —
no GPU, no `nvidia-smi`, no `psutil` all just mean the matching numbers read "n/a" rather than an error.

Pure parsing/formatting is separated from the subprocess calls so it unit-tests without any hardware.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field


@dataclass
class GpuStat:
    index: int
    name: str
    util_pct: float | None = None
    mem_used_mb: float | None = None
    mem_total_mb: float | None = None
    power_w: float | None = None
    power_limit_w: float | None = None
    temp_c: float | None = None


@dataclass
class CpuStat:
    percent: float | None = None
    logical: int = 0
    physical: int | None = None
    freq_mhz: float | None = None
    load1: float | None = None  # POSIX 1-min load average; None on Windows


@dataclass
class Readout:
    cpu: CpuStat
    gpus: list[GpuStat] = field(default_factory=list)
    mem_used_mb: float | None = None
    mem_total_mb: float | None = None
    priority: str = "normal"
    notes: list[str] = field(default_factory=list)

    @property
    def total_gpu_power_w(self) -> float | None:
        powers = [g.power_w for g in self.gpus if g.power_w is not None]
        return round(sum(powers), 1) if powers else None


# --------------------------------------------------------------------------- GPU (nvidia-smi)

_GPU_FIELDS = (
    "index,name,utilization.gpu,memory.used,memory.total,power.draw,power.limit,temperature.gpu"
)


def _to_float(token: str) -> float | None:
    token = token.strip()
    if not token or token.lower() in ("n/a", "[n/a]", "[not supported]", "not supported"):
        return None
    # strip a trailing unit if nvidia ever includes one (it does not with nounits, but be safe)
    head = token.split()[0].rstrip("%")
    try:
        return float(head)
    except ValueError:
        return None


def parse_nvidia_smi(csv_text: str) -> list[GpuStat]:
    """Parse `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` output."""
    gpus: list[GpuStat] = []
    for line in csv_text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 8:
            continue
        idx = _to_float(parts[0])
        gpus.append(
            GpuStat(
                index=int(idx) if idx is not None else len(gpus),
                name=parts[1] or "GPU",
                util_pct=_to_float(parts[2]),
                mem_used_mb=_to_float(parts[3]),
                mem_total_mb=_to_float(parts[4]),
                power_w=_to_float(parts[5]),
                power_limit_w=_to_float(parts[6]),
                temp_c=_to_float(parts[7]),
            )
        )
    return gpus


def _run(cmd: list[str], timeout: float = 8.0) -> str | None:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argument lists built in this module
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def gpu_stats() -> list[GpuStat]:
    if not shutil.which("nvidia-smi"):
        return []
    out = _run([
        "nvidia-smi", f"--query-gpu={_GPU_FIELDS}", "--format=csv,noheader,nounits",
    ])  # fmt: skip
    return parse_nvidia_smi(out) if out else []


def gpu_power_limits() -> dict[str, float | None] | None:
    """The first GPU's current / default / min / max power limit in watts, or None if unavailable."""
    if not shutil.which("nvidia-smi"):
        return None
    out = _run([
        "nvidia-smi",
        "--query-gpu=power.limit,power.default_limit,power.min_limit,power.max_limit",
        "--format=csv,noheader,nounits",
    ])  # fmt: skip
    if not out or not out.strip():
        return None
    parts = [_to_float(p) for p in out.strip().splitlines()[0].split(",")]
    if len(parts) < 4:
        return None
    return {"current": parts[0], "default": parts[1], "min": parts[2], "max": parts[3]}


def set_gpu_power_limit(watts: int) -> str:
    """Set the NVIDIA GPU power cap. This is a MACHINE-WIDE change (every app, until reboot/reset) and
    usually needs admin. Rejects out-of-range values and reports how to restore the previous limit."""
    if not shutil.which("nvidia-smi"):
        return "no nvidia-smi; GPU power limit not available"
    limits = gpu_power_limits()
    lo, hi = (limits or {}).get("min"), (limits or {}).get("max")
    if lo is not None and hi is not None and not (lo <= watts <= hi):
        return f"refused: {watts}W is outside this GPU's supported range {lo:.0f}-{hi:.0f}W"
    prev = (limits or {}).get("current")
    out = _run(["nvidia-smi", "-pl", str(watts)], timeout=15)
    if out is None:
        return f"could not set GPU power limit to {watts}W (needs admin / persistence mode)"
    note = f"GPU power limit set to {watts}W (machine-wide, until reboot)."
    if prev is not None:
        note += f" Was {prev:.0f}W — restore with: swarm hardware --gpu-power-limit {prev:.0f}"
    return note


def reset_gpu_power_limit() -> str:
    """Restore the GPU's factory-default power limit."""
    limits = gpu_power_limits()
    default = (limits or {}).get("default")
    if default is None:
        return "could not read the GPU's default power limit"
    return set_gpu_power_limit(int(round(default)))


# --------------------------------------------------------------------------- CPU / memory


def cpu_stats() -> tuple[CpuStat, float | None, float | None]:
    """CPU stat plus (mem_used_mb, mem_total_mb). Uses psutil when present, else degrades."""
    logical = os.cpu_count() or 0
    load1: float | None = None
    try:
        load1 = os.getloadavg()[0]  # POSIX only
    except (OSError, AttributeError):
        load1 = None

    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        freq = psutil.cpu_freq()
        return (
            CpuStat(
                percent=psutil.cpu_percent(interval=0.2),
                logical=logical,
                physical=psutil.cpu_count(logical=False),
                freq_mhz=round(freq.current, 0) if freq else None,
                load1=load1,
            ),
            round(vm.used / 1e6, 0),
            round(vm.total / 1e6, 0),
        )
    except Exception:  # noqa: BLE001 - psutil missing or unavailable; fall back to a minimal reading
        return CpuStat(percent=None, logical=logical, physical=None, freq_mhz=None, load1=load1), None, None


# --------------------------------------------------------------------------- assemble + render


def read(priority: str = "normal") -> Readout:
    cpu, mem_used, mem_total = cpu_stats()
    gpus = gpu_stats()
    notes: list[str] = []
    if cpu.percent is None:
        notes.append("install psutil for live CPU% and memory (pip install psutil)")
    if not gpus:
        notes.append("no NVIDIA GPU readout (nvidia-smi not found or no NVIDIA GPU)")
    return Readout(cpu=cpu, gpus=gpus, mem_used_mb=mem_used, mem_total_mb=mem_total, priority=priority, notes=notes)


def _fmt(value: float | None, unit: str = "", nd: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{nd}f}{unit}"


def render(r: Readout) -> list[str]:
    """Plain lines describing the current draw — used by `swarm hardware` and during runs."""
    lines: list[str] = []
    cpu_pct = _fmt(r.cpu.percent, "%")
    cores = f"{r.cpu.physical or '?'}c/{r.cpu.logical}t"
    freq = _fmt(r.cpu.freq_mhz, " MHz")
    load = f"  load {r.cpu.load1:.2f}" if r.cpu.load1 is not None else ""
    lines.append(f"CPU   {cpu_pct:>5}  ({cores}, {freq}){load}  [priority: {r.priority}]")
    if r.mem_total_mb:
        used_gb = (r.mem_used_mb or 0) / 1000
        total_gb = r.mem_total_mb / 1000
        lines.append(f"RAM   {used_gb:.1f}/{total_gb:.1f} GB")
    for g in r.gpus:
        mem = f"{(g.mem_used_mb or 0) / 1000:.1f}/{(g.mem_total_mb or 0) / 1000:.1f} GB"
        power = f"{_fmt(g.power_w, 'W')}/{_fmt(g.power_limit_w, 'W')}"
        lines.append(
            f"GPU{g.index} {_fmt(g.util_pct, '%'):>5}  {g.name}  {mem}  {power}  {_fmt(g.temp_c, 'C')}"
        )
    if (total := r.total_gpu_power_w) is not None:
        lines.append(f"GPU power draw: {total:.0f} W total")
    return lines
