"""Run swarm as a background citizen: lower scheduling priority so the machine stays usable.

This lowers the priority of the swarm process; the Claude Code engine and the test/lint subprocesses
it spawns inherit it, so they only get idle CPU/IO time. It is polite, not stealthy: the processes stay
named and visible in Task Manager exactly as before.
"""

from __future__ import annotations

import os

# Windows priority classes (winbase.h)
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_IDLE_PRIORITY_CLASS = 0x00000040


def lower_priority(idle: bool = False) -> str:
    """Lower this process's priority so foreground apps stay responsive. Returns a short human note.

    Children inherit the priority class, so the engine and gate subprocesses run low too. Never raises.
    `idle` uses the lowest class (only truly spare cycles); the default is below-normal (still yields, but
    keeps progressing under load).
    """
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # HANDLE is pointer-sized; without these, ctypes truncates the pseudo-handle to 32 bits and the call fails
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.SetPriorityClass.restype = ctypes.c_int
            handle = kernel32.GetCurrentProcess()
            cls = _IDLE_PRIORITY_CLASS if idle else _BELOW_NORMAL_PRIORITY_CLASS
            # set the priority CLASS (not background mode): children inherit it, so the engine and gate run low too
            if not kernel32.SetPriorityClass(handle, cls):
                return "could not lower priority (SetPriorityClass failed)"
            return "idle priority" if idle else "below-normal priority (foreground apps keep the CPU)"
        # POSIX: raise niceness; children inherit it. 19 is the lowest, 10 a gentle default.
        target = 19 if idle else 10
        current = os.nice(0)
        if current < target:
            os.nice(target - current)
        return f"nice {os.nice(0)} (foreground apps keep the CPU)"
    except Exception as exc:  # noqa: BLE001 - priority is a nicety; never break the run over it
        return f"could not lower priority ({type(exc).__name__}: {exc})"
