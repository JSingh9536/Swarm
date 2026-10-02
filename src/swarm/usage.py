"""Usage ledger and status snapshot: what the swarm cost in Claude-plan tokens, local-model work, watts and wear.

Three sources, all read-only except the swarm's own ledger file:
- the swarm's local ledger (one JSON line per local-model agent call, written by LocalBackend),
- Claude Code's own session transcripts under ~/.claude/projects (token counts per assistant message),
- the machine itself (GPU, CPU, memory, disks).

`snapshot()` returns one JSON-serialisable dict; `swarm status --json` prints it for the dashboard.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swarm import hardware

LEDGER_ENV = "SWARM_USAGE_LEDGER"
ELECTRICITY_USD_PER_KWH = float(os.environ.get("SWARM_USD_PER_KWH", "0.30"))
MAX_TRANSCRIPT_BYTES = 400_000_000


def ledger_path() -> Path:
    override = os.environ.get(LEDGER_ENV)
    return Path(override) if override else Path.home() / ".swarm" / "usage.jsonl"


def record_local(entry: dict[str, Any]) -> None:
    """Append one local-model call to the ledger. Never raises: accounting must not break a run."""
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"t": time.time(), **entry}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).astimezone().strftime("%Y-%m-%d")


def local_summary(path: Path | None = None) -> dict[str, Any]:
    """Totals and per-day rows from the local ledger."""
    path = path or ledger_path()
    days: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    modes: dict[str, int] = defaultdict(int)
    total: dict[str, float] = defaultdict(float)
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            day = _day(float(e.get("t", 0)))
            tokens_in, tokens_out = int(e.get("prompt_tokens") or 0), int(e.get("output_tokens") or 0)
            seconds = float(e.get("seconds") or 0.0)
            # Energy the model run added: GPU draw while generating, over the time spent generating.
            wh = float(e.get("gpu_w") or 0.0) * float(e.get("gen_seconds") or 0.0) / 3600.0
            for bucket in (days[day], total):
                bucket["calls"] += 1
                bucket["ok"] += 1 if e.get("ok") else 0
                bucket["tokens_in"] += tokens_in
                bucket["tokens_out"] += tokens_out
                bucket["seconds"] += seconds
                bucket["wh"] += wh
            modes[str(e.get("mode") or "full")] += 1
    wh_total = total.get("wh", 0.0)
    return {
        "calls": int(total.get("calls", 0)),
        "ok": int(total.get("ok", 0)),
        "tokens_in": int(total.get("tokens_in", 0)),
        "tokens_out": int(total.get("tokens_out", 0)),
        "seconds": round(total.get("seconds", 0.0), 1),
        "kwh": round(wh_total / 1000.0, 4),
        "electricity_usd": round(wh_total / 1000.0 * ELECTRICITY_USD_PER_KWH, 4),
        "usd_per_kwh": ELECTRICITY_USD_PER_KWH,
        "modes": dict(modes),
        "by_day": [
            {"day": d, **{k: (round(v, 2) if k in ("seconds", "wh") else int(v)) for k, v in sorted(row.items())}}
            for d, row in sorted(days.items())
        ],
    }


def claude_summary(projects_dir: Path | None = None, since_days: int = 14) -> dict[str, Any]:
    """Token counts from Claude Code's session transcripts, grouped by day, project and model.

    `output` and `input` are freshly billed tokens; `cache_write` and `cache_read` are prompt-cache tokens, which
    count far less against a plan. Sub-agent transcripts (the swarm's workers) are reported separately.
    """
    root = projects_dir or Path.home() / ".claude" / "projects"
    cutoff = time.time() - since_days * 86400
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_project: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_model: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total: dict[str, int] = defaultdict(int)
    scanned = 0
    if root.exists():
        files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff or scanned + st.st_size > MAX_TRANSCRIPT_BYTES:
                continue
            scanned += st.st_size
            project = f.relative_to(root).parts[0]
            subagent = "subagents" in f.parts
            # A streamed message is logged once per content block, and the early lines carry a partial output
            # count. Keep one row per message id with the largest value of each counter (the final one).
            rows: dict[str, tuple[float, str, dict[str, int]]] = {}
            try:
                fh = f.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    msg = o.get("message") or {}
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    try:
                        ts = datetime.fromisoformat(str(o.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        ts = st.st_mtime
                    if ts < cutoff:
                        continue
                    row = {
                        "input": int(u.get("input_tokens") or 0),
                        "output": int(u.get("output_tokens") or 0),
                        "cache_write": int(u.get("cache_creation_input_tokens") or 0),
                        "cache_read": int(u.get("cache_read_input_tokens") or 0),
                    }
                    mid = msg.get("id") or o.get("uuid") or f"line-{len(rows)}"
                    if mid in rows:
                        prev = rows[mid][2]
                        row = {k: max(v, prev[k]) for k, v in row.items()}
                    rows[mid] = (ts, str(msg.get("model") or "unknown"), row)
            for ts, model, row in rows.values():
                row["messages"] = 1
                for bucket in (by_day[_day(ts)], by_project[project], by_model[model], total):
                    for k, v in row.items():
                        bucket[k] += v
                if subagent:
                    total["subagent_output"] += row["output"]
                    total["subagent_messages"] += 1
                    by_day[_day(ts)]["subagent_output"] += row["output"]
    def ranked(groups: dict[str, dict[str, int]], key: str) -> list[dict[str, Any]]:
        return [{key: name, **dict(v)} for name, v in sorted(groups.items(), key=lambda kv: -kv[1]["output"])]

    return {
        "since_days": since_days,
        "total": dict(total),
        "by_day": [{"day": d, **dict(v)} for d, v in sorted(by_day.items())],
        "by_project": ranked(by_project, "project"),
        "by_model": ranked(by_model, "model"),
    }


def disk_health() -> list[dict[str, Any]]:
    """Physical disks with Windows' own health verdict and, where the drive reports them, wear and temperature."""
    if os.name != "nt":
        return []
    script = (
        "Get-PhysicalDisk | ForEach-Object { $r = $_ | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue; "
        "[pscustomobject]@{ name=$_.FriendlyName; media=[string]$_.MediaType; health=[string]$_.HealthStatus; "
        "size_gb=[math]::Round($_.Size/1GB); wear_pct=$r.Wear; temp_c=$r.Temperature; "
        "power_on_hours=$r.PowerOnHours; read_errors=$r.ReadErrorsUncorrected } } | ConvertTo-Json -Compress"
    )
    out = hardware._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=25.0)
    if not out or not out.strip():
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def volumes() -> list[dict[str, Any]]:
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        rows.append({"mount": part.mountpoint, "total_gb": round(u.total / 1e9), "free_gb": round(u.free / 1e9),
                     "used_pct": u.percent})  # fmt: skip
    return rows


def machine() -> dict[str, Any]:
    r = hardware.read()
    uptime_h = None
    try:
        import psutil  # type: ignore

        uptime_h = round((time.time() - psutil.boot_time()) / 3600.0, 1)
    except Exception:  # noqa: BLE001
        pass
    return {
        "cpu": {"percent": r.cpu.percent, "logical": r.cpu.logical, "physical": r.cpu.physical},
        "mem_used_mb": r.mem_used_mb,
        "mem_total_mb": r.mem_total_mb,
        "uptime_hours": uptime_h,
        "gpus": [
            {"name": g.name, "util_pct": g.util_pct, "mem_used_mb": g.mem_used_mb, "mem_total_mb": g.mem_total_mb,
             "power_w": g.power_w, "power_limit_w": g.power_limit_w, "temp_c": g.temp_c}
            for g in r.gpus
        ],  # fmt: skip
        "disks": disk_health(),
        "volumes": volumes(),
    }


def snapshot(since_days: int = 14) -> dict[str, Any]:
    from swarm import load

    decision = load.current(max_age_s=0.0)
    return {
        "generated_at": time.time(),
        "load": {"mode": decision.mode, "reasons": decision.reasons, "lines": load.render(decision)},
        "machine": machine(),
        "local": local_summary(),
        "claude": claude_summary(since_days=since_days),
    }
