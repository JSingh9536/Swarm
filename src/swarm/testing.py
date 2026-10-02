"""Offline test doubles: a scripted backend for tests and for `swarm demo` (no model calls, no cost)."""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from swarm.backend import AgentRequest, AgentResult, EventSink
from swarm.models import (
    AcceptanceCriterion,
    DevReport,
    Finding,
    Plan,
    QACriterion,
    QAReport,
    ReviewReport,
    WorkItem,
)

Reply = AgentResult | Callable[[AgentRequest], AgentResult]


def ok(
    structured: BaseModel | dict[str, Any] | None = None, text: str = "", cost: float = 0.05, turns: int = 3
) -> AgentResult:
    data = structured.model_dump(mode="json") if isinstance(structured, BaseModel) else structured
    return AgentResult(ok=True, text=text, structured=data, cost_usd=cost, turns=turns, subtype="success")


def fail(error: str, *, fatal: bool = False, cost: float = 0.0) -> AgentResult:
    return AgentResult(ok=False, error=error, fatal=fatal, cost_usd=cost, subtype="error")


class ScriptedBackend:
    """Replies with pre-programmed results, per role name, in order; records every request.

    A reply is either an AgentResult or a callable `(AgentRequest) -> AgentResult` (which may also
    touch the project directory, like a real developer would). When a role runs out of replies the
    call fails, unless `repeat_last` is set.
    """

    def __init__(self, script: dict[str, list[Reply]] | None = None, *, repeat_last: bool = False) -> None:
        self.script = {name: list(replies) for name, replies in (script or {}).items()}
        self.repeat_last = repeat_last
        self.requests: list[AgentRequest] = []

    def calls_for(self, role: str) -> list[AgentRequest]:
        return [r for r in self.requests if r.role.name == role]

    async def run(self, request: AgentRequest, on_event: EventSink | None = None) -> AgentResult:
        self.requests.append(request)
        queue = self.script.get(request.role.name)
        if not queue:
            return fail(f"no scripted reply left for role '{request.role.name}'")
        reply = queue[0] if self.repeat_last and len(queue) == 1 else queue.pop(0)
        result = reply(request) if callable(reply) else reply
        return dataclasses.replace(result)


# ---------------------------------------------------------------------------------- demo run

_SLUGIFY_V1 = '''"""Turn titles into URL slugs."""
import re


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower())
'''

_SLUGIFY_V2 = '''"""Turn titles into URL slugs."""
import re


def slugify(text: str) -> str:
    """Lower-case `text`, replace runs of other characters with '-', trim '-' from both ends."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
'''

_TESTS_V1 = """from slugify import slugify


def test_basic():
    assert slugify("Hello World") == "hello-world"


def test_collapses_separators():
    assert slugify("a   b--c") == "a-b-c"
"""

_TESTS_V2 = (
    _TESTS_V1
    + """

def test_trims_edges():
    assert slugify("  Hello, World!  ") == "hello-world"
"""
)


def demo_backend(project_dir: Path) -> ScriptedBackend:
    """A scripted 'team' that builds a tiny slugify module, gets one review round, and fixes it."""
    tests_cmd = f'"{sys.executable}" -m pytest -q'

    plan = Plan(
        title="Slugify helper",
        complexity="small",
        goal="A tiny, well-tested function that turns titles into URL slugs.",
        architecture="One module `slugify.py` using only `re`; tests in `test_slugify.py`.",
        acceptance_criteria=[
            AcceptanceCriterion(id="AC1", text="slugify('Hello World') == 'hello-world'", verify="test_basic"),
            AcceptanceCriterion(
                id="AC2", text="Punctuation at either end never leaves a '-'", verify="test_trims_edges"
            ),
        ],
        work_items=[
            WorkItem(
                id="W1",
                title="slugify module and tests",
                goal="slugify() implemented and covered by tests",
                files=["slugify.py", "test_slugify.py"],
                done_check="python -m pytest -q passes",
            ),
        ],
        test_commands=[tests_cmd],
        run_instructions="python -c \"from slugify import slugify; print(slugify('Hello, World!'))\"",
    )

    def developer(req: AgentRequest) -> AgentResult:
        (req.cwd / "slugify.py").write_text(_SLUGIFY_V1, encoding="utf-8")
        (req.cwd / "test_slugify.py").write_text(_TESTS_V1, encoding="utf-8")
        report = DevReport(
            status="done",
            summary="Implemented slugify with two tests.",
            changed=["slugify.py: new", "test_slugify.py: new"],
            verified=["python -m pytest -q -> 2 passed"],
        )
        return ok(report, cost=0.0, turns=5)

    def developer_fix(req: AgentRequest) -> AgentResult:
        (req.cwd / "slugify.py").write_text(_SLUGIFY_V2, encoding="utf-8")
        (req.cwd / "test_slugify.py").write_text(_TESTS_V2, encoding="utf-8")
        report = DevReport(
            status="done",
            summary="Trim separators at both ends; added a regression test.",
            changed=["slugify.py: strip('-')", "test_slugify.py: test_trims_edges"],
            verified=["python -m pytest -q -> 3 passed"],
        )
        return ok(report, cost=0.0, turns=3)

    qa = QAReport(
        verdict="PASS",
        summary="Behaves as specified.",
        criteria=[
            QACriterion(id="AC1", status="pass", evidence="slugify('Hello World') -> 'hello-world'"),
            QACriterion(id="AC2", status="untested", evidence="not exercised yet"),
        ],
    )
    first_review = ReviewReport(
        verdict="REQUEST_CHANGES",
        summary="Works for simple titles but violates AC2.",
        findings=[
            Finding(
                severity="major",
                location="slugify.py:6",
                problem="slugify('  Hi!  ') returns '-hi-': separators at the ends are kept.",
                fix="Call .strip('-') on the result and add a regression test.",
            )
        ],
    )
    second_review = ReviewReport(verdict="APPROVE", summary="Edge cases are handled and tested.", findings=[])

    return ScriptedBackend(
        {
            "researcher": [
                ok(
                    text="## Recommendation\nNo external libraries needed: `re` from the standard library is enough.",
                    cost=0.0,
                )
            ],
            "architect": [ok(plan, cost=0.0)],
            "developer": [developer, developer_fix],
            "tester": [ok(qa, cost=0.0)],
            "reviewer": [ok(first_review, cost=0.0), ok(second_review, cost=0.0)],
        }
    )
