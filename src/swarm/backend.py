"""Agent backends: the real Claude Code engine (via the Agent SDK) and the interface tests fake."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from swarm.claude_cli import LOGIN_HELP, looks_like_auth_error, looks_like_limit_error
from swarm.gates import clean_env
from swarm.guard import Decision, Policy, make_hooks
from swarm.roles import Role

EventSink = Callable[[str, str], None]  # (kind: "tool" | "text" | "limit", text)

ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
    "MCP_TIMEOUT": "45000",
    "PYTHONIOENCODING": "utf-8",
}  # merged on top of gates.clean_env(): pip refuses global installs, swarm's own venv is hidden
DISALLOWED = ["Agent", "Task", "AskUserQuestion"]


@dataclass
class AgentRequest:
    role: Role
    label: str
    prompt: str
    cwd: Path
    model: str | None
    tools: list[str]
    allowed_tools: list[str]
    mcp_servers: dict[str, dict]
    read_only: bool
    max_turns: int | None
    max_budget_usd: float | None
    timeout_s: float
    system_append: str
    output_schema: dict[str, Any] | None = None
    transcript: Path | None = None
    cli_path: str | None = None
    resume: str | None = None  # continue this session (keeps everything the agent already read)


@dataclass
class AgentResult:
    ok: bool
    text: str = ""
    structured: Any = None
    cost_usd: float = 0.0
    turns: int = 0
    seconds: float = 0.0
    session_id: str | None = None
    subtype: str = ""
    error: str = ""
    fatal: bool = False  # environment problem (login, missing CLI): retrying will not help
    warnings: list[str] = field(default_factory=list)
    denied: int = 0  # tool calls the guard blocked
    model: str | None = None  # the actual model that produced the result


class Backend(Protocol):
    async def run(self, request: AgentRequest, on_event: EventSink | None = None) -> AgentResult: ...


class PlanLimit(Exception):
    """The Claude plan's usage limit is (nearly) reached, or usage would spill into paid overage."""


def _reset_text(timestamp: int | None) -> str:
    if not timestamp:
        return ""
    try:
        return " (resets " + datetime.fromtimestamp(timestamp).strftime("%a %H:%M") + ")"
    except (OverflowError, OSError, ValueError):
        return ""


def _utilization(info: Any) -> float | None:
    value = getattr(info, "utilization", None)
    if value is None:
        return None
    return value / 100 if value > 1.0 else float(value)


def plan_limit_reason(info: Any, model: str | None, stop_at: float) -> str | None:
    """Why a run must stop now to stay inside the Claude plan (None = carry on).

    `info` is the SDK's RateLimitInfo (duck-typed). Stops on: any rejected window, any sign that usage is
    being served from paid overage, and a warning whose utilization has reached `stop_at`.
    """
    window = getattr(info, "rate_limit_type", None) or "plan"
    status = getattr(info, "status", "allowed")
    lowered = (model or "").lower()
    if model and (
        (window == "seven_day_opus" and "opus" not in lowered)
        or (window == "seven_day_sonnet" and "sonnet" not in lowered)
    ):
        return None  # a model-specific window for a model this call does not use
    resets = _reset_text(getattr(info, "resets_at", None))
    if window == "overage" and status != "rejected":
        return "usage has spilled into paid overage (pay-as-you-go), which is outside your plan"
    if status == "rejected":
        return f"the {window} plan limit is reached{resets}"
    used = _utilization(info)
    if status == "allowed_warning" and used is not None and used >= stop_at:
        return f"{used:.0%} of the {window} plan limit is used{resets}"
    return None


def limit_note(info: Any) -> str | None:
    """A short human note for a non-fatal warning."""
    if getattr(info, "status", "") != "allowed_warning":
        return None
    used = _utilization(info)
    window = getattr(info, "rate_limit_type", None) or "plan"
    share = f" at {used:.0%}" if used is not None else ""
    return f"plan usage warning: {window} window{share}{_reset_text(getattr(info, 'resets_at', None))}"


def describe_tool(name: str, data: dict[str, Any] | None) -> str:
    data = data or {}
    for key in ("file_path", "path", "pattern", "command", "url", "query", "notebook_path"):
        if data.get(key):
            return f"{name} {str(data[key]).strip().splitlines()[0][:100]}"
    return name


def _clip(value: Any, limit: int = 4000) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...[+{len(value) - limit} chars]"
    return value


class ClaudeBackend:
    """Runs one role on the Claude Code engine with least-privilege tools and the guard hook."""

    def __init__(self, plan_stop_at: float = 0.9) -> None:
        self.plan_stop_at = plan_stop_at

    def build_options(self, req: AgentRequest, audit: Callable[[str, dict[str, Any], Decision], None] | None = None):
        from claude_agent_sdk import ClaudeAgentOptions

        policy = Policy(req.cwd, read_only=req.read_only)
        return ClaudeAgentOptions(
            cwd=str(req.cwd),
            cli_path=req.cli_path,
            model=req.model,
            system_prompt={"type": "preset", "preset": "claude_code", "append": req.system_append},
            tools=list(req.tools),
            allowed_tools=list(req.allowed_tools),
            disallowed_tools=list(DISALLOWED),
            permission_mode="dontAsk",
            mcp_servers=dict(req.mcp_servers),
            strict_mcp_config=True,
            setting_sources=[],
            max_turns=req.max_turns,
            max_budget_usd=req.max_budget_usd,
            output_format={"type": "json_schema", "schema": req.output_schema} if req.output_schema else None,
            hooks=make_hooks(policy, audit),
            env=clean_env(ENV),
            resume=req.resume,
        )

    # ---- transcript

    @staticmethod
    def _log(req: AgentRequest, kind: str, data: Any) -> None:
        if req.transcript is None:
            return
        line = json.dumps(
            {"t": round(time.time(), 3), "role": req.role.name, "label": req.label, "kind": kind, "data": _clip(data)},
            ensure_ascii=False,
            default=str,
        )
        with contextlib.suppress(OSError), req.transcript.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # ---- run

    async def run(self, request: AgentRequest, on_event: EventSink | None = None) -> AgentResult:
        started = time.monotonic()
        denied = 0
        stderr_tail: collections.deque[str] = collections.deque(maxlen=25)

        def audit(tool: str, tool_input: dict[str, Any], decision: Decision) -> None:
            nonlocal denied
            if not decision.allowed:
                denied += 1
                self._log(request, "guard-deny", {"tool": tool, "input": tool_input, "reason": decision.reason})
                if on_event:
                    on_event("tool", f"BLOCKED {describe_tool(tool, tool_input)} ({decision.reason})")

        try:
            options = self.build_options(request, audit)
            options.stderr = stderr_tail.append
        except ImportError as exc:
            return AgentResult(False, fatal=True, error=f"claude-agent-sdk is not installed: {exc}")

        try:
            result = await asyncio.wait_for(self._consume(request, options, on_event), timeout=request.timeout_s)
        except TimeoutError:
            return AgentResult(
                False, seconds=time.monotonic() - started, subtype="timeout", denied=denied,
                error=f"timed out after {request.timeout_s:.0f}s",
            )  # fmt: skip
        except PlanLimit as exc:
            self._log(request, "plan-limit", str(exc))
            return AgentResult(
                False, seconds=time.monotonic() - started, subtype="plan_limit", denied=denied,
                error=f"stopped to protect your Claude plan: {exc}",
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - classify everything; the pipeline decides what to do
            return self._from_exception(exc, "\n".join(stderr_tail), time.monotonic() - started, denied)

        result.seconds = time.monotonic() - started
        result.denied = denied
        if not result.ok and result.subtype != "plan_limit" and looks_like_limit_error(result.error):
            result.subtype = "plan_limit"
            result.error = f"stopped to protect your Claude plan: {result.error[:300]}"
        if not result.ok and not result.fatal and looks_like_auth_error(result.error + " " + "\n".join(stderr_tail)):
            result.fatal = True
            result.error = f"not authenticated. {LOGIN_HELP}"
        self._log(request, "result", {"ok": result.ok, "cost": result.cost_usd, "turns": result.turns,
                                       "subtype": result.subtype, "error": result.error})  # fmt: skip
        return result

    async def _consume(self, req: AgentRequest, options: Any, on_event: EventSink | None) -> AgentResult:
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolUseBlock, query

        try:
            from claude_agent_sdk import RateLimitEvent
        except ImportError:  # older SDKs do not report plan limits
            RateLimitEvent = None  # noqa: N806

        emit = on_event or (lambda kind, text: None)
        texts: list[str] = []
        warnings: list[str] = []
        final: Any = None
        stream = query(prompt=req.prompt, options=options)
        try:
            async for msg in stream:
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    for srv in msg.data.get("mcp_servers") or []:
                        if srv.get("status") in ("failed", "needs-auth"):
                            warnings.append(f"MCP server '{srv.get('name')}' is {srv.get('status')}")
                elif RateLimitEvent is not None and isinstance(msg, RateLimitEvent):
                    info = msg.rate_limit_info
                    self._log(req, "rate-limit", getattr(info, "raw", None) or str(info))
                    reason = plan_limit_reason(info, req.model, self.plan_stop_at)
                    if reason:
                        raise PlanLimit(reason)
                    if note := limit_note(info):
                        emit("limit", note)
                elif isinstance(msg, AssistantMessage):
                    if getattr(msg, "error", None) in ("rate_limit", "billing_error"):
                        raise PlanLimit(f"the API reported {msg.error}")
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            texts.append(block.text)
                            self._log(req, "text", block.text)
                            emit("text", block.text)
                        elif isinstance(block, ToolUseBlock):
                            summary = describe_tool(block.name, block.input)
                            self._log(req, "tool", summary)
                            emit("tool", summary)
                elif isinstance(msg, ResultMessage):
                    final = msg
        except Exception:
            if final is None:  # a single-shot query raises after an error result; keep the result if we have it
                raise
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

        if final is None:
            return AgentResult(
                False, text="\n".join(texts), error="the engine ended without a result", warnings=warnings
            )
        return self._from_result(req, final, texts, warnings)

    @staticmethod
    def _from_result(req: AgentRequest, msg: Any, texts: list[str], warnings: list[str]) -> AgentResult:
        subtype = getattr(msg, "subtype", "") or ""
        errors = list(getattr(msg, "errors", None) or [])
        ok = not msg.is_error and subtype == "success"
        error = ""
        if not ok:
            error = "; ".join(errors) or (msg.result or "") or subtype or "unknown error"
        structured = getattr(msg, "structured_output", None)
        if ok and req.output_schema and structured is None:
            ok, subtype, error = False, "no_structured_output", "the agent finished without a structured report"
        status = getattr(msg, "api_error_status", None)
        return AgentResult(
            ok=ok,
            text=msg.result or "\n".join(texts),
            structured=structured,
            cost_usd=float(msg.total_cost_usd or 0.0),
            turns=int(msg.num_turns or 0),
            session_id=msg.session_id,
            subtype=subtype,
            error=error,
            fatal=status in (401, 403),
            warnings=warnings,
        )

    @staticmethod
    def _from_exception(exc: Exception, stderr: str, seconds: float, denied: int) -> AgentResult:
        name = type(exc).__name__
        detail = f"{exc}" + (f"\n{stderr[-800:]}" if stderr else "")
        if name == "CLINotFoundError":
            return AgentResult(
                False, seconds=seconds, fatal=True, subtype="cli_not_found", denied=denied,
                error="the Claude Code executable was not found. Install Claude Code, or set [claude] cli_path in "
                "swarm.toml or the SWARM_CLAUDE_CLI environment variable.",
            )  # fmt: skip
        fatal = looks_like_auth_error(detail)
        if not fatal and looks_like_limit_error(detail):
            return AgentResult(
                False, seconds=seconds, subtype="plan_limit", denied=denied,
                error=f"stopped to protect your Claude plan: {detail[:300]}",
            )  # fmt: skip
        error = f"not authenticated. {LOGIN_HELP}" if fatal else f"{name}: {detail}"
        return AgentResult(False, seconds=seconds, fatal=fatal, subtype="exception", error=error, denied=denied)
