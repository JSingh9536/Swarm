"""Hourly status refresh. No model, no network: reads the machine and the usage records, writes three files.

Run by Windows Task Scheduler with pythonw.exe (no console window), at low priority:
  docs/status.json            the latest full snapshot (the dashboard's data)
  docs/status-history.jsonl   one compact line per run, for trends (GPU temperature, tokens per day, disk space)
  <Obsidian vault>/Swarm/     the mirrored documents and the status note

It never raises: a failure is written to docs/status-error.log and the next hour tries again.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    from swarm import usage, vault
    from swarm.priority import lower_priority

    lower_priority(idle=True)  # never compete with a game or a call
    snap = usage.snapshot()
    (DOCS / "status.json").write_text(json.dumps(snap, indent=1), encoding="utf-8")

    gpu = (snap["machine"].get("gpus") or [{}])[0]
    today = (snap["claude"]["by_day"] or [{}])[-1]
    line = {
        "t": round(time.time()),
        "mode": snap["load"]["mode"],
        "gpu_temp_c": gpu.get("temp_c"),
        "gpu_power_w": gpu.get("power_w"),
        "gpu_util_pct": gpu.get("util_pct"),
        "cpu_pct": snap["machine"]["cpu"].get("percent"),
        "mem_used_mb": snap["machine"].get("mem_used_mb"),
        "disk_free_gb": {v["mount"]: v["free_gb"] for v in snap["machine"].get("volumes", [])},
        "disk_health": [d.get("health") for d in snap["machine"].get("disks", [])],
        "claude_output_today": today.get("output", 0),
        "claude_day": today.get("day"),
        "local_calls": snap["local"]["calls"],
        "local_tokens": snap["local"]["tokens_in"] + snap["local"]["tokens_out"],
        "local_kwh": snap["local"]["kwh"],
    }
    with (DOCS / "status-history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")

    target = vault.find_vault()
    if target is not None and target.is_dir():
        vault.sync(target, sorted(DOCS.glob("*.md")), snap)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a scheduled job must not pop up an error dialog
        with contextlib.suppress(OSError):
            (DOCS / "status-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        sys.exit(1)
