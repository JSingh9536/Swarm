"""Mirror the swarm's documents into an Obsidian vault so the owner can read and link them there.

One-way, swarm -> vault. Everything goes into one folder (`Swarm/` by default) and only files this module wrote
are ever overwritten or removed: each carries a `swarm_mirror: true` property, and a note without it is left
alone. Notes are plain Markdown with YAML properties and wikilinks, so no plugin is needed.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

FOLDER = "Swarm"
MARK = "swarm_mirror: true"
HOME = "Swarm Home"


def find_vault() -> Path | None:
    """SWARM_OBSIDIAN_VAULT, else the open (or most recently used) vault from Obsidian's own registry."""
    override = os.environ.get("SWARM_OBSIDIAN_VAULT")
    if override:
        return Path(override)
    appdata = Path(os.environ.get("APPDATA", "")) if os.name == "nt" else Path.home() / ".config"
    base = appdata / "obsidian"
    try:
        vaults = json.loads((base / "obsidian.json").read_text(encoding="utf-8")).get("vaults", {})
    except (OSError, ValueError):
        return None
    ranked = sorted(vaults.values(), key=lambda v: (bool(v.get("open")), v.get("ts", 0)), reverse=True)
    for v in ranked:
        path = Path(v.get("path", ""))
        if path.is_dir():
            return path
    return None


def _title(text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return (m.group(1).strip() if m else fallback)[:120]


def _note(body: str, source: Path, kind: str, synced: str) -> str:
    props = f'---\n{MARK}\nsource: "{source.as_posix()}"\nkind: {kind}\nsynced: {synced}\ntags: [swarm]\n---\n\n'
    return props + body.rstrip() + "\n"


def _ours(path: Path) -> bool:
    try:
        return MARK in path.read_text(encoding="utf-8", errors="replace")[:400]
    except OSError:
        return False


def status_note(snap: dict[str, Any]) -> str:
    """A readable status note from `usage.snapshot()`."""
    m, local, claude = snap["machine"], snap["local"], snap["claude"]
    lines = ["# Swarm status", "", "## PC right now", ""]
    lines += [f"- {ln}" for ln in snap["load"]["lines"][:3]]
    for g in m.get("gpus", []):
        lines.append(f"- {g['name']}: {g['temp_c']} C, {g['power_w']} W of {g['power_limit_w']} W, "
                     f"{g['mem_used_mb']:.0f}/{g['mem_total_mb']:.0f} MB video memory")  # fmt: skip
    for d in m.get("disks", []):
        lines.append(f"- disk {d.get('name')} ({d.get('size_gb')} GB): {d.get('health')}")
    for v in m.get("volumes", []):
        lines.append(f"- {v['mount']} {v['free_gb']} GB free of {v['total_gb']} GB ({v['used_pct']}% used)")
    lines += ["", "## Local model (Ollama)", "",
              f"- {local['calls']} agent calls, {local['tokens_in'] + local['tokens_out']:,} tokens, "
              f"{local['seconds']:.0f} s, {local['kwh']} kWh (about ${local['electricity_usd']})",
              f"- modes used: {local['modes'] or 'none yet'}"]  # fmt: skip
    lines += ["", f"## Claude plan, last {claude['since_days']} days", ""]
    lines += ["| Day | Output tokens | Input tokens | Cache read | Messages |", "|---|---:|---:|---:|---:|"]
    for row in claude["by_day"]:
        lines.append(f"| {row['day']} | {row.get('output', 0):,} | {row.get('input', 0):,} | "
                     f"{row.get('cache_read', 0):,} | {row.get('messages', 0):,} |")  # fmt: skip
    lines += ["", "| Project | Output tokens |", "|---|---:|"]
    lines += [f"| {p['project']} | {p.get('output', 0):,} |" for p in claude["by_project"]]
    return "\n".join(lines)


def sync(
    vault: Path, sources: list[Path], snap: dict[str, Any] | None = None, folder: str = FOLDER
) -> dict[str, Any]:
    """Write the mirror. Returns {"folder", "written", "removed", "skipped"}."""
    dest = vault / folder
    dest.mkdir(parents=True, exist_ok=True)
    synced = time.strftime("%Y-%m-%d %H:%M")
    written: list[str] = []
    skipped: list[str] = []
    index: list[tuple[str, str]] = []

    def put(name: str, text: str) -> None:
        target = dest / f"{name}.md"
        if target.exists() and not _ours(target):
            skipped.append(target.name)  # the owner's own note with the same name: never overwrite
            return
        target.write_text(text, encoding="utf-8")
        written.append(target.name)

    for src in sources:
        try:
            body = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name = re.sub(r'[\\/:*?"<>|#^\[\]]', "-", src.stem)
        put(name, _note(body, src, "document", synced))
        index.append((name, _title(body, src.stem)))
    if snap is not None:
        put("Swarm Status", _note(status_note(snap), Path("swarm status --json"), "status", synced))
        index.append(("Swarm Status", "Live usage and PC health snapshot"))

    intro = f"Mirror of the swarm's documents. Last synced {synced}. Edit the originals, not these."
    home = [f"# {HOME}", "", intro, ""]
    home += [f"- [[{name}]]: {title}" for name, title in sorted(index)]
    put(HOME, _note("\n".join(home), Path("swarm vault"), "index", synced))

    keep = {f"{name}.md" for name, _ in index} | {f"{HOME}.md"}
    removed = []
    for old in dest.glob("*.md"):
        if old.name not in keep and _ours(old):
            old.unlink()
            removed.append(old.name)
    return {"folder": str(dest), "written": written, "removed": removed, "skipped": skipped}
