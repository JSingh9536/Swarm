from __future__ import annotations

from pathlib import Path

import pytest

from swarm.roles import RoleError, default_roles_dir, load_roles, parse_role

VALID = """---
name: helper
description: Helps with things
tools: Read, Grep, Bash, mcp__grep, mcp__context7__query-docs, mcp__grep__searchGitHub
model: sonnet
maxTurns: 12
color: green
---

You are a helper.
"""


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_valid(tmp_path: Path) -> None:
    role = parse_role(write(tmp_path, "helper", VALID))
    assert role.name == "helper"
    assert role.description == "Helps with things"
    assert role.prompt == "You are a helper.\n"
    assert role.model == "sonnet"
    assert role.max_turns == 12
    assert role.builtin_tools == ["Read", "Grep", "Bash"]
    assert role.mcp_servers == ["grep", "context7"]
    assert not role.can_write


def test_crlf_and_quotes(tmp_path: Path) -> None:
    text = VALID.replace("description: Helps with things", 'description: "Quoted: yes"').replace("\n", "\r\n")
    role = parse_role(write(tmp_path, "helper", text))
    assert role.description == "Quoted: yes"
    assert role.prompt.strip() == "You are a helper."


def test_inherit_model_and_defaults(tmp_path: Path) -> None:
    role = parse_role(write(tmp_path, "lead", "---\nname: lead\ndescription: d\nmodel: inherit\n---\nbody\n"))
    assert role.model is None
    assert role.max_turns is None
    assert role.tools == ()


def test_can_write(tmp_path: Path) -> None:
    role = parse_role(write(tmp_path, "dev", "---\nname: dev\ndescription: d\ntools: Read, Edit\n---\nbody\n"))
    assert role.can_write


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("no frontmatter\n", "must start"),
        ("---\nname: helper\ndescription: d\n", "not closed"),
        ("---\nname: other\ndescription: d\n---\nbody\n", "must match"),
        ("---\ndescription: d\n---\nbody\n", "missing 'name'"),
        ("---\nname: helper\n---\nbody\n", "missing 'description'"),
        ("---\nname: helper\ndescription: d\n---\n\n", "empty prompt"),
        ("---\nname: helper\ndescription: d\ntools: Read, Teleport\n---\nbody\n", "unknown tool"),
        ("---\nname: helper\ndescription: d\nmaxTurns: lots\n---\nbody\n", "maxTurns"),
        ("---\nname: helper\njunk line\n---\nbody\n", "bad frontmatter"),
    ],
)
def test_parse_errors(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(RoleError, match=message):
        parse_role(write(tmp_path, "helper", text))


def test_load_roles_errors(tmp_path: Path) -> None:
    with pytest.raises(RoleError, match="not found"):
        load_roles(tmp_path / "missing")
    with pytest.raises(RoleError, match="no role files"):
        load_roles(tmp_path)


def test_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_ROLES_DIR", str(tmp_path))
    assert default_roles_dir() == tmp_path


def test_shipped_roles_are_valid() -> None:
    roles = load_roles()
    expected = {
        "architect", "debugger", "developer", "docs-writer", "researcher",
        "reviewer", "security-auditor", "tech-lead", "tester",
    }  # fmt: skip
    assert expected <= set(roles)
    assert not roles["reviewer"].can_write
    assert not roles["security-auditor"].can_write
    assert roles["developer"].can_write
    assert "WebFetch" in roles["researcher"].tools
    for name, role in roles.items():
        if name != "researcher":
            assert "WebFetch" not in role.tools, f"{name} must not have web access"
