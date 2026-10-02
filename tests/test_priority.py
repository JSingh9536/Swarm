"""Background-mode priority lowering (polite, not stealthy)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from swarm.priority import lower_priority


def test_returns_note_and_never_raises() -> None:
    note = lower_priority()
    assert isinstance(note, str) and note
    assert lower_priority(idle=True)


def test_failure_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        def __getattr__(self, name: str) -> object:
            raise OSError("boom")

    if os.name == "nt":
        import ctypes

        monkeypatch.setattr(ctypes, "windll", Boom(), raising=False)
    else:
        monkeypatch.setattr(os, "nice", lambda inc: (_ for _ in ()).throw(PermissionError("nope")))
    assert "could not lower priority" in lower_priority()


@pytest.mark.skipif(os.name != "nt", reason="Windows priority-class check")
def test_windows_lowers_class_and_children_inherit() -> None:
    import ctypes

    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.GetPriorityClass.argtypes = [ctypes.c_void_p]
    k.GetPriorityClass.restype = ctypes.c_uint32

    probe = (
        "import ctypes;k=ctypes.windll.kernel32;k.GetCurrentProcess.restype=ctypes.c_void_p;"
        "k.GetPriorityClass.argtypes=[ctypes.c_void_p];k.GetPriorityClass.restype=ctypes.c_uint32;"
        "print(hex(k.GetPriorityClass(k.GetCurrentProcess())))"
    )
    note = lower_priority()
    assert "below-normal" in note
    assert k.GetPriorityClass(k.GetCurrentProcess()) == 0x4000
    child = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert child.stdout.strip() == "0x4000", "children must inherit the lowered priority"


@pytest.mark.skipif(os.name == "nt", reason="POSIX niceness check")
def test_posix_raises_niceness(monkeypatch: pytest.MonkeyPatch) -> None:
    level = {"v": 0}
    monkeypatch.setattr(os, "nice", lambda inc: level.__setitem__("v", level["v"] + inc) or level["v"])
    assert "nice 10" in lower_priority()
    assert "nice 19" in lower_priority(idle=True)
