from __future__ import annotations

import json
import subprocess
import urllib.error
from typing import Any

import pytest

import swarm.mcp as mcp
from swarm.mcp import CATALOG, DEFAULT_ENABLED, allow_patterns, probe, sdk_config


def test_catalog_is_https() -> None:
    assert set(CATALOG) == {"grep", "deepwiki", "context7", "rea"}
    for server in CATALOG.values():
        assert server.local or server.url.startswith("https://")


def test_local_servers_are_opt_in() -> None:
    """A local server runs code on this machine, so it is never enabled by default."""
    assert set(DEFAULT_ENABLED) == {"grep", "deepwiki", "context7"}
    assert CATALOG["rea"].local and not CATALOG["grep"].local
    assert "rea" not in DEFAULT_ENABLED


def test_rea_is_pinned_to_one_version() -> None:
    package = CATALOG["rea"].command[2]
    assert package == f"rea-agents@{mcp.REA_VERSION}"
    assert "latest" not in package and mcp.REA_VERSION.count(".") == 2


def test_launch_command_wraps_npx_on_windows() -> None:
    rea = CATALOG["rea"]
    assert mcp.launch_command(rea, "win32") == ["cmd", "/c", "npx", "-y", f"rea-agents@{mcp.REA_VERSION}", "mcp"]
    assert mcp.launch_command(rea, "linux") == ["npx", "-y", f"rea-agents@{mcp.REA_VERSION}", "mcp"]


@pytest.mark.parametrize(
    ("platform", "command", "prefix"), [("linux", "npx", []), ("darwin", "npx", []), ("win32", "cmd", ["/c", "npx"])]
)
def test_sdk_config_local_server(
    monkeypatch: pytest.MonkeyPatch, platform: str, command: str, prefix: list[str]
) -> None:
    monkeypatch.setattr(mcp.sys, "platform", platform)
    cfg = sdk_config(["grep", "rea"])
    assert cfg["grep"]["type"] == "http"
    assert cfg["rea"] == {
        "type": "stdio", "command": command, "args": [*prefix, "-y", f"rea-agents@{mcp.REA_VERSION}", "mcp"],
    }  # fmt: skip


# --------------------------------------------------------------------------- stdio probe (no process started)


def fake_run(monkeypatch: pytest.MonkeyPatch, outcome: str | Exception) -> list[Any]:
    calls: list[Any] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kw))
        if isinstance(outcome, Exception):
            raise outcome
        return subprocess.CompletedProcess(argv, 0, stdout=outcome, stderr="")

    monkeypatch.setattr(mcp.subprocess, "run", run)
    return calls


def test_probe_stdio_reads_initialize_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "rea", "version": "3.2.1"}}})
    calls = fake_run(monkeypatch, "npm notice: something\n" + reply + "\n")
    assert mcp.probe_stdio(["npx", "rea"], timeout=5) == (True, "rea 3.2.1")
    argv, kw = calls[0]
    assert argv == ["npx", "rea"] and kw["timeout"] == 5
    assert json.loads(kw["input"])["method"] == "initialize"


def test_probe_stdio_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, "")
    assert mcp.probe_stdio(["x"]) == (False, "no reply")
    fake_run(monkeypatch, "{not json}\n")
    assert mcp.probe_stdio(["x"]) == (False, "unexpected response")
    fake_run(monkeypatch, FileNotFoundError("npx"))
    ok, detail = mcp.probe_stdio(["x"])
    assert not ok and "FileNotFoundError" in detail
    fake_run(monkeypatch, subprocess.TimeoutExpired(["x"], 7))
    assert mcp.probe_stdio(["x"], timeout=7) == (False, "no reply within 7s")


def test_probe_stdio_timeout_after_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that answers but keeps running until the timeout still counts as reachable."""
    reply = json.dumps({"result": {"serverInfo": {"name": "rea", "version": "3.2.1"}}}).encode()
    fake_run(monkeypatch, subprocess.TimeoutExpired(["x"], 7, output=reply))
    assert mcp.probe_stdio(["x"], timeout=7) == (True, "rea 3.2.1")


def test_check_dispatches_by_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(mcp, "probe", lambda url, timeout=0: seen.append("http") or (True, "h"))
    monkeypatch.setattr(mcp, "probe_stdio", lambda argv, timeout=0: seen.append("stdio") or (True, "s"))
    assert mcp.check(CATALOG["grep"]) == (True, "h")
    assert mcp.check(CATALOG["rea"]) == (True, "s")
    assert seen == ["http", "stdio"]


def test_sdk_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTEXT7_API_KEY", raising=False)
    cfg = sdk_config(["grep", "context7"])
    assert cfg == {
        "grep": {"type": "http", "url": CATALOG["grep"].url},
        "context7": {"type": "http", "url": CATALOG["context7"].url},
    }
    assert sdk_config([]) == {}


def test_context7_key_only_for_context7(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTEXT7_API_KEY", "k-123")
    cfg = sdk_config(DEFAULT_ENABLED)
    assert cfg["context7"]["headers"] == {"Authorization": "Bearer k-123"}
    assert "headers" not in cfg["grep"]
    assert "headers" not in cfg["deepwiki"]


def test_unknown_server() -> None:
    with pytest.raises(KeyError, match="github"):
        sdk_config(["grep", "github"])


def test_allow_patterns() -> None:
    assert allow_patterns(["grep", "context7"]) == ["mcp__grep__*", "mcp__context7__*"]
    assert allow_patterns(()) == []


# --------------------------------------------------------------------------- probe (no network)


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.body = body.encode()

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def fake_urlopen(monkeypatch: pytest.MonkeyPatch, body: str | Exception) -> list[Any]:
    calls: list[Any] = []

    def urlopen(req: Any, timeout: float = 0) -> FakeResponse:
        calls.append((req, timeout))
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body)

    monkeypatch.setattr(mcp.urllib.request, "urlopen", urlopen)
    return calls


INIT = {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "grep-mcp", "version": "1.2.3"}}}


def test_probe_plain_json(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_urlopen(monkeypatch, json.dumps(INIT))
    assert probe("https://mcp.example", timeout=3) == (True, "grep-mcp 1.2.3")
    req, timeout = calls[0]
    assert timeout == 3
    assert req.get_method() == "POST"
    assert json.loads(req.data)["method"] == "initialize"
    assert "text/event-stream" in req.get_header("Accept")


def test_probe_sse(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_urlopen(monkeypatch, f"event: message\ndata: {json.dumps(INIT)}\n\n")
    assert probe("https://mcp.example") == (True, "grep-mcp 1.2.3")


def test_probe_missing_version(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_urlopen(monkeypatch, json.dumps({"result": {"serverInfo": {"name": "x"}}}))
    assert probe("https://mcp.example") == (True, "x")


@pytest.mark.parametrize("body", ["not json", "{}", json.dumps({"result": None}), "data: [1]"])
def test_probe_unexpected(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    fake_urlopen(monkeypatch, body)
    assert probe("https://mcp.example") == (False, "unexpected response")


@pytest.mark.parametrize(
    "exc", [urllib.error.URLError("no route"), TimeoutError("slow"), ConnectionResetError("reset")]
)
def test_probe_network_errors(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    fake_urlopen(monkeypatch, exc)
    ok, detail = probe("https://mcp.example")
    assert not ok
    assert type(exc).__name__ in detail
