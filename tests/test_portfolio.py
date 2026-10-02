"""`swarm.portfolio`: one PORTFOLIO.md row from a run's summary.json. Offline, tmp_path only."""

from __future__ import annotations

import json
from pathlib import Path

from swarm.portfolio import append_row, row_from_summary

TABLE = (
    "# Portfolio\n\n"
    "| App | Location | What it does | Status | Run report | Verified |\n"
    "|---|---|---|---|---|---|\n"
    "| old | `D:\\old` | something | working | `r/report.md` | 1 of 1 gate commands passed |\n"
    "\n## How this stays current\ntext after the table\n"
)


def _summary(**kw) -> dict:
    base = {
        "status": "success",
        "task": "Build a slug helper\nwith more detail on later lines",
        "project_dir": "D:\\Code\\swarm\\workspace\\slugify",
        "run_dir": "D:\\Code\\swarm\\workspace\\slugify\\.swarm\\runs\\1",
        "gate": [
            {"command": "pytest -q", "ok": True, "skipped": False, "exit_code": 0},
            {"command": "ruff check .", "ok": False, "skipped": False, "exit_code": 1},
        ],
    }
    base.update(kw)
    return base


def test_success_row_is_exact() -> None:
    assert row_from_summary(_summary()) == (
        "| slugify | `D:\\Code\\swarm\\workspace\\slugify` | Build a slug helper | working | "
        "`D:\\Code\\swarm\\workspace\\slugify\\.swarm\\runs\\1/report.md` | 1 of 2 gate commands passed |"
    )


def test_status_words() -> None:
    assert " | needs-attention | " in row_from_summary(_summary(status="needs_attention"))
    assert " | failed | " in row_from_summary(_summary(status="plan_limit"))
    assert " | failed | " in row_from_summary(_summary(status=None))


def test_gate_counts_ignore_skipped_and_handle_none() -> None:
    assert row_from_summary(_summary(gate=[])).endswith("| no gate ran |")
    assert row_from_summary(_summary(gate=None)).endswith("| no gate ran |")
    gate = [{"ok": True, "skipped": False}, {"ok": False, "skipped": True}]
    assert row_from_summary(_summary(gate=gate)).endswith("| 1 of 1 gate commands passed |")
    assert row_from_summary(_summary(gate=[{"ok": False, "skipped": True}])).endswith("| no gate ran |")


def test_pipes_and_length_cannot_break_the_table() -> None:
    row = row_from_summary(_summary(task="a | b " + "x" * 300))
    assert row.count(" | ") == 5  # still six cells
    assert "a / b" in row
    assert len(row.split(" | ")[2]) <= 120


def test_posix_project_path_gives_the_folder_name() -> None:
    assert row_from_summary(_summary(project_dir="/home/me/apps/slugify")).startswith("| slugify | ")


def test_append_row_goes_after_the_table_and_only_once(tmp_path: Path) -> None:
    summary, portfolio = tmp_path / "summary.json", tmp_path / "PORTFOLIO.md"
    summary.write_text(json.dumps(_summary()), encoding="utf-8")
    portfolio.write_text(TABLE, encoding="utf-8")

    row = append_row(summary, portfolio)
    lines = portfolio.read_text(encoding="utf-8").splitlines()
    assert lines[lines.index(row) - 1].startswith("| old |")  # directly after the last table line
    assert lines[lines.index(row) + 1] == ""  # and before the text that follows the table
    assert lines[-1] == "text after the table"

    assert append_row(summary, portfolio) == row
    assert portfolio.read_text(encoding="utf-8").splitlines().count(row) == 1


def test_append_row_to_a_file_without_a_table(tmp_path: Path) -> None:
    summary, portfolio = tmp_path / "summary.json", tmp_path / "PORTFOLIO.md"
    summary.write_text(json.dumps(_summary()), encoding="utf-8")
    assert append_row(summary, portfolio) == portfolio.read_text(encoding="utf-8").strip()
