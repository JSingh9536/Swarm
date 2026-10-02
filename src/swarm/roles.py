"""Role definitions.

A role is a Markdown file with simple frontmatter - the same format Claude Code uses for
subagents - so one file serves both the interactive team (`.claude/agents/`) and this
orchestrator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BUILTIN_TOOLS = frozenset(
    {
        "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "Bash", "PowerShell",
        "WebSearch", "WebFetch", "Agent", "Skill", "TodoWrite",
    }
)  # fmt: skip


class RoleError(ValueError):
    """A role file is missing or malformed."""


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    prompt: str
    tools: tuple[str, ...]
    model: str | None
    max_turns: int | None
    path: Path

    @property
    def builtin_tools(self) -> list[str]:
        return [t for t in self.tools if not t.startswith("mcp__")]

    @property
    def mcp_servers(self) -> list[str]:
        """Names of MCP servers this role may use (`mcp__grep` or `mcp__grep__tool` -> `grep`)."""
        names: list[str] = []
        for tool in self.tools:
            if tool.startswith("mcp__"):
                server = tool.split("__")[1]
                if server and server not in names:
                    names.append(server)
        return names

    @property
    def can_write(self) -> bool:
        return any(t in self.tools for t in ("Write", "Edit", "MultiEdit", "NotebookEdit"))


def repo_root() -> Path:
    """The swarm checkout (contains `.claude/agents`); works for editable installs."""
    return Path(__file__).resolve().parents[2]


def default_roles_dir() -> Path:
    override = os.environ.get("SWARM_ROLES_DIR")
    return Path(override) if override else repo_root() / ".claude" / "agents"


def parse_role(path: Path) -> Role:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise RoleError(f"{path.name}: must start with '---' frontmatter")
    end = text.find("\n---", 3)
    if end == -1:
        raise RoleError(f"{path.name}: frontmatter is not closed with '---'")
    front = text[3:end].strip("\r\n")
    body = text[end + 4 :].lstrip("\r\n").rstrip() + "\n"

    fields: dict[str, str] = {}
    for line in front.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise RoleError(f"{path.name}: bad frontmatter line {line!r}")
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip("\"'")

    name = fields.get("name", "")
    if not name:
        raise RoleError(f"{path.name}: missing 'name'")
    if name != path.stem:
        raise RoleError(f"{path.name}: name {name!r} must match the file name")
    if not fields.get("description"):
        raise RoleError(f"{path.name}: missing 'description'")
    if not body.strip():
        raise RoleError(f"{path.name}: empty prompt body")

    tools = tuple(t.strip() for t in fields.get("tools", "").split(",") if t.strip())
    for tool in tools:
        if not tool.startswith("mcp__") and tool not in BUILTIN_TOOLS:
            raise RoleError(f"{path.name}: unknown tool {tool!r}")

    max_turns: int | None = None
    if "maxTurns" in fields:
        try:
            max_turns = int(fields["maxTurns"])
        except ValueError as exc:
            raise RoleError(f"{path.name}: maxTurns must be an integer") from exc

    model = fields.get("model") or None
    if model == "inherit":
        model = None
    return Role(name, fields["description"], body, tools, model, max_turns, path)


def load_roles(directory: Path | None = None) -> dict[str, Role]:
    directory = directory or default_roles_dir()
    if not directory.is_dir():
        raise RoleError(f"roles directory not found: {directory}")
    roles: dict[str, Role] = {}
    for path in sorted(directory.glob("*.md")):
        role = parse_role(path)
        roles[role.name] = role
    if not roles:
        raise RoleError(f"no role files in {directory}")
    return roles
