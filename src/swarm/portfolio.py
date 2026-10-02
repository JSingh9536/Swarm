"""One row of docs/PORTFOLIO.md from a run's summary.json (`swarm portfolio --add`).

The row reports what the run's own gate did, never what an agent claimed.
"""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

STATUS_WORDS = {"success": "working", "needs_attention": "needs-attention"}
MAX_TASK_CHARS = 120


def _cell(text: str) -> str:
    """A table cell cannot hold a pipe or a line break."""
    return " ".join(str(text).replace("|", "/").split())


def row_from_summary(summary: dict) -> str:
    """The Markdown table row for one run: App | Location | What it does | Status | Run report | Verified."""
    project = str(summary.get("project_dir", ""))
    task_lines = str(summary.get("task", "")).strip().splitlines()
    task = _cell(task_lines[0] if task_lines else "")[:MAX_TASK_CHARS].rstrip()
    ran = [g for g in summary.get("gate") or [] if not g.get("skipped")]
    passed = sum(1 for g in ran if g.get("ok"))
    verified = f"{passed} of {len(ran)} gate commands passed" if ran else "no gate ran"
    cells = [
        _cell(PureWindowsPath(project).name or project),  # PureWindowsPath splits on both \ and /
        f"`{_cell(project)}`",
        task,
        STATUS_WORDS.get(str(summary.get("status")), "failed"),
        f"`{_cell(summary.get('run_dir', ''))}/report.md`",
        verified,
    ]
    return "| " + " | ".join(cells) + " |"


def append_row(summary_path: Path, portfolio_path: Path) -> str:
    """Insert the run's row after the last table line of `portfolio_path`; a row already there is not repeated."""
    row = row_from_summary(json.loads(Path(summary_path).read_text(encoding="utf-8")))
    portfolio_path = Path(portfolio_path)
    lines = portfolio_path.read_text(encoding="utf-8").splitlines() if portfolio_path.is_file() else []
    if row in lines:
        return row
    table = [i for i, line in enumerate(lines) if line.startswith("|")]
    lines.insert(table[-1] + 1 if table else len(lines), row)
    portfolio_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return row
