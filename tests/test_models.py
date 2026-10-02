from __future__ import annotations

import pytest
from pydantic import ValidationError

from swarm.models import DevReport, Finding, Plan, QAReport, ReviewReport, blocking


def test_blocking_filters_severity() -> None:
    findings = [
        Finding(severity="nit", problem="a"),
        Finding(severity="major", problem="b"),
        Finding(severity="minor", problem="c"),
        Finding(severity="blocker", problem="d"),
    ]
    assert [f.problem for f in blocking(findings)] == ["b", "d"]
    assert blocking([]) == []


def test_plan_round_trip() -> None:
    plan = Plan.model_validate(
        {
            "title": "CLI",
            "complexity": "small",
            "goal": "Add a CLI",
            "acceptance_criteria": [{"id": "AC1", "text": "prints help"}],
            "work_items": [{"id": "W1", "title": "cli", "goal": "cli exists", "files": ["cli.py"]}],
        }
    )
    assert plan.security_relevant is False
    assert plan.work_items[0].depends_on == []
    assert Plan.model_validate_json(plan.model_dump_json()) == plan


def test_invalid_literals_rejected() -> None:
    with pytest.raises(ValidationError):
        ReviewReport(verdict="LGTM")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        DevReport(status="finished")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Finding(severity="critical", problem="x")  # type: ignore[arg-type]


@pytest.mark.parametrize("model", [Plan, DevReport, ReviewReport, QAReport])
def test_json_schema_generates(model: type) -> None:
    schema = model.model_json_schema()
    assert schema["type"] == "object"
    assert schema["properties"]
