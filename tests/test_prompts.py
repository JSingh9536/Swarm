from __future__ import annotations

from pathlib import Path

import pytest

from swarm import prompts
from swarm.models import Finding, Plan


@pytest.fixture
def plan() -> Plan:
    return Plan.model_validate(
        {
            "title": "Todo CLI",
            "complexity": "small",
            "goal": "A todo list on the command line",
            "architecture": "One module `todo.py`.",
            "acceptance_criteria": [
                {"id": "AC1", "text": "add stores an item", "verify": "pytest -k add"},
                {"id": "AC2", "text": "list prints items"},
            ],
            "work_items": [
                {"id": "W1", "title": "storage", "goal": "json store", "files": ["todo.py"], "done_check": "pytest"},
                {"id": "W2", "title": "cli", "goal": "argparse", "files": ["cli.py"], "depends_on": ["W1"]},
            ],
            "test_commands": ["python -m pytest -q"],
            "run_instructions": "python cli.py add milk",
            "risks": ["file locking"],
        }
    )


def test_render_plan(plan: Plan) -> None:
    text = prompts.render_plan(plan, {"W1": "done"})
    assert text.startswith("# Todo CLI (small)")
    assert "- AC1: add stores an item  [verify: pytest -k add]" in text
    assert "- AC2: list prints items" in text
    assert "- W1 [done] storage | files: todo.py | after: - | check: pytest" in text
    assert "- W2 [todo] cli | files: cli.py | after: W1" in text
    assert "One module `todo.py`." in text
    assert "Tests: python -m pytest -q" in text
    assert "Lint: (none)" in text
    assert "Run: python cli.py add milk" in text
    assert "- file locking" in text


def test_render_minimal_plan() -> None:
    minimal = Plan(title="T", complexity="trivial", goal="g", acceptance_criteria=[], work_items=[])
    text = prompts.render_plan(minimal)
    assert "Architecture notes" not in text and "Risks" not in text and "Run:" not in text
    assert "(detect from project)" in text


def test_runtime_notes(tmp_path: Path) -> None:
    structured = prompts.runtime_notes(tmp_path, tmp_path / ".swarm" / "runs" / "r1", structured=True)
    plain = prompts.runtime_notes(tmp_path, tmp_path, structured=False)
    assert str(tmp_path) in structured and "runs" in structured
    assert prompts.STRUCTURED_NOTE in structured
    assert prompts.STRUCTURED_NOTE not in plain
    assert "{" not in plain, "unformatted placeholder left in the notes"


def test_implement_prompt_marks_this_item(plan: Plan, tmp_path: Path) -> None:
    research = tmp_path / "research.md"
    research.write_text("brief", encoding="utf-8")
    text = prompts.implement_prompt("task", plan, plan.work_items[1], {"W1": "done"}, research, ["pytest"], [])
    assert text.startswith("# Work item W2: cli")
    assert "- W2 [THIS ITEM] cli" in text
    assert "- W1 [done] storage" in text
    assert str(research) in text
    assert "Lint: (none configured)" in text


def test_implement_prompt_without_research(plan: Plan, tmp_path: Path) -> None:
    text = prompts.implement_prompt("t", plan, plan.work_items[0], {}, tmp_path / "missing.md", [], [], "EXTRA")
    assert "No research brief" in text
    assert text.rstrip().endswith("EXTRA")


def test_review_prompt_with_git(plan: Plan) -> None:
    text = prompts.review_prompt("reviewer", "task", plan, "git diff abc123", True, ["a.py"], [])
    assert "`git diff abc123`" in text
    assert "Previous round" not in text
    assert prompts.REVIEW_FOCUS["reviewer"] in text


def test_review_prompt_without_git_lists_files(plan: Plan) -> None:
    text = prompts.review_prompt("security-auditor", "task", plan, "git diff", False, ["a.py", "b.py"], [])
    assert "not under git" in text and "a.py, b.py" in text
    assert "git diff" not in text
    assert prompts.REVIEW_FOCUS["security-auditor"] in text


def test_review_prompt_previous_findings(plan: Plan) -> None:
    prev = [Finding(severity="major", location="a.py:3", problem="off by one")]
    text = prompts.review_prompt("reviewer", "t", plan, "git diff", True, [], prev)
    assert "Previous round" in text and "- [major] a.py:3 - off by one" in text


def test_fix_prompt_numbers_findings(plan: Plan) -> None:
    findings = [
        Finding(severity="blocker", location="a.py:1", problem="crash", fix="guard None"),
        Finding(severity="major", problem="no test"),
    ]
    text = prompts.fix_prompt("t", plan, findings, 2)
    assert "(round 2)" in text
    assert "1. [blocker] a.py:1 - crash\n   Suggested fix: guard None" in text
    assert "2. [major] (no location) - no test" in text


def test_other_prompts(plan: Plan) -> None:
    assert "Custom Q?" in prompts.research_prompt("t", ["Custom Q?"])
    assert "Which libraries" in prompts.research_prompt("t")
    assert "(none - rely on standard practice)" in prompts.plan_prompt("t", "- empty", [], [], None)
    assert "BRIEF" in prompts.plan_prompt("t", "- empty", ["pytest"], [], "  BRIEF  ")
    dbg = prompts.debug_prompt("t", plan, "FAILED test_x", ["pytest -q"])
    assert "FAILED test_x" in dbg and "- pytest -q" in dbg
    qa = prompts.qa_prompt("t", plan, ["it works"], ["pytest"])
    assert "- it works" in qa and "python cli.py add milk" in qa
    assert "- (none)" in prompts.qa_prompt("t", plan, [], [])
    assert "pytest" in prompts.docs_prompt("t", plan, ["pytest"])
