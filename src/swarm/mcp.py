"""MCP servers the team may use.

The default three are public, hosted, HTTP servers that give the researcher real-world code and up-to-date docs -
nothing is installed or executed locally. Queries sent to them leave the machine, so the guard caps their size
(see guard.Policy).

`rea` (reverse engineering, used by the reverse-engineer role) is different: a local stdio server that `npx`
downloads and runs on this machine. It is pinned to one exact version and is opt-in (`[mcp] enabled`), never a
default.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

REA_VERSION = "3.2.1"


@dataclass(frozen=True)
class McpServer:
    name: str
    url: str  # "" for a local server
    purpose: str
    command: tuple[str, ...] = ()  # set for a local stdio server

    @property
    def local(self) -> bool:
        return bool(self.command)


CATALOG: dict[str, McpServer] = {
    "grep": McpServer("grep", "https://mcp.grep.app", "literal code search across public GitHub repos"),
    "deepwiki": McpServer(
        "deepwiki", "https://mcp.deepwiki.com/mcp", "AI-written documentation for indexed public GitHub repos"
    ),
    "context7": McpServer("context7", "https://mcp.context7.com/mcp", "up-to-date library docs and examples"),
    "rea": McpServer(
        "rea",
        "",
        "local reverse engineering of binaries, app bundles, archives and JavaScript apps (runs on this machine)",
        ("npx", "-y", f"rea-agents@{REA_VERSION}", "mcp"),
    ),
}
DEFAULT_ENABLED: tuple[str, ...] = tuple(name for name, server in CATALOG.items() if not server.local)


def launch_command(server: McpServer, platform: str | None = None) -> list[str]:
    """argv for a local server. On Windows `npx` is a .cmd shim, which only starts through cmd.exe."""
    if (platform or sys.platform) == "win32" and server.command[0] == "npx":
        return ["cmd", "/c", *server.command]
    return list(server.command)


def sdk_config(names: list[str] | tuple[str, ...]) -> dict[str, dict]:
    """The `mcp_servers` mapping for ClaudeAgentOptions."""
    servers: dict[str, dict] = {}
    for name in names:
        if name not in CATALOG:
            raise KeyError(f"unknown MCP server {name!r}; known: {', '.join(CATALOG)}")
        server = CATALOG[name]
        if server.local:
            argv = launch_command(server)
            servers[name] = {"type": "stdio", "command": argv[0], "args": argv[1:]}
            continue
        cfg: dict = {"type": "http", "url": server.url}
        key = os.environ.get("CONTEXT7_API_KEY") if name == "context7" else None
        if key:
            cfg["headers"] = {"Authorization": f"Bearer {key}"}
        servers[name] = cfg
    return servers


def allow_patterns(names: list[str] | tuple[str, ...]) -> list[str]:
    return [f"mcp__{n}__*" for n in names]


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "swarm-doctor", "version": "0"},
    },
}


def _server_info(data: str) -> tuple[bool, str]:
    try:
        info = json.loads(data)["result"]["serverInfo"]
    except (ValueError, KeyError, TypeError):
        return False, "unexpected response"
    return True, f"{info.get('name', '?')} {info.get('version', '')}".strip()


def check(server: McpServer, timeout: float = 15.0) -> tuple[bool, str]:
    """Probe a catalog server: an HTTP handshake, or start a local one and handshake over stdio."""
    return probe_stdio(launch_command(server), timeout) if server.local else probe(server.url, timeout)


def probe(url: str, timeout: float = 15.0) -> tuple[bool, str]:
    """MCP `initialize` handshake over HTTP. Returns (reachable, server name/version or error)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(_INITIALIZE).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URLs
            body = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    data = next((ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")), body.strip())
    return _server_info(data)


def probe_stdio(argv: list[str], timeout: float = 60.0) -> tuple[bool, str]:
    """Start a local MCP server, send `initialize`, read its reply, and stop it."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv comes from the fixed catalog
            argv,
            input=json.dumps(_INITIALIZE) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        out = proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        out = partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial
        if not out.strip():
            return False, f"no reply within {timeout:.0f}s"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    line = next((ln for ln in out.splitlines() if ln.lstrip().startswith("{")), "")
    return _server_info(line) if line else (False, "no reply")
