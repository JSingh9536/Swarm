"""Where events come from: tailing a live run's `events.jsonl`, and replaying a finished one at any speed."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swarm_ui import runs

MAX_GAP = 1.2  # a replay never sits still longer than this (scaled by speed) between two events
MIN_GAP = 0.08  # ...nor fires two events closer than this, so instant handoffs stay visible
SPEEDS = (0.5, 1.0, 2.0, 4.0, 8.0)


@dataclass(frozen=True)
class RunRef:
    project: str
    run: str
    source: str = "swarm"  # swarm (a pipeline run under the workspace) | claude (a recorded Claude Code session)

    @property
    def key(self) -> str:
        return f"{self.project}/{self.run}"

    @property
    def iid(self) -> str:
        return f"{self.source}:{self.key}"

    @classmethod
    def from_iid(cls, iid: str) -> RunRef | None:
        source, _, key = iid.partition(":")
        project, _, run = key.partition("/")
        return cls(project, run, source) if source in ("swarm", "claude") and project and run else None


class Tail:
    """Reads only what was appended to a run's events since the last call (partial lines wait for the next one)."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.path = folder / "events.jsonl"
        self.offset = 0
        self._partial = b""
        self.synthetic = False

    def read_new(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            if self.offset == 0 and not self.synthetic:  # older runs: rebuild a coarse timeline once
                events, _ = runs.load_events(self.folder)
                self.synthetic = bool(events)
                return events
            return []
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                data = fh.read(1024 * 1024)
        except OSError:
            return []
        self.offset += len(data)
        data = self._partial + data
        lines = data.split(b"\n")
        self._partial = lines.pop()
        out = []
        for line in lines:
            try:
                rec = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out


class Player:
    """Plays a list of events on a virtual clock: compressed gaps, adjustable speed, pause and seek."""

    def __init__(self, events: list[dict[str, Any]] | None = None, speed: float = 1.0) -> None:
        self.events: list[dict[str, Any]] = list(events or [])
        self.index = 0
        self.speed = speed
        self.paused = False
        self._wait = 0.0

    @property
    def done(self) -> bool:
        return self.index >= len(self.events)

    @property
    def progress(self) -> float:
        return self.index / len(self.events) if self.events else 0.0

    def extend(self, events: list[dict[str, Any]]) -> None:
        self.events.extend(events)

    def _gap(self, i: int) -> float:
        if i == 0:
            return 0.0
        a, b = self.events[i - 1].get("t"), self.events[i].get("t")
        real = (b - a) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else 0.3
        return min(max(real, MIN_GAP), MAX_GAP)

    def advance(self, dt: float) -> list[dict[str, Any]]:
        """Events that became due during `dt` seconds of wall time."""
        if self.paused or self.done:
            return []
        self._wait -= dt * self.speed
        out = []
        while not self.done and self._wait <= 0:
            out.append(self.events[self.index])
            self.index += 1
            if not self.done:
                self._wait += self._gap(self.index)
        return out

    def seek(self, index: int) -> list[dict[str, Any]]:
        """Jump to `index`; returns every event up to it so the caller can rebuild state from scratch."""
        self.index = max(0, min(index, len(self.events)))
        self._wait = self._gap(self.index) if not self.done else 0.0
        return self.events[: self.index]

    def catch_up(self) -> list[dict[str, Any]]:
        """Live mode: everything not yet played, at once."""
        out = self.events[self.index :]
        self.index = len(self.events)
        self._wait = 0.0
        return out


def list_runs(workspace: Path, limit: int = 80) -> list[dict[str, Any]]:
    return runs.overview(workspace, limit)["runs"] if workspace.is_dir() else []


def newest_since(workspace: Path, since: float) -> RunRef | None:
    """The newest run folder touched after `since` (how a job the harness just launched is found)."""
    best: tuple[float, RunRef] | None = None
    for info in list_runs(workspace, 20):
        if info["updated"] >= since - 2 and (best is None or info["updated"] > best[0]):
            best = (info["updated"], RunRef(info["project"], info["run"]))
    return best[1] if best else None


def newest_active(root: Path, within: float = 20.0) -> RunRef | None:
    """The Claude Code session that wrote most recently, if it did so in the last `within` seconds."""
    best: tuple[float, Path] | None = None
    for events in root.glob("*/.swarm/runs/*/events.jsonl") if root.is_dir() else []:
        try:
            mtime = events.stat().st_mtime
        except OSError:
            continue
        if not (events.parent / "task.md").exists():
            continue  # not prompted yet: nothing to show
        if time.time() - mtime < within and (best is None or mtime > best[0]):
            best = (mtime, events.parent)
    if best is None or (best[1] / "summary.json").exists():
        return None
    run = best[1]
    return RunRef(run.parents[2].name, run.name, "claude")


def folder(workspace: Path, ref: RunRef) -> Path | None:
    return runs.run_dir(workspace, ref.project, ref.run)


def is_live(folder_path: Path, window: float = 120.0) -> bool:
    """No summary yet and written to recently: the pipeline is still going."""
    if (folder_path / "summary.json").exists():
        return False
    newest = max((p.stat().st_mtime for p in folder_path.iterdir() if p.is_file()), default=0.0)
    return time.time() - newest < window
