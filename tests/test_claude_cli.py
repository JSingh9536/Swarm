from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import swarm.claude_cli as cc
from swarm.claude_cli import AuthInfo, auth_info, cli_version, find_cli, looks_like_auth_error

AUTH_VARS = (
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)  # fmt: skip


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- find_cli


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """No real Claude install is visible: fake home, APPDATA, PATH and bundled binary."""
    home = tmp_path / "home"
    appdata = tmp_path / "appdata"
    home.mkdir()
    appdata.mkdir()
    monkeypatch.setattr(cc.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("SWARM_CLAUDE_CLI", raising=False)
    monkeypatch.setattr(cc, "_bundled", lambda: None)
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    return {"home": home, "appdata": appdata, "root": tmp_path}


def test_find_cli_nothing(isolated: dict[str, Path]) -> None:
    assert find_cli() is None
    assert find_cli(str(isolated["root"] / "missing.exe")) is None


def test_find_cli_precedence(isolated: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root = isolated["root"]
    desktop = touch(isolated["appdata"] / "Claude" / "claude-code" / "2.0.1" / "claude.exe")
    assert find_cli() == desktop

    local = touch(isolated["home"] / ".local" / "bin" / "claude")
    assert find_cli() == local

    on_path = touch(root / "path" / "claude.exe")
    monkeypatch.setattr(cc.shutil, "which", lambda name: str(on_path))
    assert find_cli() == on_path

    bundled = touch(root / "sdk" / "_bundled" / "claude.exe")
    monkeypatch.setattr(cc, "_bundled", lambda: bundled)
    assert find_cli() == bundled

    env = touch(root / "env" / "claude.exe")
    monkeypatch.setenv("SWARM_CLAUDE_CLI", str(env))
    assert find_cli() == env

    explicit = touch(root / "explicit" / "claude.exe")
    assert find_cli(str(explicit)) == explicit


def test_find_cli_skips_missing_candidates(isolated: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    local = touch(isolated["home"] / ".local" / "bin" / "claude.exe")
    monkeypatch.setenv("SWARM_CLAUDE_CLI", str(isolated["root"] / "gone.exe"))
    assert find_cli(str(isolated["root"] / "also-gone.exe")) == local


def test_find_cli_newest_desktop_version(isolated: dict[str, Path]) -> None:
    base = isolated["appdata"] / "Claude" / "claude-code"
    touch(base / "2.0.9" / "claude.exe")
    newest = touch(base / "2.0.10" / "claude.exe")
    touch(base / "1.9.99" / "claude.exe")
    (base / "3.0.0").mkdir()  # newer folder without a binary is skipped
    assert find_cli() == newest


# --------------------------------------------------------------------------- version / auth


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.fixture
def no_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in AUTH_VARS:
        monkeypatch.delenv(var, raising=False)


def test_cli_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "_run", lambda cmd, timeout=0: completed("2.1.0 (Claude Code)\n"))
    assert cli_version(Path("claude")) == "2.1.0 (Claude Code)"
    monkeypatch.setattr(cc, "_run", lambda cmd, timeout=0: completed("", 1))
    assert cli_version(Path("claude")) is None
    monkeypatch.setattr(cc, "_run", lambda cmd, timeout=0: None)
    assert cli_version(Path("claude")) is None


def test_run_handles_missing_executable(tmp_path: Path) -> None:
    assert cc._run([str(tmp_path / "definitely-missing.exe")]) is None


@pytest.mark.parametrize(
    ("var", "method"),
    [
        ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
        ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"),
        ("CLAUDE_CODE_USE_BEDROCK", "Amazon Bedrock"),
        ("CLAUDE_CODE_USE_VERTEX", "Google Vertex AI"),
        ("CLAUDE_CODE_USE_FOUNDRY", "Microsoft Foundry"),
    ],
)
def test_auth_from_env(no_auth_env: None, monkeypatch: pytest.MonkeyPatch, var: str, method: str) -> None:
    monkeypatch.setenv(var, "1")
    monkeypatch.setattr(cc, "_run", lambda *a, **k: pytest.fail("must not run the CLI"))
    info = auth_info(None)
    assert info.ok and info.method == method


def test_api_key_wins(no_auth_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    assert auth_info(None).method == "ANTHROPIC_API_KEY"


def test_auth_no_cli(no_auth_env: None) -> None:
    assert auth_info(None) == AuthInfo(False, "none", "Claude Code executable not found")


@pytest.mark.parametrize(
    ("proc", "expected"),
    [
        (completed(json.dumps({"loggedIn": True, "authMethod": "claude.ai"})),
         AuthInfo(True, "claude.ai", "logged in via `claude auth login`")),
        (completed(json.dumps({"loggedIn": True})), AuthInfo(True, "login", "logged in via `claude auth login`")),
        (completed(json.dumps({"loggedIn": False})), AuthInfo(False, "none", "not logged in")),
        (completed("Error: unknown command", 1), AuthInfo(False, "unknown", "Error: unknown command")),
        (completed("", 1, "boom"), AuthInfo(False, "unknown", "boom")),
        (completed("", 1), AuthInfo(False, "unknown", "no output")),
        (None, AuthInfo(False, "unknown", "could not run `claude auth status`")),
    ],
)  # fmt: skip
def test_auth_status(
    no_auth_env: None, monkeypatch: pytest.MonkeyPatch, proc: object, expected: AuthInfo
) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: float = 0) -> object:
        seen.append(cmd)
        return proc

    monkeypatch.setattr(cc, "_run", fake_run)
    assert auth_info(Path("claude")) == expected
    assert seen == [["claude", "auth", "status"]]


def test_auth_detail_truncated(no_auth_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "_run", lambda cmd, timeout=0: completed("x" * 500, 1))
    assert len(auth_info(Path("claude")).detail) == 200


@pytest.mark.parametrize(
    "text",
    [
        "Error: Not logged in. Please run /login",
        "Invalid API key · Fix external API key",
        "API Error: 401 authentication_error",
        "OAuth token has expired",
    ],
)
def test_auth_errors_detected(text: str) -> None:
    assert looks_like_auth_error(text)


@pytest.mark.parametrize("text", ["", "Tests failed: 3 errors", "rate limit exceeded", "permission denied"])
def test_other_errors_not_auth(text: str) -> None:
    assert not looks_like_auth_error(text)


def test_login_help_mentions_both_options() -> None:
    assert "claude auth login" in cc.LOGIN_HELP
    assert "ANTHROPIC_API_KEY" in cc.LOGIN_HELP
