"""Locate the Claude Code executable and check that it can authenticate."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", path.name)) or (0,)


def _bundled() -> Path | None:
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    folder = Path(claude_agent_sdk.__file__).parent / "_bundled"
    for name in ("claude.exe", "claude"):
        if (folder / name).is_file():
            return folder / name
    return None


def find_cli(explicit: str | None = None) -> Path | None:
    """First existing Claude Code binary: explicit, env, SDK-bundled, PATH, ~/.local/bin, desktop app."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("SWARM_CLAUDE_CLI"):
        candidates.append(Path(os.environ["SWARM_CLAUDE_CLI"]))
    if (bundled := _bundled()) is not None:
        candidates.append(bundled)
    if on_path := shutil.which("claude"):
        candidates.append(Path(on_path))
    home = Path.home()
    candidates += [home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata) / "Claude" / "claude-code"
        if base.is_dir():
            versions = sorted((p for p in base.iterdir() if p.is_dir()), key=_version_key, reverse=True)
            candidates += [v / "claude.exe" for v in versions]
    return next((c for c in candidates if c.is_file()), None)


def _run(cmd: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None


def cli_version(cli: Path) -> str | None:
    proc = _run([str(cli), "--version"], 20)
    return proc.stdout.strip() if proc and proc.returncode == 0 else None


@dataclass(frozen=True)
class AuthInfo:
    ok: bool
    method: str
    detail: str


LOGIN_HELP = (
    "Standalone runs need their own login (the desktop app's login is separate). Either:\n"
    "  1. Subscription:  run   claude auth login   in a terminal (opens your browser), or\n"
    "  2. API key:       set   ANTHROPIC_API_KEY   (console.anthropic.com), then reopen the terminal."
)


def auth_info(cli: Path | None) -> AuthInfo:
    """Best-effort check without making a model call."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AuthInfo(True, "ANTHROPIC_API_KEY", "API key from the environment (usage is billed to that key)")
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(var):
            return AuthInfo(True, var, "token from the environment")
    for var, label in (
        ("CLAUDE_CODE_USE_BEDROCK", "Amazon Bedrock"),
        ("CLAUDE_CODE_USE_VERTEX", "Google Vertex AI"),
        ("CLAUDE_CODE_USE_FOUNDRY", "Microsoft Foundry"),
    ):
        if os.environ.get(var):
            return AuthInfo(True, label, "cloud provider credentials are used (not verified here)")
    if cli is None:
        return AuthInfo(False, "none", "Claude Code executable not found")
    proc = _run([str(cli), "auth", "status"], 30)
    if proc is None:
        return AuthInfo(False, "unknown", "could not run `claude auth status`")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return AuthInfo(False, "unknown", (proc.stdout or proc.stderr).strip()[:200] or "no output")
    if data.get("loggedIn"):
        return AuthInfo(True, str(data.get("authMethod", "login")), "logged in via `claude auth login`")
    return AuthInfo(False, "none", "not logged in")


_PLAN_METHODS = frozenset({"claude.ai", "CLAUDE_CODE_OAUTH_TOKEN"})
_METERED_METHODS = frozenset(
    {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "Amazon Bedrock", "Google Vertex AI", "Microsoft Foundry"}
)

METERED_HELP = (
    "This login ({method}) is billed per token, outside your Claude plan, so swarm will not use it by default.\n"
    "To use your plan instead: remove the variable for this terminal (PowerShell: Remove-Item Env:ANTHROPIC_API_KEY),\n"
    "make sure it is not set in Windows environment settings, and run  claude auth login.\n"
    "To knowingly allow metered billing, pass --allow-api-billing (or set allow_api_billing = true in swarm.toml)."
)


def billing_kind(info: AuthInfo) -> str:
    """'plan' = counts against the Claude subscription; 'metered' = billed per token; 'unknown' = can't tell."""
    if not info.ok:
        return "unknown"
    if info.method in _PLAN_METHODS:
        return "plan"
    if info.method in _METERED_METHODS or info.method.lower() in ("console", "api_key", "apikey", "api-key"):
        return "metered"
    return "unknown"


AUTH_ERROR_HINTS = ("not logged in", "please run /login", "invalid api key", "authentication", "oauth token")


def looks_like_auth_error(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in AUTH_ERROR_HINTS)


LIMIT_ERROR_HINTS = (
    "usage limit",
    "limit reached",
    "5-hour limit",
    "weekly limit",
    "rate limit",
    "rate_limit",
    "credit balance",
    "billing_error",
    "out of extra usage",
)


def looks_like_limit_error(text: str) -> bool:
    """True when an error message says the plan's usage limit (or credit) has run out."""
    low = text.lower()
    return any(h in low for h in LIMIT_ERROR_HINTS)
