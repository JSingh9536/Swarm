"""The machine-readable work queue the lead hands to the autopilot: queue.json.

Items are plain dicts (JSON round-trips exactly). `load`/`save` validate and write atomically;
`next_runnable`/`mark` are the only ways the autopilot should look at or change the list.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

STATES = ("todo", "doing", "done", "blocked")
TIERS = (0, 1, 2, 3)
REQUIRED = ("id", "state", "tier", "project", "task", "check")
DEFAULTS: dict[str, Any] = {
    "urgent": False,
    "attempts_local": 0,
    "attempts_claude": 0,
    "note": "",
    "report": "",
}


class QueueError(ValueError):
    """The queue file is malformed."""


def _validate_item(raw: Any, seen_ids: set[int]) -> dict:
    if not isinstance(raw, dict):
        raise QueueError(f"queue item must be an object, got {type(raw).__name__}: {raw!r}")
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise QueueError(f"queue item {raw.get('id', '?')!r} is missing field(s): {', '.join(missing)}")
    item = {**DEFAULTS, **raw}
    if not isinstance(item["id"], int) or isinstance(item["id"], bool):
        raise QueueError(f"item id must be an int, got {item['id']!r}")
    if item["id"] in seen_ids:
        raise QueueError(f"duplicate item id: {item['id']}")
    seen_ids.add(item["id"])
    if item["state"] not in STATES:
        raise QueueError(f"item {item['id']}: state must be one of {STATES}, got {item['state']!r}")
    if item["tier"] not in TIERS:
        raise QueueError(f"item {item['id']}: tier must be one of {TIERS}, got {item['tier']!r}")
    if not isinstance(item["task"], str) or not item["task"].strip():
        raise QueueError(f"item {item['id']}: task must be a non-empty string")
    if not isinstance(item["check"], str) or not item["check"].strip():
        raise QueueError(f"item {item['id']}: check must be a non-empty string")
    if not isinstance(item["project"], str):
        raise QueueError(f"item {item['id']}: project must be a string (absolute path, or empty)")
    return item


def load(path: Path) -> list[dict]:
    """Load and validate queue.json. Missing file -> empty queue. Raises `QueueError` if malformed."""
    path = Path(path)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QueueError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(raw, list):
        raise QueueError(f"{path}: queue must be a JSON list of items, got {type(raw).__name__}")
    seen: set[int] = set()
    return [_validate_item(r, seen) for r in raw]


def save(path: Path, items: list[dict]) -> None:
    """Write `items` atomically: a temp file in the same directory, then an atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".queue-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def next_runnable(items: list[dict], claude_available: bool) -> dict | None:
    """First `todo` item in queue order that the autopilot (not the lead) may run right now.

    Tier 3 is the lead's own work and is never returned. Tier 2 needs Claude: it is returned only when
    `claude_available` is true, or the item opts into a local fallback (`"local_fallback": true`). Tier 1
    is always returned regardless of `claude_available` (the local-vs-Claude choice for it is `choose()`'s
    job, via `swarm.load.route`). Tier 0 is script-only work with no model step for the autopilot to run.
    """
    for item in items:
        if item["state"] != "todo":
            continue
        tier = item["tier"]
        if tier in (0, 3):
            continue
        if tier == 2 and not claude_available and not item.get("local_fallback"):
            continue
        return item
    return None


def mark(items: list[dict], id: int, **fields: Any) -> dict:  # noqa: A002 - "id" matches the item schema
    """Update the item with this `id` in place and return it. Raises `QueueError` if it is not found."""
    for item in items:
        if item["id"] == id:
            item.update(fields)
            return item
    raise QueueError(f"no item with id {id}")
