"""A local model backend: run swarm agents on your own machine via Ollama, with zero Claude-plan usage.

This talks to Ollama's HTTP API (http://localhost:11434 by default) and runs the agentic loop itself:
the model asks for tools, this executes them, feeds the results back, and repeats. Every tool call goes
through the SAME `guard.Policy` the Claude path uses, so a local run is under the identical safety rules
(no secret reads, no writes outside the project, no outbound shell network, no git push, ...).

It installs nothing. You run Ollama and pull a model yourself; `cost_usd` is always 0. A small local model
is weaker than Claude and easier to prompt-inject, so untrusted content stays data, never instructions, and
roles keep least privilege.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from swarm import gates
from swarm.backend import AgentRequest, AgentResult, EventSink, describe_tool
from swarm.guard import Policy

MAX_SHELL_TIMEOUT = 600.0


def find_bash() -> str | None:
    """A POSIX bash whose syntax matches what guard.py parses.

    Critically NOT cmd.exe: the guard parses bash, but cmd.exe treats `^` as an escape that vanishes, so
    `g^it push` would slip past every verb rule. We also avoid Windows' System32\\bash.exe (the WSL launcher,
    a different filesystem). Prefer Git for Windows' bash.
    """
    env = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if env and Path(env).is_file():
        return env
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent  # <git>/cmd/git.exe or <git>/bin/git.exe -> <git>
        for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if cand.is_file():
                return str(cand)
    found = shutil.which("bash")
    if found and os.name == "nt" and ("system32" in found.lower() or "windows" in found.lower()):
        return None  # that is WSL's launcher, not a real bash
    return found

DEFAULT_URL = "http://localhost:11434"
# Licence notes per model family, from each family's Hugging Face model card. These are a STARTING POINT:
# a family's licence varies by size/tag (e.g. some Qwen2.5-Coder sizes are 'qwen-research', not Apache), so
# model_license() and `swarm local` always tell the user to verify the specific tag before commercial use.
# Deliberately NOT listed as permissive: deepseek-coder-v2 (custom DeepSeek Model Licence), llama*/gemma*
# (community licences, not permissive) — the user wants clean ownership.
MODEL_LICENSES: dict[str, str] = {
    "mistral": "Apache-2.0 on Mistral-7B / Nemo — verify the tag (huggingface.co/mistralai)",
    "mistral-nemo": "Apache-2.0 — verify the tag (huggingface.co/mistralai/Mistral-Nemo-Instruct-2407)",
    "olmo2": "Apache-2.0, fully open (huggingface.co/allenai) — verify the tag",
    "granite3.1-dense": "Apache-2.0 (huggingface.co/ibm-granite) — verify the tag",
    "qwen2.5-coder": "VARIES BY SIZE: some sizes are 'qwen-research' (not permissive) — verify the tag's card",
    "qwen2.5": "varies by size — verify the tag (huggingface.co/Qwen)",
    "qwen3": "Apache-2.0 on most tags — verify the tag (huggingface.co/Qwen)",
}
DEFAULT_MODEL = os.environ.get("SWARM_OLLAMA_MODEL", "qwen2.5-coder")
# Ollama's own default context is 4096 tokens and it truncates silently, which drops the role prompt or the file
# an agent just read. 16384 fits a 7B coder model entirely on an 8 GB GPU; lower it with SWARM_OLLAMA_NUM_CTX
# if the model spills to CPU (`ollama ps` shows the split).
DEFAULT_NUM_CTX = 16384
MIN_NUM_CTX, MAX_NUM_CTX = 2048, 131072


def num_ctx() -> int:
    """Context window requested from Ollama: SWARM_OLLAMA_NUM_CTX, clamped; DEFAULT_NUM_CTX when unset or invalid."""
    try:
        value = int(os.environ.get("SWARM_OLLAMA_NUM_CTX", DEFAULT_NUM_CTX))
    except ValueError:
        return DEFAULT_NUM_CTX
    return max(MIN_NUM_CTX, min(MAX_NUM_CTX, value))


# --------------------------------------------------------------------------- HTTP client


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, base_url: str | None = None, timeout: float = 600.0) -> None:
        self.base_url = (base_url or os.environ.get("SWARM_OLLAMA_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:  # noqa: S310 - fixed localhost
                return json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise OllamaError(f"Ollama request to {path} failed: {exc}") from exc

    def _get(self, path: str, timeout: float = 10.0) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(f"{self.base_url}{path}", timeout=timeout) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise OllamaError(f"Ollama request to {path} failed: {exc}") from exc

    def is_up(self) -> bool:
        try:
            self._get("/api/tags", timeout=4.0)
            return True
        except OllamaError:
            return False

    def models(self) -> list[str]:
        try:
            data = self._get("/api/tags", timeout=8.0)
        except OllamaError:
            return []
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        fmt: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            # extra_options carries the PC-load decision (CPU-only, fewer threads) set by LocalBackend
            "options": {"num_ctx": num_ctx(), **getattr(self, "extra_options", {})},
        }
        if tools:
            payload["tools"] = tools
        if fmt:
            payload["format"] = fmt
        data = self._post("/api/chat", payload, timeout=timeout)
        stats = self.__dict__.setdefault("stats", {"prompt_tokens": 0, "output_tokens": 0, "gen_seconds": 0.0})
        stats["prompt_tokens"] += int(data.get("prompt_eval_count") or 0)
        stats["output_tokens"] += int(data.get("eval_count") or 0)
        stats["gen_seconds"] += float(data.get("eval_duration") or 0) / 1e9
        return data.get("message", {}) or {}


# --------------------------------------------------------------------------- tool execution


def _ok(text: str) -> str:
    return text if text else "(ok)"


class ToolExecutor:
    """Executes the model's tool calls against the real machine, every call gated by `Policy` first."""

    def __init__(self, policy: Policy, cwd: Path, on_event: EventSink | None = None) -> None:
        self.policy = policy
        self.cwd = cwd
        self.on_event = on_event
        self.denied = 0
        self.calls = 0

    def _emit(self, kind: str, text: str) -> None:
        if self.on_event:
            self.on_event(kind, text)

    # the guard tool-name each local tool maps to, for Policy.check
    _GUARD_NAME = {
        "read_file": "Read", "write_file": "Write", "edit_file": "Edit",
        "run_shell": "Bash", "grep": "Grep", "glob": "Glob", "list_dir": "Glob",
    }  # fmt: skip

    def run(self, name: str, args: dict[str, Any]) -> str:
        guard_tool = self._GUARD_NAME.get(name)
        if guard_tool is None:
            return f"error: unknown tool '{name}'"
        self.calls += 1
        tool_input = self._to_guard_input(name, args)
        decision = self.policy.check(guard_tool, tool_input)
        if not decision.allowed:
            self.denied += 1
            self._emit("tool", f"BLOCKED {describe_tool(guard_tool, tool_input)} ({decision.reason})")
            return f"blocked by guard: {decision.reason}"
        self._emit("tool", f"{name} {str(list(args.values())[:1])[:80]}")
        try:
            return self._execute(name, args)
        except Exception as exc:  # noqa: BLE001 - a tool error is data for the model, not a crash
            return f"error: {type(exc).__name__}: {exc}"

    def _to_guard_input(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name in ("read_file",):
            return {"file_path": args.get("path", "")}
        if name in ("write_file", "edit_file"):
            return {"file_path": args.get("path", "")}
        if name == "run_shell":
            return {"command": args.get("command", "")}
        if name in ("grep", "glob", "list_dir"):
            return {"path": args.get("path", "")}
        return {}

    def _resolve(self, raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else (self.cwd / p)

    def _execute(self, name: str, args: dict[str, Any]) -> str:
        if name == "read_file":
            text = self._resolve(args["path"]).read_text(encoding="utf-8", errors="replace")
            return text[:20000]
        if name == "write_file":
            path = self._resolve(args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.get("content", ""), encoding="utf-8")
            return _ok(f"wrote {path}")
        if name == "edit_file":
            path = self._resolve(args["path"])
            old, new = args.get("old", ""), args.get("new", "")
            text = path.read_text(encoding="utf-8")
            if old not in text:
                return "error: 'old' text not found in the file"
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
            return _ok(f"edited {path}")
        if name == "run_shell":
            return self._shell(str(args.get("command", "")), args.get("timeout", 120))
        if name in ("grep", "glob", "list_dir"):
            return self._search(name, args)
        return f"error: unhandled tool '{name}'"

    def _venv_command(self, command: str) -> str:
        """Point a bare `python`/`pytest`/`ruff` at the project's own virtualenv, as the gates do.

        Without this the model's `python -m pytest` runs the machine's Python, finds no pytest, and the model
        starts building virtualenvs instead of doing the task. Only a single plain command is rewritten; anything
        with shell operators is left exactly as the guard approved it.
        """
        py = gates.venv_python(Path(self.cwd))
        if py is None or any(ch in command for ch in ";&|<>`$()\n"):
            return command
        first, _, rest = command.strip().partition(" ")
        quoted = shlex.quote(py.as_posix())
        if first in ("python", "python3", "py"):
            return f"{quoted} {rest}".rstrip()
        if first in ("pytest", "ruff", "mypy", "flake8", "pylint"):
            return f"{quoted} -m {first} {rest}".rstrip()
        return command

    def _shell(self, command: str, timeout: Any) -> str:
        """Run the (already policy-allowed) command through bash, NOT cmd.exe, so the guard's bash parse holds.

        Uses swarm's clean_env (pip locked to venvs, swarm's own venv hidden), a model-proof timeout clamp, and
        the kill-the-whole-tree-on-timeout pattern so a hung child cannot block the agent loop.
        """
        bash = find_bash()
        if bash is None:
            return (
                "error: no POSIX bash found; local shell commands run through bash (not cmd.exe) for safety. "
                "Install Git for Windows or set CLAUDE_CODE_GIT_BASH_PATH."
            )
        try:
            secs = min(max(1.0, float(timeout)), MAX_SHELL_TIMEOUT)
        except (TypeError, ValueError):
            secs = 120.0
        extra: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {}
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell; the command was allowlisted by Policy
            [bash, "-c", self._venv_command(command)], cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", env=gates.clean_env(), **extra,
        )  # fmt: skip
        try:
            out, _ = proc.communicate(timeout=secs)
            return f"exit {proc.returncode}\n{out}"[:15000]
        except subprocess.TimeoutExpired:
            gates._kill_tree(proc)
            try:
                out, _ = proc.communicate(timeout=10)
            except subprocess.SubprocessError:
                out = ""
            return f"timed out after {secs:.0f}s\n{(out or '')[:5000]}"

    def _search(self, name: str, args: dict[str, Any]) -> str:
        root = self._resolve(args.get("path", "."))
        if name == "glob" or name == "list_dir":
            pattern = args.get("pattern", "*")
            hits = [str(p) for p in sorted(root.glob(pattern))][:200]
            return "\n".join(hits) or "(no matches)"
        # grep. The pattern comes from the model, so bound the work: cap files, skip big files, and clip each
        # line before matching (a crude but effective brake on catastrophic-backtracking regexes).
        import re

        try:
            pattern = re.compile(args.get("pattern", ""))
        except re.error as exc:
            return f"error: bad regex: {exc}"
        out: list[str] = []
        files = [root] if root.is_file() else sorted(root.rglob(args.get("glob", "*")))[:2000]
        for f in files:
            if not f.is_file() or f.stat().st_size > 2_000_000:
                continue
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if pattern.search(line[:2000]):
                        out.append(f"{f}:{i}: {line.strip()[:200]}")
                        if len(out) >= 200:
                            return "\n".join(out)
            except OSError:
                continue
        return "\n".join(out) or "(no matches)"


# --------------------------------------------------------------------------- tool schemas


_STR = {"type": "string"}


def _fn(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def _tool_schemas(read_only: bool) -> list[dict[str, Any]]:
    s = [
        _fn("read_file", "Read a text file", {"path": _STR}, ["path"]),
        _fn("glob", "List files matching a glob", {"path": _STR, "pattern": _STR}, ["pattern"]),
        _fn("grep", "Search files for a regex", {"path": _STR, "pattern": _STR}, ["pattern"]),
        _fn("run_shell", "Run a shell command (allowlisted test/lint runners only)", {"command": _STR}, ["command"]),
    ]
    if not read_only:
        s += [
            _fn("write_file", "Create or overwrite a file", {"path": _STR, "content": _STR}, ["path", "content"]),
            _fn("edit_file", "Replace old with new in a file", {"path": _STR, "old": _STR, "new": _STR},
                ["path", "old", "new"]),
        ]
    return s


# --------------------------------------------------------------------------- the backend


class LocalBackend:
    """Runs one role on a local Ollama model. Same Backend interface as ClaudeBackend; cost is always 0."""

    def __init__(self, model: str | None = None, base_url: str | None = None, load_aware: bool | None = None) -> None:
        self.model = model or DEFAULT_MODEL
        self.client = OllamaClient(base_url)
        # Load awareness probes the real machine and changes Ollama's process priority, and the ledger writes to
        # the user's home folder; SWARM_LOAD_AWARE=0 turns both off (the test suite does).
        self.load_aware = os.environ.get("SWARM_LOAD_AWARE", "1") != "0" if load_aware is None else load_aware
        self._mode = "full"
        self._req: AgentRequest | None = None

    def _record(self, ok: bool, turns: int, seconds: float, subtype: str) -> None:
        """One ledger line per agent call: tokens, time, PC mode and GPU draw, for the usage dashboard."""
        from swarm import hardware, usage

        stats = getattr(self.client, "stats", None) or {}
        gpus = hardware.gpu_stats()
        usage.record_local({
            "role": getattr(getattr(self._req, "role", None), "name", ""),
            "label": getattr(self._req, "label", ""),
            "model": self.model,
            "ok": ok,
            "subtype": subtype,
            "turns": turns,
            "seconds": round(seconds, 2),
            "prompt_tokens": stats.get("prompt_tokens", 0),
            "output_tokens": stats.get("output_tokens", 0),
            "gen_seconds": round(stats.get("gen_seconds", 0.0), 2),
            "mode": self._mode,
            "gpu_w": gpus[0].power_w if gpus and self._mode in ("full", "background") else 0.0,
        })  # fmt: skip
        if stats:
            stats.update({"prompt_tokens": 0, "output_tokens": 0, "gen_seconds": 0.0})

    async def run(self, request: AgentRequest, on_event: EventSink | None = None) -> AgentResult:
        import asyncio

        return await asyncio.to_thread(self._run_sync, request, on_event)

    def _run_sync(self, req: AgentRequest, on_event: EventSink | None) -> AgentResult:
        started = time.monotonic()
        if not self.client.is_up():
            return AgentResult(
                False, fatal=True, subtype="ollama_down",
                error=f"Ollama is not reachable at {self.client.base_url}. Start it (`ollama serve`) and "
                f"pull a model (`ollama pull {self.model}`).",
            )  # fmt: skip
        self._mode = "full"
        self._req = req
        if self.load_aware:
            # Look at what the owner is doing first: stay off the GPU during a game, go quiet during a call,
            # wait out a saturated machine. See swarm.load.
            from swarm import load

            decision = load.prepare(on_note=(lambda note: on_event("text", note)) if on_event else None)
            self._mode = decision.mode
            self.client.extra_options = decision.ollama_options()
            if on_event and decision.busy:
                on_event("text", f"PC in use: running in {decision.mode} mode ({'; '.join(decision.reasons)})")
        policy = Policy(req.cwd, read_only=req.read_only)
        executor = ToolExecutor(policy, req.cwd, on_event)
        tools = _tool_schemas(req.read_only)
        tool_names = {t["function"]["name"] for t in tools}
        system = (req.system_append or "You are a careful software engineering agent.") + LOCAL_TOOL_RULES
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": req.prompt + LOCAL_TOOL_REMINDER},
        ]
        nudges = 0
        max_turns = req.max_turns or 20
        texts: list[str] = []
        turns = 0
        try:
            for turns in range(1, max_turns + 1):
                msg = self.client.chat(self.model, messages, tools=tools)
                content = (msg.get("content") or "").strip()
                calls = msg.get("tool_calls") or []
                if not calls and content:
                    # Small local models (qwen2.5-coder among them) often write the call as JSON in the message
                    # body instead of the structured field. Same executor, same guard, either way.
                    calls = calls_from_text(content, tool_names)
                    if calls:
                        content = ""
                if content:
                    texts.append(content)
                    if on_event:
                        on_event("text", content)
                if not calls:
                    # A small model will happily write "done, verified" without having touched anything. A role
                    # that is allowed to write and has not run a single tool has done nothing: say so, once or twice.
                    if not req.read_only and executor.calls == 0 and nudges < MAX_NUDGES:
                        nudges += 1
                        messages.append({"role": "assistant", "content": content})
                        messages.append({"role": "user", "content": NUDGE})
                        if texts and content:
                            texts.pop()
                        continue
                    break
                messages.append({"role": "assistant", "content": content, "tool_calls": calls})
                for call in calls:
                    fn = call.get("function", {})
                    name, raw_args = fn.get("name", ""), fn.get("arguments", {})
                    args = raw_args if isinstance(raw_args, dict) else _parse_args(raw_args)
                    result = executor.run(name, args)
                    messages.append({"role": "tool", "content": result[:15000]})
                if time.monotonic() - started > req.timeout_s:
                    return self._result(False, texts, turns, executor, started, subtype="timeout",
                                        error=f"timed out after {req.timeout_s:.0f}s")  # fmt: skip
            structured = self._structured(req, messages) if req.output_schema else None
        except OllamaError as exc:
            return self._result(False, texts, turns, executor, started, subtype="error", error=str(exc))

        ok = bool(texts) or structured is not None
        if req.output_schema and structured is None:
            return self._result(False, texts, turns, executor, started, subtype="no_structured_output",
                                error="the local model did not return a valid structured report")  # fmt: skip
        return self._result(ok, texts, turns, executor, started, structured=structured,
                            subtype="success" if ok else "empty")  # fmt: skip

    def _structured(self, req: AgentRequest, messages: list[dict[str, Any]]) -> Any:
        ask = messages + [{"role": "user", "content": "Now output ONLY your final report as JSON matching the schema."}]
        try:
            msg = self.client.chat(self.model, ask, tools=None, fmt=req.output_schema)
            return json.loads(msg.get("content") or "")
        except (OllamaError, ValueError):
            return None

    def _result(
        self, ok: bool, texts: list[str], turns: int, ex: ToolExecutor, started: float,
        structured: Any = None, subtype: str = "", error: str = "", fatal: bool = False,
    ) -> AgentResult:  # fmt: skip
        if self.load_aware:
            self._record(ok, turns, time.monotonic() - started, subtype)
        return AgentResult(
            ok=ok, text="\n".join(texts), structured=structured, cost_usd=0.0, turns=turns,
            seconds=time.monotonic() - started, subtype=subtype, error=error, fatal=fatal, denied=ex.denied,
            model=self.model,
        )  # fmt: skip


LOCAL_TOOL_RULES = """

HOW TO USE TOOLS (this overrides any other format you know):
- To act, reply with ONLY one JSON object and nothing else: {"name": "<tool>", "arguments": {...}}
- One tool call per reply. No prose, no code fences, no XML tags around it.
- Nothing happens unless you call a tool: text you write does not create or change any file.
- Never say something is done, changed or verified unless a tool result in this conversation shows it.
- Write your final report in plain text only after the tool results show the work is finished."""
LOCAL_TOOL_REMINDER = (
    '\n\nStart by calling a tool. Reply with ONLY a JSON object: {"name": "<tool>", "arguments": {...}}'
)
NUDGE = (
    "You have not called any tool, so nothing has been created, changed or run. Do the work now. Reply with ONLY "
    'one JSON object, for example: {"name": "write_file", "arguments": {"path": "example.py", "content": "..."}}'
)
MAX_NUDGES = 2

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
MAX_TEXT_CALLS = 8


def _json_values(text: str) -> list[Any]:
    """Every top-level JSON value found in text, in order (objects or arrays, possibly several, possibly with prose)."""
    decoder = json.JSONDecoder(strict=False)  # small models put raw newlines inside JSON strings
    values: list[Any] = []
    i = 0
    while i < len(text):
        if text[i] in "{[":
            try:
                value, end = decoder.raw_decode(text, i)
            except ValueError:
                i += 1
                continue
            values.append(value)
            i = end
        else:
            i += 1
    return values


def calls_from_text(content: str, tool_names: set[str]) -> list[dict[str, Any]]:
    """Tool calls a model wrote as text instead of using the structured tool_calls field.

    Accepts a bare JSON object, several objects, a JSON array, ```json fences and <tool_call> tags. Only objects
    whose "name" is one of the tools offered and whose "arguments" (or "parameters") is an object count; anything
    else is treated as ordinary prose. Returns the same shape Ollama uses for structured calls.
    """
    chunks = _TAG_RE.findall(content) or _FENCE_RE.findall(content) or [content]
    calls: list[dict[str, Any]] = []
    for chunk in chunks:
        for value in _json_values(chunk):
            for item in value if isinstance(value, list) else [value]:
                if not isinstance(item, dict):
                    continue
                item = item.get("function", item) if isinstance(item.get("function"), dict) else item
                name = item.get("name")
                args = item.get("arguments", item.get("parameters"))
                if isinstance(args, str):
                    args = _parse_args(args)
                if name in tool_names and isinstance(args, dict):
                    calls.append({"function": {"name": name, "arguments": args}})
                if len(calls) >= MAX_TEXT_CALLS:
                    return calls
    return calls


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}
    return {}


def model_license(model: str) -> str:
    """A licence note for a model's family, always ending in "verify" — a lookup is not legal clearance."""
    base = model.split(":")[0]
    return MODEL_LICENSES.get(base, "unknown licence — verify the model card before commercial use")
