"""Is the owner using this PC right now, and how should a local run behave if so?

The local model shares one GPU and one CPU with whatever the owner is doing. A game needs the GPU and its
memory; a video call needs steady CPU. This module looks at what is running and picks a mode:

    full        nobody is using the machine: GPU, normal priority
    background  a call or other foreground work: GPU, few CPU threads, model server at idle priority
    cpu_only    a game owns the GPU: no GPU, model unloaded from video memory, few threads, idle priority
    defer       the machine is saturated: wait for it to calm down, then run cpu_only

It also answers the routing question (`route`): spend Claude-plan tokens only when the work needs Claude, when
the local model has already failed, or when the PC is busy AND the task cannot wait.

Detection is separated from decision so the decision is unit-tested without any hardware. Everything degrades
gracefully: no psutil, no nvidia-smi and no Windows APIs each just remove one signal.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field

from swarm import hardware

MODES = ("full", "background", "cpu_only", "defer")

# Process names (lower-case) that mean a live call. Zoom's CptHost.exe only exists while a meeting is running.
CALL_PROCESSES = frozenset({"cpthost.exe", "zoom meeting.exe", "webexmta.exe", "gotomeeting.exe"})
# Open conferencing/streaming apps: a hint, not proof of a call.
CALL_HINT_PROCESSES = frozenset({"zoom.exe", "ms-teams.exe", "teams.exe", "obs64.exe", "obs32.exe"})
# Any GPU process whose path contains one of these is treated as a game.
GAME_PATH_MARKERS = (
    "\\steamapps\\common\\", "\\epic games\\", "\\riot games\\", "\\games\\", "\\bdo\\", "\\gog galaxy\\games\\",
)  # fmt: skip
# GPU users that live in game folders but are not games.
NOT_GAMES = frozenset({"wallpaper32.exe", "wallpaper64.exe", "steamwebhelper.exe", "steam.exe"})

GPU_BUSY_PCT = 55.0  # other apps using more than this much of the GPU: leave it alone
GPU_SATURATED_PCT = 90.0
CPU_BUSY_PCT = 65.0
CPU_SATURATED_PCT = 90.0
VRAM_NEEDED_MB = 5600.0  # a 7B coder model with a 16K context
BACKGROUND_THREADS = 4
DEFER_POLL_S = 20.0
DEFER_MAX_S = float(os.environ.get("SWARM_DEFER_MAX_S", "600"))
CACHE_S = 15.0


@dataclass
class Signals:
    """What was observed. All optional: a missing signal never counts as busy."""

    gpu_util_pct: float | None = None
    vram_free_mb: float | None = None
    cpu_pct: float | None = None
    games: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    call_hints: list[str] = field(default_factory=list)
    fullscreen: str | None = None
    model_loaded: bool = False  # our own model currently sits in video memory


@dataclass
class Decision:
    mode: str
    reasons: list[str]
    signals: Signals

    @property
    def busy(self) -> bool:
        return self.mode != "full"

    def ollama_options(self) -> dict[str, int]:
        """Extra Ollama request options for this mode (merged over the context-size option)."""
        if self.mode in ("cpu_only", "defer"):
            return {"num_gpu": 0, "num_thread": BACKGROUND_THREADS}
        if self.mode == "background":
            return {"num_thread": BACKGROUND_THREADS}
        return {}


def decide(s: Signals) -> Decision:
    """Pure decision from observed signals."""
    reasons: list[str] = []
    cpu = s.cpu_pct or 0.0
    gpu = s.gpu_util_pct or 0.0
    # Our own loaded model occupies video memory, so "free" is only meaningful when it is not loaded.
    vram_short = s.vram_free_mb is not None and not s.model_loaded and s.vram_free_mb < VRAM_NEEDED_MB

    if s.games:
        reasons.append(f"game running: {', '.join(s.games[:2])}")
    if s.fullscreen and not s.games:
        reasons.append(f"fullscreen app: {s.fullscreen}")
    if s.calls:
        reasons.append(f"call in progress: {', '.join(s.calls[:2])}")
    if gpu >= GPU_BUSY_PCT:
        reasons.append(f"GPU {gpu:.0f}% used by other apps")
    if vram_short:
        reasons.append(f"only {s.vram_free_mb:.0f} MB video memory free (model needs {VRAM_NEEDED_MB:.0f})")
    if cpu >= CPU_BUSY_PCT:
        reasons.append(f"CPU {cpu:.0f}%")

    gpu_contended = bool(s.games) or bool(s.fullscreen) or gpu >= GPU_BUSY_PCT or vram_short
    if cpu >= CPU_SATURATED_PCT or (gpu_contended and cpu >= CPU_BUSY_PCT) or (s.calls and gpu >= GPU_SATURATED_PCT):
        return Decision("defer", reasons + ["machine is saturated: wait, then run on spare CPU"], s)
    if gpu_contended:
        return Decision("cpu_only", reasons, s)
    if s.calls or cpu >= CPU_BUSY_PCT:
        return Decision("background", reasons, s)
    if s.call_hints:
        reasons.append(f"{', '.join(s.call_hints[:2])} open (no call detected)")
    return Decision("full", reasons, s)


# --------------------------------------------------------------------------- routing


@dataclass
class Route:
    backend: str  # "none" | "local" | "claude"
    how: str  # mode for local runs, or the reason category for claude
    why: str


def route(tier: int, decision: Decision, *, local_failures: int = 0, urgent: bool = False,
          local_available: bool = True) -> Route:  # fmt: skip
    """Which engine should run a task. Tiers: 0 no model, 1 local-capable, 2 needs Claude, 3 lead only.

    Claude-plan tokens are spent only when the task needs Claude (tier >= 2), when the local model has already
    failed twice on a real test/lint gate, when there is no local model, or when the PC is busy and the task
    is marked urgent. A busy PC alone is never a reason: non-urgent work runs in the background or waits.
    """
    if tier <= 0:
        return Route("none", "script", "tests, lint and scripted checks need no model")
    if tier >= 2:
        return Route("claude", "needs-claude", "multi-file, review, security or production work")
    if not local_available:
        return Route("claude", "no-local-model", "Ollama is not running or has no model")
    if local_failures >= 2:
        return Route("claude", "escalated", "the local model failed the gate twice")
    if decision.mode in ("cpu_only", "defer") and urgent:
        return Route("claude", "busy-and-urgent", "the PC is in use and this task cannot wait")
    return Route("local", decision.mode, "; ".join(decision.reasons) or "PC is idle")


# --------------------------------------------------------------------------- detection


def _gpu_processes() -> list[str]:
    if not shutil.which("nvidia-smi"):
        return []
    lines: list[str] = []
    for query in ("--query-compute-apps=process_name", "--query-apps=process_name"):
        out = hardware._run(["nvidia-smi", query, "--format=csv,noheader"])
        if out:
            lines += [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines


def classify_processes(gpu_paths: list[str], all_names: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(games, calls, call_hints) from GPU process paths and all process names. Pure."""
    games: list[str] = []
    for path in gpu_paths:
        low = path.lower().replace("/", "\\")
        name = low.rsplit("\\", 1)[-1]
        if name in NOT_GAMES or "ollama" in name:
            continue
        if any(marker in low for marker in GAME_PATH_MARKERS) and name not in games:
            games.append(name)
    names = {n.lower() for n in all_names}
    return games, sorted(names & CALL_PROCESSES), sorted(names & CALL_HINT_PROCESSES)


def _process_names() -> list[str]:
    try:
        import psutil  # type: ignore

        return [p.info["name"] or "" for p in psutil.process_iter(["name"])]
    except Exception:  # noqa: BLE001 - psutil missing or a process vanished mid-scan
        return []


def _fullscreen_app() -> str | None:
    """Name of a foreground window that covers the whole primary screen (a game or a presentation), else None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd or hwnd in (user32.GetDesktopWindow(), user32.GetShellWindow()):
            return None
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        if (rect.right - rect.left) < width or (rect.bottom - rect.top) < height:
            return None
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value in ("WorkerW", "Progman", "Shell_TrayWnd"):  # the desktop itself
            return None
        title = ctypes.create_unicode_buffer(120)
        user32.GetWindowTextW(hwnd, title, 120)
        return title.value or cls.value or "fullscreen window"
    except Exception:  # noqa: BLE001 - a missing API is a missing signal
        return None


def _model_loaded() -> bool:
    out = hardware._run(["ollama", "ps"]) if shutil.which("ollama") else None
    return bool(out and len([ln for ln in out.splitlines() if ln.strip()]) > 1)


def observe() -> Signals:
    cpu, _, _ = hardware.cpu_stats()
    gpus = hardware.gpu_stats()
    gpu = gpus[0] if gpus else None
    games, calls, hints = classify_processes(_gpu_processes(), _process_names())
    loaded = _model_loaded()
    free = None
    if gpu and gpu.mem_total_mb is not None and gpu.mem_used_mb is not None:
        free = gpu.mem_total_mb - gpu.mem_used_mb
    util = gpu.util_pct if gpu else None
    if loaded and not games and (util or 0) > GPU_BUSY_PCT:
        util = None  # while our own model is generating, GPU load is ours and says nothing about the owner
    return Signals(
        gpu_util_pct=util,
        vram_free_mb=free,
        cpu_pct=cpu.percent,
        games=games,
        calls=calls,
        call_hints=hints,
        fullscreen=_fullscreen_app(),
        model_loaded=loaded,
    )  # fmt: skip


_cache: tuple[float, Decision] | None = None


def current(max_age_s: float = CACHE_S) -> Decision:
    """The decision for right now, cached briefly so a run does not probe the machine on every model call."""
    global _cache
    now = time.monotonic()
    if _cache is None or now - _cache[0] > max_age_s:
        _cache = (now, decide(observe()))
    return _cache[1]


# --------------------------------------------------------------------------- acting on it


def set_ollama_priority(idle: bool) -> int:
    """Put the Ollama server and runner processes at idle (or back to normal) priority. Returns how many changed."""
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001
        return 0
    changed = 0
    for proc in psutil.process_iter(["name"]):
        try:
            if "ollama" not in (proc.info["name"] or "").lower():
                continue
            if os.name == "nt":
                proc.nice(psutil.IDLE_PRIORITY_CLASS if idle else psutil.NORMAL_PRIORITY_CLASS)
            else:
                proc.nice(19 if idle else 0)
            changed += 1
        except Exception:  # noqa: BLE001 - not ours to change, or it just exited
            continue
    return changed


def unload_model() -> None:
    """Free the video memory our model holds (used when a game needs it). The next call reloads it."""
    if shutil.which("ollama"):
        out = hardware._run(["ollama", "ps"]) or ""
        for line in out.splitlines()[1:]:
            name = line.split()[0] if line.split() else ""
            if name:
                hardware._run(["ollama", "stop", name], timeout=20.0)


def prepare(wait: bool = True, sleep=time.sleep, on_note=None) -> Decision:
    """Called before a local model call: wait out a saturated machine, set priorities, free video memory.

    Returns the decision to run under. `defer` waits up to DEFER_MAX_S for the machine to calm down and then
    proceeds on spare CPU, so a long game session delays work but never blocks it forever.
    """
    decision = current()
    waited = 0.0
    while wait and decision.mode == "defer" and waited < DEFER_MAX_S:
        if on_note and waited == 0.0:
            on_note(f"PC is busy ({'; '.join(decision.reasons)}): waiting up to {DEFER_MAX_S:.0f}s")
        sleep(DEFER_POLL_S)
        waited += DEFER_POLL_S
        decision = current(max_age_s=0.0)
    if decision.mode in ("cpu_only", "defer"):
        if decision.signals.model_loaded:
            unload_model()
        set_ollama_priority(idle=True)
    elif decision.mode == "background":
        set_ollama_priority(idle=True)
    else:
        set_ollama_priority(idle=False)
    return decision


def render(decision: Decision) -> list[str]:
    s = decision.signals
    explain = {
        "full": "PC is idle: local runs use the GPU at normal priority",
        "background": "PC is in use: local runs keep the GPU but use few CPU threads at idle priority",
        "cpu_only": "GPU is in use: local runs stay off the GPU, on spare CPU at idle priority",
        "defer": "PC is saturated: local runs wait, then use spare CPU at idle priority",
    }[decision.mode]
    lines = [f"mode: {decision.mode}  ({explain})"]
    lines += [f"  - {r}" for r in decision.reasons] or ["  - nothing competing for the machine"]
    gpu = "n/a" if s.gpu_util_pct is None else f"{s.gpu_util_pct:.0f}%"
    free = "n/a" if s.vram_free_mb is None else f"{s.vram_free_mb:.0f} MB"
    cpu = "n/a" if s.cpu_pct is None else f"{s.cpu_pct:.0f}%"
    lines.append(f"signals: CPU {cpu} | GPU {gpu} | video memory free {free} | our model loaded: {s.model_loaded}")
    for tier, label in ((1, "small task"), (2, "multi-file / review")):
        r = route(tier, decision)
        lines.append(f"route tier {tier} ({label}): {r.backend} [{r.how}] - {r.why}")
    urgent = route(1, decision, urgent=True)
    lines.append(f"route tier 1 when urgent: {urgent.backend} [{urgent.how}] - {urgent.why}")
    return lines
