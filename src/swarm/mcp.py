"""MCP servers that give the researcher access to real-world code and up-to-date docs.

All three are public, hosted, HTTP MCP servers - nothing is installed or executed locally.
Queries sent to them leave the machine, so the guard caps their size (see guard.Policy).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class McpServer:
    name: str
    url: str
    purpose: str


CATALOG: dict[str, McpServer] = {
    "grep": McpServer("grep", "https://mcp.grep.app", "literal code search across public GitHub repos"),
    "deepwiki": McpServer(
        "deepwiki", "https://mcp.deepwiki.com/mcp", "AI-written documentation for indexed public GitHub repos"
    ),
    "context7": McpServer("context7", "https://mcp.context7.com/mcp", "up-to-date library docs and examples"),
}
DEFAULT_ENABLED: tuple[str, ...] = tuple(CATALOG)


def sdk_config(names: list[str] | tuple[str, ...]) -> dict[str, dict]:
    """The `mcp_servers` mapping for ClaudeAgentOptions."""
    servers: dict[str, dict] = {}
    for name in names:
        if name not in CATALOG:
            raise KeyError(f"unknown MCP server {name!r}; known: {', '.join(CATALOG)}")
        cfg: dict = {"type": "http", "url": CATALOG[name].url}
        key = os.environ.get("CONTEXT7_API_KEY") if name == "context7" else None
        if key:
            cfg["headers"] = {"Authorization": f"Bearer {key}"}
        servers[name] = cfg
    return servers


def allow_patterns(names: list[str] | tuple[str, ...]) -> list[str]:
    return [f"mcp__{n}__*" for n in names]


def probe(url: str, timeout: float = 15.0) -> tuple[bool, str]:
    """MCP `initialize` handshake. Returns (reachable, server name/version or error)."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "swarm-doctor", "version": "0"},
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URLs
            body = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    data = next((ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")), body.strip())
    try:
        info = json.loads(data)["result"]["serverInfo"]
    except (ValueError, KeyError, TypeError):
        return False, "unexpected response"
    return True, f"{info.get('name', '?')} {info.get('version', '')}".strip()
