from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

import swarm.mcp as mcp
from swarm.mcp import CATALOG, DEFAULT_ENABLED, allow_patterns, probe, sdk_config


def test_catalog_is_https() -> None:
    assert set(DEFAULT_ENABLED) == set(CATALOG) == {"grep", "deepwiki", "context7"}
    for server in CATALOG.values():
        assert server.url.startswith("https://")


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
