"""Structured reports exchanged between pipeline stages.

Agents return these as validated JSON (SDK structured output), so the pipeline's
control flow never depends on parsing free text.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Complexity = Literal["trivial", "small", "medium", "large"]
Severity = Literal["blocker", "major", "minor", "nit"]
BLOCKING: frozenset[str] = frozenset({"blocker", "major"})


class AcceptanceCriterion(BaseModel):
    id: str = Field(description="Short id such as AC1")
    text: str = Field(description="A testable statement of required behavior")
    verify: str = Field(default="", description="How to prove it: a test name or a command")


class WorkItem(BaseModel):
    id: str = Field(description="Short id such as W1")
    title: str
    goal: str = Field(description="What must be true when this item is done")
    files: list[str] = Field(default_factory=list, description="Files this item creates or edits")
    depends_on: list[str] = Field(default_factory=list, description="Ids of items that must finish first")
    done_check: str = Field(default="", description="Command or test that proves the item is done")


class Plan(BaseModel):
    title: str
    complexity: Complexity
    goal: str
    architecture: str = Field(
        default="", description="Markdown design notes: modules, interfaces, data shapes, choices"
    )
    security_relevant: bool = Field(
        default=False,
        description="True if the work handles untrusted input, files, network, credentials, "
        "dependencies, or runs shell commands",
    )
    acceptance_criteria: list[AcceptanceCriterion]
    work_items: list[WorkItem]
    test_commands: list[str] = Field(default_factory=list, description="Commands that run the test suite")
    lint_commands: list[str] = Field(default_factory=list, description="Commands for linters or type checks")
    run_instructions: str = Field(default="", description="How a user runs the finished software")
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class DevReport(BaseModel):
    status: Literal["done", "partial", "blocked"]
    summary: str = ""
    changed: list[str] = Field(default_factory=list, description="Files changed, one line each")
    verified: list[str] = Field(default_factory=list, description="Commands run and their results")
    needs_research: list[str] = Field(
        default_factory=list, description="Questions that need outside information to answer"
    )
    concerns: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    severity: Severity
    location: str = Field(default="", description="path:line")
    problem: str
    fix: str = ""


class ReviewReport(BaseModel):
    verdict: Literal["APPROVE", "REQUEST_CHANGES"]
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)


class QACriterion(BaseModel):
    id: str
    status: Literal["pass", "fail", "untested"]
    evidence: str = Field(default="", description="Exact command and observed result")


class QAReport(BaseModel):
    verdict: Literal["PASS", "FAIL", "PARTIAL"]
    summary: str = ""
    criteria: list[QACriterion] = Field(default_factory=list)
    defects: list[Finding] = Field(default_factory=list)
    tests_added: list[str] = Field(default_factory=list)


def blocking(findings: list[Finding]) -> list[Finding]:
    """Findings that must be fixed before the work can ship."""
    return [f for f in findings if f.severity in BLOCKING]
