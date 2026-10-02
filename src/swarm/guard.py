"""Guardrails for unattended agent runs.

This is defense in depth, NOT a sandbox. It blocks the obvious ways an agent - or a prompt
injection that reached it - could leak secrets, publish code, reach the network from a shell,
or damage the machine. Anything that runs code (python, node, make, ...) can still do harm, so
run untrusted repositories inside a VM or container.

The policy is pure (tool name + input -> Decision), so it is unit-tested without the SDK and
plugged in as a PreToolUse hook by `make_hooks`.
"""

from __future__ import annotations

import ipaddress
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


ALLOW = Decision(True)


def deny(reason: str) -> Decision:
    return Decision(False, reason)


# --------------------------------------------------------------------------- paths

_SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".azure", ".kube", ".gcloud", ".docker"})
_SECRET_FILE_RE = re.compile(
    r"^(?:"
    r"\.env(?:\.[^/\\]+)?"
    r"|\.netrc|_netrc|\.npmrc|\.pypirc|\.git-credentials|\.pgpass"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)"
    r"|.+\.(?:pem|key|pfx|p12|kdbx|ovpn)"
    r"|\.?credentials(?:\.json)?"
    r"|service[-_]?account.*\.json"
    r")$",
    re.IGNORECASE,
)
_ENV_TEMPLATE_RE = re.compile(r"^\.env\.(?:example|sample|template|dist|defaults?)$", re.IGNORECASE)
_SECRET_FRAGMENTS = (
    "appdata/local/google/chrome/user data",
    "appdata/local/microsoft/edge/user data",
    "appdata/roaming/mozilla/firefox/profiles",
    "appdata/local/microsoft/credentials",
    "appdata/roaming/microsoft/credentials",
    "appdata/roaming/microsoft/protect",
    "library/keychains",
)
_PATH_KEYS = {
    "Read": ("file_path",),
    "NotebookRead": ("notebook_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("path",),
    "Grep": ("path",),
}
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _inside(path: Path | str, root: Path | str) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def _msys_to_windows(token: str) -> str:
    """Git Bash style `/c/Users/x` -> `C:/Users/x` (no-op elsewhere)."""
    if os.name == "nt":
        m = re.match(r"^/([A-Za-z])(?:/|$)", token)
        if m:
            return f"{m.group(1).upper()}:{token[2:] or '/'}"
    return token


# --------------------------------------------------------------------------- shell parsing

_HEREDOC_MARK = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_]\w*)\1")
_REDIRECT_RE = re.compile(r"(?<![<>])(?:\d|&)?>{1,2}(?!&)\s*([^\s;|&<>()]+)")
_TOKEN_RE = re.compile(r"\"([^\"]*)\"|'([^']*)'|(\S+)")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = frozenset({"env", "command", "builtin", "nohup", "time", "exec", "nice", "stdbuf", "timeout"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "cmd", "powershell", "pwsh"})


def strip_heredocs(cmd: str) -> str:
    """Remove heredoc bodies so their text is not mistaken for commands."""
    out: list[str] = []
    pending: list[str] = []
    for line in cmd.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        out.append(line)
        pending.extend(m.group(2) for m in _HEREDOC_MARK.finditer(line))
    return "\n".join(out)


def split_segments(cmd: str) -> list[str]:
    """Split a shell command on unquoted `&& || ; | & ( ) `` and newlines."""
    segments: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    i, n = 0, len(cmd)

    def flush() -> None:
        text = "".join(cur).strip()
        if text:
            segments.append(text)
        cur.clear()

    while i < n:
        c = cmd[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                cur.append(cmd[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
            cur.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            nxt = cmd[i + 1]
            if nxt == "\n":
                i += 2
                continue
            if nxt in ";&|()`":
                cur.append(c + nxt)
                i += 2
                continue
        if cmd.startswith(("&&", "||"), i):
            flush()
            i += 2
            continue
        if c == "&" and ((cur and cur[-1] == ">") or cmd.startswith("&>", i)):
            cur.append(c)  # part of a redirection such as 2>&1 or &>file
            i += 1
            continue
        if c == "|" and cmd.startswith("|&", i):
            flush()
            i += 2
            continue
        if c in ";|&\n()`":
            flush()
            i += 1
            continue
        cur.append(c)
        i += 1
    flush()
    return segments


def tokenize(segment: str) -> list[str]:
    """Whitespace tokens with quotes removed; backslashes stay literal (Windows paths)."""
    return [a or b or c for a, b, c in _TOKEN_RE.findall(segment)]


def blank_quotes(segment: str) -> str:
    """Replace quoted text with spaces so shell operators are only seen where they are real."""
    out: list[str] = []
    quote: str | None = None
    for ch in segment:
        if quote:
            out.append(" ")
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _command_tokens(tokens: list[str]) -> list[str]:
    """Drop leading VAR=value assignments and transparent wrappers (env, nohup, ...)."""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if _ENV_ASSIGN_RE.match(t):
            i += 1
        elif os.path.basename(t).lower().removesuffix(".exe") in _WRAPPERS:
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-") or _ENV_ASSIGN_RE.match(tokens[i])):
                i += 1
        else:
            break
    return tokens[i:]


def _verb(tokens: list[str]) -> str:
    return os.path.basename(tokens[0]).lower().removesuffix(".exe") if tokens else ""


def _is_flag(tok: str) -> bool:
    return bool(re.fullmatch(r"-{1,2}[A-Za-z][\w-]*(?::\S*)?|-\d+|/[A-Za-z?](?::\S+)?", tok))


# --------------------------------------------------------------------------- deny tables

_DENIED_VERBS = {
    "sudo": "privilege escalation is blocked",
    "su": "privilege escalation is blocked",
    "doas": "privilege escalation is blocked",
    "runas": "privilege escalation is blocked",
    "ssh": "remote shells and transfers are blocked",
    "scp": "remote shells and transfers are blocked",
    "sftp": "remote shells and transfers are blocked",
    "ftp": "remote shells and transfers are blocked",
    "tftp": "remote shells and transfers are blocked",
    "telnet": "raw network tools are blocked",
    "nc": "raw network tools are blocked",
    "ncat": "raw network tools are blocked",
    "netcat": "raw network tools are blocked",
    "socat": "raw network tools are blocked",
    "reg": "registry edits are blocked",
    "schtasks": "scheduled tasks are blocked",
    "netsh": "network configuration is blocked",
    "bcdedit": "boot configuration is blocked",
    "setx": "persistent environment changes are blocked",
    "wmic": "WMI is blocked",
    "certutil": "certutil (download/encode abuse) is blocked",
    "bitsadmin": "BITS transfers are blocked",
    "mshta": "script hosts are blocked",
    "regsvr32": "script hosts are blocked",
    "rundll32": "script hosts are blocked",
    "cscript": "script hosts are blocked",
    "wscript": "script hosts are blocked",
    "icacls": "permission changes are blocked",
    "cacls": "permission changes are blocked",
    "takeown": "permission changes are blocked",
    "diskpart": "disk tools are blocked",
    "format": "disk tools are blocked",
    "mkfs": "disk tools are blocked",
    "dd": "raw disk copies are blocked",
    "shutdown": "power control is blocked",
    "reboot": "power control is blocked",
    "halt": "power control is blocked",
    "powercfg": "power configuration is blocked",
    "pkill": "killing processes by name is blocked",
    "killall": "killing processes by name is blocked",
    "set-executionpolicy": "changing PowerShell policy is blocked",
    "new-service": "creating services is blocked",
    "register-scheduledtask": "scheduled tasks are blocked",
    "iex": "Invoke-Expression is blocked",
    "invoke-expression": "Invoke-Expression is blocked",
    "gh": "the GitHub CLI can publish or change remote state; ask the human",
    "aws": "cloud CLIs are blocked",
    "az": "cloud CLIs are blocked",
    "gcloud": "cloud CLIs are blocked",
    "kubectl": "cloud CLIs are blocked",
    "helm": "cloud CLIs are blocked",
    "terraform": "infrastructure tools are blocked",
    "pulumi": "infrastructure tools are blocked",
    "heroku": "deployment CLIs are blocked",
    "vercel": "deployment CLIs are blocked",
    "netlify": "deployment CLIs are blocked",
    "flyctl": "deployment CLIs are blocked",
    "firebase": "deployment CLIs are blocked",
    "wrangler": "deployment CLIs are blocked",
    "twine": "publishing packages is blocked",
}

_DELETE_VERBS = frozenset({"rm", "rmdir", "unlink", "shred", "del", "erase", "rd", "remove-item", "ri"})
_MOVE_VERBS = frozenset({"mv", "move", "move-item", "mi"})
_COPY_VERBS = frozenset({"cp", "copy", "copy-item", "cpi", "xcopy", "install", "rsync"})
_CREATE_VERBS = frozenset(
    {"mkdir", "md", "touch", "new-item", "ni", "tee", "truncate", "ln", "mklink",
     "set-content", "add-content", "out-file"}
)  # fmt: skip
_FETCH_VERBS = frozenset({"curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod", "start-bitstransfer"})
_SHELL_VERBS = (
    r"(?:sh|bash|zsh|dash|ksh|fish|python[\d.]*|node|perl|ruby|php|iex|invoke-expression|powershell|pwsh|cmd)"
)

_DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE), why)
    for p, why in [
        (rf"\b(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n]*\|\s*{_SHELL_VERBS}\b",
         "piping a download into an interpreter is blocked"),
        (rf"\b{_SHELL_VERBS}\s+-c\s+[\"']?\$\(\s*(?:curl|wget)", "running downloaded code is blocked"),
        (r"\b(?:powershell|pwsh)(?:\.exe)?\b[^\n]*\s-(?:e|ec|enc|encodedcommand)\b",
         "encoded PowerShell is blocked"),
        (r"\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}", "fork bombs are blocked"),
        (r"\bchmod\s+(?:-\w+\s+)*[0-7]*777\b[^\n]*\s(?:/|~|\$HOME|%USERPROFILE%)(?:\s|$)",
         "mass permission changes are blocked"),
        (r"(?:\$\{?|%|\$env:|environ\[\s*[\"']|getenv\(\s*[\"'])(?:ANTHROPIC|CLAUDE|OPENAI|GITHUB|GH|AWS|"
         r"AZURE|GOOGLE|GCP|NPM|PYPI|SLACK|STRIPE|HF|HUGGINGFACE|DATABASE|DB|SECRET|API|AUTH|ACCESS)"
         r"[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)\b",
         "reading secret environment variables is blocked"),
        (r"(?:^|[;&|(]\s*)(?:printenv|env)\s*(?:$|[|;&>])", "dumping the environment is blocked"),
        (r"(?:^|[;&|(]\s*)set\s*(?:$|[|;&>])", "dumping the environment is blocked"),
        (r"\b(?:get-childitem|gci|dir|ls)\s+env:", "dumping the environment is blocked"),
        (r"(?:^|[\s\"'/\\=:])\.env(?!\.(?:example|sample|template|dist|defaults?)\b)(?:\.\w+)?(?=$|[\s\"';|&)])",
         "reading .env files is blocked"),
        (r"(?:^|[\s\"'/\\=])(?:\.ssh|\.aws|\.gnupg|\.azure|\.kube|\.gcloud)(?=$|[\s\"'/\\;|&)])",
         "credential directories are off limits"),
        (r"\b(?:\.netrc|_netrc|\.npmrc|\.pypirc|\.git-credentials|\.pgpass|id_rsa|id_ed25519|"
         r"\.credentials\.json)\b", "credential files are off limits"),
        (r"[\\/]\.claude(?:\.json|[\\/])", "Claude Code configuration is off limits"),
        (r"\b(?:cookies|login data|web data)\b[^\n]*\b(?:sqlite|db)\b", "browser stores are off limits"),
        (r"\bdocker(?:\.exe)?\s+[^\n|;&]*(?:--privileged|\bpush\b|\blogin\b)", "unsafe docker usage is blocked"),
        (r"\b(?:npm|pnpm|yarn|bun)\s+(?:publish|login|adduser|token|unpublish|deprecate|owner|access)\b",
         "publishing to registries is blocked"),
        (r"\b(?:cargo)\s+(?:publish|login|owner|yank)\b", "publishing to registries is blocked"),
        (r"\bgem\s+(?:push|yank|owner)\b", "publishing to registries is blocked"),
        (r"\b(?:poetry|uv|hatch|flit)\s+publish\b", "publishing to registries is blocked"),
        (r"\b(?:npm|pnpm|yarn)\s+config\s+(?:set|edit|delete)\b", "changing package manager config is blocked"),
        (r"\bpip[\d.]*\s+config\s+(?:set|edit|unset)\b", "changing package manager config is blocked"),
        (r"\bPIP_[A-Z_]+\s*=", "setting pip environment variables is blocked (pip is locked to virtualenvs)"),
        (r"\brobocopy\b[^\n]*\s/(?:mir|purge)\b", "mirroring/purging copies are blocked"),
    ]
]  # fmt: skip

VENV_HELP = (
    "packages must be installed into the project's own virtualenv, never globally: create it with "
    "`python -m venv .venv`, then run `.venv/Scripts/python -m pip install <name>` (Windows) or "
    "`.venv/bin/python -m pip install <name>`"
)
_PIP_UNSAFE = re.compile(
    r"://|git\+|^-{1,2}(?:i|index-url|extra-index-url|trusted-host|f|find-links|proxy)\b", re.IGNORECASE
)
_NPM_UNSAFE = re.compile(r"://|git\+|^(?:github|gitlab|bitbucket):", re.IGNORECASE)
_GIT_CONFIG_UNSAFE = re.compile(
    r"hookspath|credential|sshcommand|fsmonitor|^alias\.|core\.editor|gpg\.program|diff\.external|^filter\.|"
    r"core\.pager|url\..*insteadof",
    re.IGNORECASE,
)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})
_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.IGNORECASE)

# Read-only mode (reviewer / security): a strict allowlist of inspection commands.
_RO_SIMPLE = frozenset(
    {"ls", "dir", "cat", "type", "head", "tail", "wc", "sort", "uniq", "cut", "tr", "grep", "egrep",
     "fgrep", "rg", "diff", "cmp", "file", "stat", "du", "tree", "echo", "printf", "pwd", "which",
     "where", "true", "false", "basename", "dirname", "realpath", "readlink", "date", "sleep", "test",
     "[", "less", "more", "nl", "column", "od", "md5sum", "sha1sum", "sha256sum", "cd", "pushd",
     "popd", "set-location", "get-content", "get-childitem", "select-string", "pytest", "ruff",
     "mypy", "flake8", "pylint", "pyflakes", "pip-audit", "eslint"}
)  # fmt: skip
_RO_GIT = frozenset(
    {"diff", "log", "show", "status", "blame", "ls-files", "ls-tree", "rev-parse", "rev-list",
     "describe", "shortlog", "cat-file", "merge-base", "grep", "branch", "tag", "remote", "stash",
     "diff-tree", "whatchanged", "count-objects", "check-ignore"}
)  # fmt: skip
_GIT_MUTATING_ARGS = frozenset(
    {"-d", "-D", "-m", "-M", "-c", "-C", "add", "remove", "rm", "rename", "set-url", "drop", "clear", "pop",
     "apply", "push", "save", "-f", "--force"}
)  # fmt: skip
_RO_PY_MODULES = frozenset(
    {"pytest", "ruff", "mypy", "flake8", "pyflakes", "pylint", "pip_audit", "unittest", "compileall",
     "json.tool", "pip"}
)  # fmt: skip
_RO_RUNNERS = {
    "npm": {"test", "t", "run", "audit", "ls", "list", "outdated", "view", "--version"},
    "pnpm": {"test", "run", "audit", "ls", "list", "outdated"},
    "yarn": {"test", "run", "audit", "list"},
    "go": {"test", "vet", "list", "version", "build"},
    "cargo": {"test", "check", "clippy", "metadata", "build"},
    "dotnet": {"test", "build", "--version"},
    "mvn": {"test", "verify", "-q"},
    "make": {"test", "check", "lint"},
    "tsc": None,
}  # fmt: skip


# --------------------------------------------------------------------------- policy


class Policy:
    """Decides whether one tool call may proceed."""

    def __init__(
        self,
        project_dir: Path | str,
        *,
        read_only: bool = False,
        allow_temp: bool = True,
        extra_write_roots: tuple[Path, ...] = (),
        max_mcp_input_chars: int = 1500,
        max_query_chars: int = 400,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.read_only = read_only
        temp = (Path(tempfile.gettempdir()).resolve(),) if allow_temp else ()
        self.write_roots = (self.project_dir, *temp, *extra_write_roots)
        self.max_mcp_input_chars = max_mcp_input_chars
        self.max_query_chars = max_query_chars
        self._activated = False  # the command being checked activates a virtualenv

    # ---- dispatch

    def check(self, tool: str, tool_input: dict[str, Any] | None) -> Decision:
        data = tool_input or {}
        if tool in _PATH_KEYS:
            return self._check_file_tool(tool, data)
        if tool in ("Bash", "PowerShell"):
            return self.check_bash(str(data.get("command", "")))
        if tool == "WebFetch":
            return self.check_url(str(data.get("url", "")))
        if tool == "WebSearch":
            query = str(data.get("query", ""))
            if len(query) > self.max_query_chars:
                return deny(f"search query is {len(query)} chars; keep queries short and never paste code")
            return ALLOW
        if tool.startswith("mcp__"):
            size = len(str(data))
            if size > self.max_mcp_input_chars:
                return deny(f"MCP request is {size} chars; describe the problem instead of pasting code")
            return ALLOW
        if tool in ("Agent", "Task"):
            return deny("this pipeline does not use nested agents")
        if tool == "AskUserQuestion":
            return deny("no human is available; make a reasonable assumption and record it in your report")
        return ALLOW

    # ---- files

    def resolve(self, raw: str, cwd: Path | None = None) -> Path:
        expanded = os.path.expandvars(os.path.expanduser(_msys_to_windows(raw)))
        p = Path(expanded)
        if not p.is_absolute():
            p = (cwd or self.project_dir) / p
        try:
            return p.resolve(strict=False)
        except (OSError, RuntimeError):
            return p

    def secret_reason(self, path: Path) -> str | None:
        parts = [p.lower() for p in path.parts]
        for part in parts:
            if part in _SECRET_DIRS:
                return f"{part} holds credentials"
        name = path.name
        if _SECRET_FILE_RE.match(name) and not _ENV_TEMPLATE_RE.match(name):
            return f"{name} looks like a secrets file"
        home = Path.home()
        if _inside(path, home / ".claude") or _norm(path) == _norm(home / ".claude.json"):
            return "Claude Code configuration and credentials are off limits"
        joined = "/".join(parts).replace("\\", "/")
        for frag in _SECRET_FRAGMENTS:
            if frag in joined:
                return "browser and OS credential stores are off limits"
        return None

    def _write_block_reason(self, path: Path) -> str | None:
        if not any(_inside(path, root) for root in self.write_roots):
            return f"writes are limited to the project directory ({self.project_dir})"
        if _inside(path, self.project_dir):
            try:
                rel = [p.lower() for p in path.relative_to(self.project_dir).parts]
            except ValueError:
                rel = []
            if ".git" in rel:
                return "the .git directory is managed by git, not by agents"
            if rel[:1] == [".claude"] and (len(rel) > 1 and (rel[1].startswith("settings") or rel[1] == "hooks")):
                return "Claude Code settings and hooks are off limits"
            if rel[:1] == [".mcp.json"]:
                return "MCP configuration is off limits"
            if rel[:1] == [".swarm"]:
                return "run artifacts and lessons are written by the pipeline, not by agents"
        return None

    def check_path(self, raw: str, *, write: bool, cwd: Path | None = None) -> Decision:
        if not raw:
            return ALLOW
        if re.search(r"[$%`]|\$\(", raw):
            return deny(f"cannot verify a path containing shell variables: {raw}")
        path = self.resolve(raw, cwd)
        reason = self.secret_reason(path)
        if reason:
            return deny(reason)
        if write:
            reason = self._write_block_reason(path)
            if reason:
                return deny(reason)
        return ALLOW

    def _check_file_tool(self, tool: str, data: dict[str, Any]) -> Decision:
        write = tool in _WRITE_TOOLS
        if write and self.read_only:
            return deny("this role is read-only")
        for key in _PATH_KEYS[tool]:
            value = data.get(key)
            if value:
                decision = self.check_path(str(value), write=write)
                if not decision.allowed:
                    return decision
        return ALLOW

    # ---- URLs

    def check_url(self, url: str) -> Decision:
        if len(url) > 800:
            return deny("URL is too long; long URLs can smuggle data out")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return deny("only http(s) URLs are allowed")
        if parts.username or parts.password:
            return deny("URLs with embedded credentials are blocked")
        host = parts.hostname.lower().strip("[]")
        if host in _LOOPBACK_HOSTS or host.endswith((".local", ".internal", ".lan", ".localhost")):
            return deny("local and internal hosts are blocked")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if re.fullmatch(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+))*", host):
                return deny("numeric host forms are blocked")
            return ALLOW
        if not ip.is_global:
            return deny("private and reserved IP addresses are blocked")
        return ALLOW

    # ---- shell

    def check_bash(self, command: str) -> Decision:
        cmd = strip_heredocs(command.replace("\r\n", "\n"))
        for pattern, reason in _DENY_PATTERNS:
            if pattern.search(cmd):
                return deny(reason)
        outer_activated = self._activated
        self._activated = outer_activated or bool(re.search(r"\bactivate\b", cmd))
        try:
            cwd: Path | None = self.project_dir
            for segment in split_segments(cmd):
                decision, cwd = self._check_segment(segment, cwd)
                if not decision.allowed:
                    return decision
            return ALLOW
        finally:
            self._activated = outer_activated

    def _check_segment(self, segment: str, cwd: Path | None) -> tuple[Decision, Path | None]:
        tokens = _command_tokens(tokenize(segment))

        for match in _REDIRECT_RE.finditer(blank_quotes(segment)):
            target = match.group(1).strip("\"'")
            if target.lower() in ("/dev/null", "nul", "nul:", "&1", "&2"):
                continue
            if self.read_only:
                return deny("this role is read-only; redirection is blocked"), cwd
            decision = self._check_write_target(target, cwd)
            if not decision.allowed:
                return decision, cwd

        if not tokens:
            return ALLOW, cwd
        verb = _verb(tokens)
        args = tokens[1:]

        if verb in _DENIED_VERBS:
            return deny(_DENIED_VERBS[verb]), cwd

        inner = self._inner_command(verb, args)
        if inner:
            decision = self.check_bash(inner)
            if not decision.allowed:
                return deny(f"inside nested command: {decision.reason}"), cwd

        if verb in ("cd", "pushd", "set-location", "chdir"):
            target = next((a for a in args if not a.startswith("-") or a == "-"), "")
            if not target or target == "-":
                cwd = None if target == "-" else Path.home()
            elif re.search(r"[$%`]", target):
                cwd = None
            else:
                cwd = self.resolve(target, cwd)
            return ALLOW, cwd

        checks = [
            (verb in _DELETE_VERBS, self._check_delete),
            (verb in _MOVE_VERBS, self._check_move),
            (verb in _COPY_VERBS, self._check_copy),
            (verb in _CREATE_VERBS, self._check_create),
            (verb in _FETCH_VERBS, self._check_fetch),
            (verb == "find", self._check_find),
            (verb == "sed", self._check_sed),
            (verb == "git" or verb == "git-bash", self._check_git),
            (verb == "taskkill", self._check_taskkill),
            (verb == "sc", self._check_sc),
            (verb == "net" or verb == "net1", self._check_net),
            (self._is_pip(tokens), self._check_pip),
            (verb in ("npm", "pnpm", "yarn", "bun"), self._check_npm),
        ]
        for applies, check in checks:
            if applies:
                decision = check(tokens, cwd)
                if not decision.allowed:
                    return decision, cwd

        if self.read_only:
            decision = self._check_read_only(tokens)
            if not decision.allowed:
                return decision, cwd
        return ALLOW, cwd

    @staticmethod
    def _inner_command(verb: str, args: list[str]) -> str:
        """The command hidden inside `bash -c '...'`, `cmd /c ...`, `eval '...'`, `xargs ...`."""
        lowered = [a.lower() for a in args]
        if verb in _SHELLS:
            for idx, a in enumerate(lowered):
                if a in ("-c", "-lc", "-ec", "-command", "-c", "/c", "/k"):
                    rest = args[idx + 1 :]
                    return " ".join(rest) if verb in ("cmd", "powershell", "pwsh") else (rest[0] if rest else "")
            return ""
        if verb == "eval":
            return " ".join(args)
        if verb == "xargs":
            i = 0
            while i < len(args):
                if args[i] in ("-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a", "-i"):
                    i += 2
                elif args[i].startswith("-"):
                    i += 1
                else:
                    break
            return " ".join(args[i:])
        return ""

    # -- path helpers for shell arguments

    def _check_write_target(self, token: str, cwd: Path | None) -> Decision:
        token = token.strip("\"'")
        if cwd is None and not os.path.isabs(_msys_to_windows(token)):
            return deny("cannot verify a relative path after an unknown directory change")
        return self.check_path(token, write=True, cwd=cwd)

    def _operands(self, args: list[str]) -> list[str]:
        return [a for a in args if not _is_flag(a)]

    # -- verb checks (each takes the full token list and the tracked cwd)

    def _check_delete(self, tokens: list[str], cwd: Path | None) -> Decision:
        targets = self._operands(tokens[1:])
        if _verb(tokens) == "remove-item":
            targets = [t for t in targets if not t.lower().startswith("-")]
        for t in targets:
            if t in ("*", ".", "..", "./*", ".\\*", ".*", "/", "\\", "~", "*.*"):
                return deny(f"refusing to delete {t!r}: name a specific sub-path of the project")
            decision = self._check_write_target(t, cwd)
            if not decision.allowed:
                return deny(f"delete blocked: {decision.reason}")
            if _norm(self.resolve(t, cwd)) == _norm(self.project_dir):
                return deny("refusing to delete the project root")
        return ALLOW

    def _check_move(self, tokens: list[str], cwd: Path | None) -> Decision:
        for t in self._operands(tokens[1:]):
            decision = self._check_write_target(t, cwd)
            if not decision.allowed:
                return deny(f"move blocked: {decision.reason}")
        return ALLOW

    def _check_copy(self, tokens: list[str], cwd: Path | None) -> Decision:
        ops = self._operands(tokens[1:])
        if not ops:
            return ALLOW
        dest = ops[1] if _verb(tokens) == "robocopy" and len(ops) > 1 else ops[-1]
        decision = self._check_write_target(dest, cwd)
        return decision if decision.allowed else deny(f"copy blocked: {decision.reason}")

    def _check_create(self, tokens: list[str], cwd: Path | None) -> Decision:
        for t in self._operands(tokens[1:]):
            decision = self._check_write_target(t, cwd)
            if not decision.allowed:
                return deny(f"write blocked: {decision.reason}")
        return ALLOW

    def _check_fetch(self, tokens: list[str], cwd: Path | None) -> Decision:
        urls = _URL_RE.findall(" ".join(tokens[1:]))
        loopback_words = [t for t in tokens[1:] if re.match(r"^(?:localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)\b", t)]
        if not urls and not loopback_words:
            return deny("outbound HTTP from the shell is blocked; use the researcher for web access")
        for u in urls:
            host = (urlsplit(u).hostname or "").lower()
            if host not in _LOOPBACK_HOSTS:
                return deny("outbound HTTP from the shell is blocked (loopback only); use the researcher")
        return ALLOW

    def _check_find(self, tokens: list[str], cwd: Path | None) -> Decision:
        args = tokens[1:]
        if not any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for a in args):
            return ALLOW
        roots: list[str] = []
        for a in args:
            if a.startswith("-") or a in ("(", ")", "!"):
                break
            roots.append(a)
        for r in roots or ["."]:
            resolved = self.resolve(r, cwd) if cwd else None
            if resolved is None or not _inside(resolved, self.project_dir):
                return deny("find -delete/-exec is only allowed inside the project directory")
        return ALLOW

    def _check_sed(self, tokens: list[str], cwd: Path | None) -> Decision:
        args = tokens[1:]
        if any(a == "-i" or (a.startswith("-i") and not a.startswith("--")) or a == "--in-place" for a in args):
            for t in self._operands(args)[1:]:
                decision = self._check_write_target(t, cwd)
                if not decision.allowed:
                    return deny(f"in-place edit blocked: {decision.reason}")
        return ALLOW

    @staticmethod
    def _git_parts(tokens: list[str]) -> tuple[str, list[str], list[str]]:
        """Return (subcommand, its args, values passed via `git -c key=value`)."""
        i, inline_config = 1, []
        while i < len(tokens):
            t = tokens[i]
            if t == "-c" and i + 1 < len(tokens):
                inline_config.append(tokens[i + 1])
                i += 2
            elif t in ("-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"):
                i += 2
            elif t.startswith("-"):
                i += 1
            else:
                break
        if i >= len(tokens):
            return "", [], inline_config
        return tokens[i].lower(), tokens[i + 1 :], inline_config

    def _check_git(self, tokens: list[str], cwd: Path | None) -> Decision:
        sub, rest, inline_config = self._git_parts(tokens)
        if any(_GIT_CONFIG_UNSAFE.search(c) for c in inline_config):
            return deny("that git setting can execute code or leak credentials")
        if not sub:
            return ALLOW
        rest_l = [r.lower() for r in rest]
        if sub == "push":
            return deny("git push is blocked: publishing is a human decision")
        if sub == "remote" and any(r in ("add", "set-url", "remove", "rm", "rename", "set-head") for r in rest_l):
            return deny("changing git remotes is blocked")
        if sub == "config":
            if "--global" in rest_l or "--system" in rest_l:
                return deny("global git config is blocked")
            if any(_GIT_CONFIG_UNSAFE.search(r) for r in rest):
                return deny("that git setting can execute code or leak credentials")
        if sub == "reset" and "--hard" in rest_l:
            return deny("git reset --hard would destroy uncommitted work")
        if sub == "clean":
            return deny("git clean deletes untracked files")
        if sub in ("filter-branch", "filter-repo", "credential", "daemon", "instaweb", "update-ref"):
            return deny(f"git {sub} is blocked")
        if sub in ("checkout", "restore") and any(r in (".", ":/", "*") for r in rest):
            return deny("this would discard all uncommitted changes")
        return ALLOW

    def _check_taskkill(self, tokens: list[str], cwd: Path | None) -> Decision:
        lowered = [t.lower() for t in tokens[1:]]
        if "/im" in lowered or "-im" in lowered:
            return deny("killing processes by image name is blocked; use /pid for your own process")
        return ALLOW

    def _check_sc(self, tokens: list[str], cwd: Path | None) -> Decision:
        lowered = [t.lower() for t in tokens[1:]]
        if any(t in lowered for t in ("create", "config", "delete", "start", "stop", "failure")):
            return deny("service control is blocked")
        return ALLOW

    def _check_net(self, tokens: list[str], cwd: Path | None) -> Decision:
        lowered = [t.lower() for t in tokens[1:]]
        if any(t in lowered for t in ("user", "localgroup", "share", "use", "start", "stop", "accounts")):
            return deny("net administration commands are blocked")
        return ALLOW

    @staticmethod
    def _is_pip(tokens: list[str]) -> bool:
        verb = _verb(tokens)
        if verb in ("pip", "pip3", "pipx"):
            return True
        if verb in ("python", "python3", "py") and len(tokens) > 2 and tokens[1] == "-m":
            return tokens[2] == "pip"
        return verb == "uv" and len(tokens) > 1 and tokens[1] in ("pip", "add")

    def _project_local(self, executable: str, cwd: Path | None) -> bool:
        """True if `executable` is a path (not a bare PATH lookup) that lives inside the project."""
        if not os.path.dirname(executable.replace("\\", "/")):
            return False
        return cwd is not None and _inside(self.resolve(executable, cwd), self.project_dir)

    def _check_pip(self, tokens: list[str], cwd: Path | None) -> Decision:
        lowered = [t.lower() for t in tokens]
        changing = any(t in lowered for t in ("install", "uninstall", "add", "remove"))
        if any(t in lowered for t in ("--break-system-packages", "--system", "--user")):
            return deny(VENV_HELP)
        if not changing:
            return ALLOW
        for t in tokens[1:]:
            if _PIP_UNSAFE.search(t):
                return deny(
                    "installing from URLs, VCS or alternate indexes is blocked; "
                    "use plain package names from the default index"
                )
        # uv works on the project's own .venv by design; everything else must name a project-local interpreter/pip
        if _verb(tokens) != "uv" and not (self._activated or self._project_local(tokens[0], cwd)):
            return deny(VENV_HELP)
        return ALLOW

    def _check_npm(self, tokens: list[str], cwd: Path | None) -> Decision:
        lowered = [t.lower() for t in tokens[1:]]
        if any(t in lowered for t in ("install", "i", "add")):
            if "-g" in lowered or "--global" in lowered:
                return deny("global installs modify the user's machine; install locally")
            for t in tokens[1:]:
                if _NPM_UNSAFE.search(t):
                    return deny("installing from URLs or VCS is blocked; use plain package names")
        return ALLOW

    # -- read-only allowlist

    def _check_read_only(self, tokens: list[str]) -> Decision:
        verb, args = _verb(tokens), tokens[1:]
        lowered = [a.lower() for a in args]
        if verb in _RO_SIMPLE:
            return ALLOW
        if verb == "git":
            sub, rest, _ = self._git_parts(tokens)
            if sub in _RO_GIT:
                if sub in ("branch", "tag", "remote", "stash") and _GIT_MUTATING_ARGS & set(rest):
                    return deny("this git subcommand form modifies the repository")
                return ALLOW
            return deny(f"read-only role: git {sub} is not allowed")
        if verb == "find":
            bad = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
            return deny("find with actions is blocked") if bad & set(args) else ALLOW
        if verb in ("python", "python3", "py"):
            if lowered[:1] in (["--version"], ["-v"]):
                return ALLOW
            if lowered[:1] == ["-m"] and len(args) > 1 and args[1] in _RO_PY_MODULES:
                if args[1] == "pip" and not (set(lowered[2:3]) & {"list", "show", "freeze", "check", "--version"}):
                    return deny("read-only role: only pip list/show/freeze/check")
                return ALLOW
            return deny("read-only role: python may only run test/lint modules (python -m pytest ...)")
        if verb in ("pip", "pip3"):
            ok = set(lowered[:1]) & {"list", "show", "freeze", "check", "--version"}
            return ALLOW if ok else deny("read-only role: only pip list/show/freeze/check")
        if verb in _RO_RUNNERS:
            allowed = _RO_RUNNERS[verb]
            if allowed is None:
                return ALLOW
            sub = next((a.lower() for a in args if not a.startswith("-")), "")
            if sub not in allowed:
                return deny(f"read-only role: {verb} {sub} is not allowed")
            if sub == "run":
                script = next((a for a in args[1:] if not a.startswith("-")), "")
                if script not in ("test", "lint", "typecheck", "check"):
                    return deny("read-only role: only test/lint scripts may be run")
            return ALLOW
        return deny(f"read-only role: '{verb}' is not an allowed inspection command")


# --------------------------------------------------------------------------- SDK glue


def hook_output(decision: Decision) -> dict[str, Any]:
    if decision.allowed:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"[swarm guard] {decision.reason}",
        }
    }


def make_hooks(
    policy: Policy, audit: Callable[[str, dict[str, Any], Decision], None] | None = None
) -> dict[str, list[Any]]:
    """Build the `hooks` argument for ClaudeAgentOptions (imports the SDK lazily)."""
    from claude_agent_sdk import HookMatcher

    async def pre_tool_use(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool = str(input_data.get("tool_name", ""))
        tool_input = input_data.get("tool_input") or {}
        try:
            decision = policy.check(tool, tool_input)
        except Exception as exc:  # noqa: BLE001 - a guard bug must fail closed
            decision = deny(f"guard error ({type(exc).__name__}): {exc}")
        if audit:
            audit(tool, tool_input, decision)
        return hook_output(decision)

    return {"PreToolUse": [HookMatcher(hooks=[pre_tool_use])]}
