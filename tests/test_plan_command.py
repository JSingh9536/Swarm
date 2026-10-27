"""`swarm plan`: a one-line idea becomes a full written plan; only the architect runs and nothing is built."""

from __future__ import annotations

import asyncio
from pathlib import Path

from swarm import prompts
from swarm.cli import build_parser, cmd_plan
from swarm.config import Config
from swarm.models import Plan
from swarm.pipeline import Pipeline
from swarm.report import RecordingReporter
from swarm.roles import load_roles
from swarm.testing import ScriptedBackend, fail, ok

ROLES = load_roles()

PLAN = Plan.model_validate(
    {
        "title": "Invoice CSV export",
        "complexity": "small",
        "goal": "Office staff download an issued invoice's lines as CSV",
        "architecture": "One pure `toCsv()` in the web app; cells starting with = + - @ are escaped.",
        "security_relevant": True,
        "acceptance_criteria": [
            {"id": "AC1", "text": "Download gives one row per invoice line", "verify": "test_csv_rows"},
            {"id": "AC2", "text": "Formula-looking cells are escaped", "verify": "test_csv_escape"},
        ],
        "work_items": [
            {
                "id": "W1",
                "title": "CSV builder",
                "goal": "toCsv() with escaping",
                "files": ["csv.ts", "csv.test.ts"],
                "done_check": "npm test",
            },
            {
                "id": "W2",
                "title": "Download button",
                "goal": "button on the invoice page",
                "files": ["Periods.tsx"],
                "depends_on": ["W1"],
            },
        ],  # fmt: skip
        "test_commands": ["npm test"],
        "risks": ["Excel reads a leading tab as text"],
        "open_questions": ["Include voided invoices?"],
    }
)


def plan_pipeline(script: dict) -> tuple[Pipeline, ScriptedBackend]:
    backend = ScriptedBackend(script)
    return Pipeline(Config(), ROLES, backend, RecordingReporter()), backend


def test_plan_runs_only_the_architect_and_writes_the_full_plan(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("print('hi')\n", encoding="utf-8")
    pipe, backend = plan_pipeline({"architect": [ok(PLAN)]})

    s = asyncio.run(pipe.plan_only("add CSV export to invoices", project))

    assert s.status == "success"
    assert [r.role.name for r in backend.requests] == ["architect"]
    for name in ("plan.json", "plan.md", "plan-full.md"):
        assert (s.run_dir / name).is_file(), name
    doc = (s.run_dir / "plan-full.md").read_text(encoding="utf-8")
    assert doc == prompts.plan_document("add CSV export to invoices", s.plan)
    # the project itself is untouched: no git repo created, no files besides the run folder
    assert not (project / ".git").exists()
    assert sorted(p.name for p in project.iterdir()) == [".swarm", "app.py"]
    assert (project / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_plan_document_has_every_section_a_person_needs() -> None:
    doc = prompts.plan_document("  add CSV export to invoices  ", PLAN)
    for heading in ("# Plan: Invoice CSV export", "## The request", "## Goal", "## Design and scope", "## Steps",
                    "## Acceptance tests", "## Commands", "## Risks", "## Questions for the owner"):  # fmt: skip
        assert heading in doc, heading
    assert "add CSV export to invoices\n" in doc
    assert "Size: small. Security-relevant: yes." in doc
    assert "1. **W1 CSV builder**: toCsv() with escaping" in doc
    assert "   - Files: csv.ts, csv.test.ts" in doc
    assert "   - After: W1" in doc and "   - Done when: npm test" in doc
    assert "- AC2: Formula-looking cells are escaped (check: test_csv_escape)" in doc
    assert "- Include voided invoices?" in doc and "- Excel reads a leading tab as text" in doc


def test_plan_document_says_so_when_lists_are_empty() -> None:
    bare = PLAN.model_copy(update={"open_questions": [], "risks": [], "architecture": "", "work_items": []})
    doc = prompts.plan_document("x", bare)
    assert "## Design and scope" not in doc
    assert "- (none: assumptions are in the design)" in doc
    assert "## Steps\n- (none)" in doc


def test_a_failed_architect_leaves_no_plan(tmp_path: Path) -> None:
    pipe, backend = plan_pipeline({"architect": [fail("model error")]})
    s = asyncio.run(pipe.plan_only("anything", tmp_path / "new"))
    assert s.status == "failed"
    assert not (s.run_dir / "plan-full.md").exists()
    assert any("planning failed" in n for n in s.notes)


def test_cli_parses_plan_and_refuses_an_empty_request(capsys) -> None:
    args = build_parser().parse_args(["plan", "add", "CSV", "export", "--project", ".", "--yes", "--budget", "1"])
    assert args.func is cmd_plan and args.task == ["add", "CSV", "export"] and args.budget == 1.0
    empty = build_parser().parse_args(["plan"])
    assert cmd_plan(empty) == 2
    assert "swarm plan" in capsys.readouterr().out


def test_cli_refuses_a_missing_project(tmp_path: Path, capsys) -> None:
    args = build_parser().parse_args(["plan", "x", "--project", str(tmp_path / "nope"), "--yes"])
    assert cmd_plan(args) == 2
    assert "not found" in capsys.readouterr().out
