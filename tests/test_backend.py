from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import swarm.backend as backend_mod
from swarm.backend import (
    DISALLOWED,
    AgentRequest,
    AgentResult,
    ClaudeBackend,
    PlanLimit,
    describe_tool,
    limit_note,
    plan_limit_reason,
    plan_reset_time,
)
from swarm.claude_cli import AuthInfo, billing_kind, looks_like_limit_error
from swarm.roles import Role

sdk = pytest.importorskip("claude_agent_sdk")


def make_request(tmp_path: Path, **overrides: Any) -> AgentRequest:
    role = Role("developer", "d", "prompt\n", ("Read", "Write", "Bash"), "sonnet", 10, Path("developer.md"))
    fields: dict[str, Any] = dict(
        role=role, label="W1", prompt="do it", cwd=tmp_path, model="sonnet", tools=["Read", "Write", "Bash"],
        allowed_tools=["Read", "Write", "Bash", "mcp__grep__*"], mcp_servers={"grep": {"type": "http", "url": "u"}},
        read_only=False, max_turns=10, max_budget_usd=1.5, timeout_s=30, system_append="APPEND",
    )  # fmt: skip
    fields.update(overrides)
    return AgentRequest(**fields)


def info(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {"status": "allowed", "rate_limit_type": "five_hour", "utilization": None, "resets_at": None}
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- plan protection


class TestPlanLimitReason:
    @pytest.mark.parametrize("window", ["five_hour", "seven_day", None])
    @pytest.mark.parametrize("model", ["sonnet", "claude-opus-4", "haiku", None])
    def test_allowed_carries_on(self, window: str | None, model: str | None) -> None:
        assert plan_limit_reason(info(rate_limit_type=window), model, 0.9) is None

    @pytest.mark.parametrize("window", ["five_hour", "seven_day", None])
    @pytest.mark.parametrize("model", ["sonnet", "opus", "haiku", None])
    def test_rejected_general_window_stops(self, window: str | None, model: str | None) -> None:
        reason = plan_limit_reason(info(status="rejected", rate_limit_type=window), model, 0.9)
        assert reason and "limit is reached" in reason

    @pytest.mark.parametrize("status", ["allowed", "allowed_warning"])
    @pytest.mark.parametrize("model", ["sonnet", "haiku", None])
    def test_overage_stops(self, status: str, model: str | None) -> None:
        reason = plan_limit_reason(info(status=status, rate_limit_type="overage"), model, 0.9)
        assert reason and "overage" in reason

    def test_rejected_overage_stops(self) -> None:
        assert plan_limit_reason(info(status="rejected", rate_limit_type="overage"), "sonnet", 0.9)

    @pytest.mark.parametrize(
        ("window", "model", "stops"),
        [
            ("seven_day_opus", "opus", True),
            ("seven_day_opus", "claude-opus-4-1", True),
            ("seven_day_opus", "Claude-OPUS", True),
            ("seven_day_opus", None, True),
            ("seven_day_opus", "sonnet", False),
            ("seven_day_opus", "haiku", False),
            ("seven_day_sonnet", "sonnet", True),
            ("seven_day_sonnet", "claude-sonnet-4-5", True),
            ("seven_day_sonnet", None, True),
            ("seven_day_sonnet", "opus", False),
            ("seven_day_sonnet", "haiku", False),
        ],
    )
    def test_model_specific_windows(self, window: str, model: str | None, stops: bool) -> None:
        for status in ("rejected", "allowed_warning"):
            got = plan_limit_reason(info(status=status, rate_limit_type=window, utilization=0.99), model, 0.9)
            assert bool(got) is stops, (status, window, model)

    @pytest.mark.parametrize(
        ("utilization", "stop_at", "stops"),
        [
            (0.95, 0.9, True),
            (0.9, 0.9, True),
            (0.89, 0.9, False),
            (0.5, 0.5, True),
            (1.0, 0.9, True),
            (95.0, 0.9, True),  # percent form
            (80.0, 0.9, False),
            (None, 0.9, False),
            (0.0, 0.9, False),
        ],
    )
    def test_warning_threshold(self, utilization: float | None, stop_at: float, stops: bool) -> None:
        got = plan_limit_reason(info(status="allowed_warning", utilization=utilization), "sonnet", stop_at)
        assert bool(got) is stops

    def test_allowed_with_high_utilization_carries_on(self) -> None:
        assert plan_limit_reason(info(status="allowed", utilization=0.99), "sonnet", 0.9) is None

    def test_reason_includes_percent_and_reset(self) -> None:
        reason = plan_limit_reason(info(status="allowed_warning", utilization=0.93, resets_at=1_700_000_000), None, 0.9)
        assert reason is not None
        assert "93%" in reason and "five_hour" in reason and "resets" in reason

    @pytest.mark.parametrize("resets_at", [0, None, 10**20, -(10**20)])
    def test_bad_reset_timestamps_do_not_crash(self, resets_at: int | None) -> None:
        reason = plan_limit_reason(info(status="rejected", resets_at=resets_at), None, 0.9)
        assert reason and "resets" not in reason

    def test_missing_attributes(self) -> None:
        assert plan_limit_reason(SimpleNamespace(), None, 0.9) is None
        assert plan_limit_reason(SimpleNamespace(status="rejected"), None, 0.9) == "the plan plan limit is reached"

    def test_real_sdk_rate_limit_info(self) -> None:
        RateLimitInfo = sdk.RateLimitInfo if hasattr(sdk, "RateLimitInfo") else None  # noqa: N806
        if RateLimitInfo is None:
            pytest.skip("SDK has no RateLimitInfo")
        real = RateLimitInfo(status="allowed_warning", rate_limit_type="seven_day", utilization=0.97)
        assert plan_limit_reason(real, "sonnet", 0.9)
        assert plan_limit_reason(RateLimitInfo(status="allowed"), "sonnet", 0.9) is None


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"resets_at": 1_700_000_000}, 1_700_000_000.0),
        ({"resets_at": 1_700_000_000.5, "rate_limit_type": "seven_day"}, 1_700_000_000.5),
        ({"resets_at": None}, None),
        ({"resets_at": 0}, None),
        ({"resets_at": -5}, None),
        ({"resets_at": "soon"}, None),
        ({"resets_at": True}, None),
        ({"resets_at": 1_700_000_000, "rate_limit_type": "overage"}, None),  # not a plan window
    ],
)
def test_plan_reset_time(kw: dict[str, Any], expected: float | None) -> None:
    assert plan_reset_time(info(**kw)) == expected


def test_plan_reset_time_missing_attribute() -> None:
    assert plan_reset_time(SimpleNamespace()) is None


def test_limit_note() -> None:
    assert limit_note(info(status="allowed")) is None
    assert limit_note(info(status="rejected")) is None
    assert limit_note(info(status="allowed_warning", utilization=0.75)) == "plan usage warning: five_hour window at 75%"
    assert limit_note(info(status="allowed_warning", rate_limit_type=None)) == "plan usage warning: plan window"


@pytest.mark.parametrize(
    ("method", "ok", "kind"),
    [
        ("claude.ai", True, "plan"),
        ("CLAUDE_CODE_OAUTH_TOKEN", True, "plan"),
        ("ANTHROPIC_API_KEY", True, "metered"),
        ("ANTHROPIC_AUTH_TOKEN", True, "metered"),
        ("Amazon Bedrock", True, "metered"),
        ("Google Vertex AI", True, "metered"),
        ("Microsoft Foundry", True, "metered"),
        ("console", True, "metered"),
        ("API_KEY", True, "metered"),
        ("api-key", True, "metered"),
        ("something-new", True, "unknown"),
        ("claude.ai", False, "unknown"),
        ("none", False, "unknown"),
    ],
)
def test_billing_kind(method: str, ok: bool, kind: str) -> None:
    assert billing_kind(AuthInfo(ok, method, "")) == kind


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("Claude AI usage limit reached|1700000000", True),
        ("You've hit your 5-hour limit", True),
        ("Weekly limit reached", True),
        ("API Error: 429 rate_limit_error", True),
        ("Credit balance is too low", True),
        ("You're out of extra usage", True),
        ("billing_error", True),
        ("tests failed", False),
        ("", False),
    ],
)
def test_looks_like_limit_error(text: str, hit: bool) -> None:
    assert looks_like_limit_error(text) is hit


# --------------------------------------------------------------------------- describe_tool


def test_describe_tool() -> None:
    assert describe_tool("Read", {"file_path": "src/a.py"}) == "Read src/a.py"
    assert describe_tool("Bash", {"command": "  pytest -q\nsecond line"}) == "Bash pytest -q"
    assert describe_tool("Bash", {"command": "x" * 300}) == "Bash " + "x" * 100
    assert describe_tool("TodoWrite", {"todos": []}) == "TodoWrite"
    assert describe_tool("Glob", None) == "Glob"


# --------------------------------------------------------------------------- build_options


def test_build_options(tmp_path: Path) -> None:
    schema = {"type": "object", "properties": {}}
    opts = ClaudeBackend().build_options(make_request(tmp_path, output_schema=schema))
    assert opts.cwd == str(tmp_path)
    assert opts.model == "sonnet"
    assert list(opts.tools) == ["Read", "Write", "Bash"]
    assert opts.allowed_tools == ["Read", "Write", "Bash", "mcp__grep__*"]
    assert set(DISALLOWED) <= set(opts.disallowed_tools)
    assert {"Agent", "Task", "AskUserQuestion"} <= set(opts.disallowed_tools)
    assert opts.permission_mode == "dontAsk"
    assert opts.strict_mcp_config is True
    assert opts.setting_sources == []
    assert opts.max_turns == 10
    assert opts.max_budget_usd == 1.5
    assert opts.output_format == {"type": "json_schema", "schema": schema}
    assert opts.mcp_servers == {"grep": {"type": "http", "url": "u"}}
    assert opts.system_prompt == {"type": "preset", "preset": "claude_code", "append": "APPEND"}
    assert "PreToolUse" in opts.hooks
    assert opts.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_build_options_without_schema(tmp_path: Path) -> None:
    assert ClaudeBackend().build_options(make_request(tmp_path)).output_format is None


def _hook(opts: Any):
    return opts.hooks["PreToolUse"][0].hooks[0]


def _decide(hook: Any, tool: str, data: dict[str, Any]) -> bool:
    out = asyncio.run(hook({"tool_name": tool, "tool_input": data}, None, None))
    return out == {}


def test_guard_hook_read_only(tmp_path: Path) -> None:
    rw = _hook(ClaudeBackend().build_options(make_request(tmp_path, read_only=False)))
    ro = _hook(ClaudeBackend().build_options(make_request(tmp_path, read_only=True)))
    target = {"file_path": str(tmp_path / "a.py")}
    assert _decide(rw, "Write", target)
    assert not _decide(ro, "Write", target)
    assert not _decide(rw, "Bash", {"command": "git push"})


def test_guard_hook_is_rooted_at_cwd(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    hook = _hook(ClaudeBackend().build_options(make_request(project)))
    assert not _decide(hook, "Write", {"file_path": str(project / ".git" / "config")})


def test_audit_counts_denials(tmp_path: Path) -> None:
    seen: list[bool] = []
    opts = ClaudeBackend().build_options(make_request(tmp_path), audit=lambda t, d, dec: seen.append(dec.allowed))
    _decide(_hook(opts), "Bash", {"command": "sudo ls"})
    assert seen == [False]


# --------------------------------------------------------------------------- _from_result / _from_exception


def result_msg(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        subtype="success", is_error=False, result="final text", total_cost_usd=0.12, num_turns=4,
        session_id="s1", structured_output=None, errors=None, api_error_status=None,
    )  # fmt: skip
    base.update(kw)
    return SimpleNamespace(**base)


def test_from_result_success(tmp_path: Path) -> None:
    r = ClaudeBackend._from_result(make_request(tmp_path), result_msg(), ["a"], ["w"])
    assert r.ok and r.subtype == "success" and r.error == ""
    assert r.text == "final text" and r.cost_usd == 0.12 and r.turns == 4 and r.session_id == "s1"
    assert r.warnings == ["w"] and not r.fatal


def test_from_result_text_fallback_and_none_numbers(tmp_path: Path) -> None:
    r = ClaudeBackend._from_result(
        make_request(tmp_path), result_msg(result=None, total_cost_usd=None, num_turns=None), ["a", "b"], []
    )
    assert r.text == "a\nb" and r.cost_usd == 0.0 and r.turns == 0


def test_from_result_structured(tmp_path: Path) -> None:
    req = make_request(tmp_path, output_schema={"type": "object"})
    r = ClaudeBackend._from_result(req, result_msg(structured_output={"status": "done"}), [], [])
    assert r.ok and r.structured == {"status": "done"}


def test_from_result_missing_structured(tmp_path: Path) -> None:
    req = make_request(tmp_path, output_schema={"type": "object"})
    r = ClaudeBackend._from_result(req, result_msg(), [], [])
    assert not r.ok and r.subtype == "no_structured_output"


def test_from_result_is_error(tmp_path: Path) -> None:
    r = ClaudeBackend._from_result(make_request(tmp_path), result_msg(is_error=True, result="boom"), [], [])
    assert not r.ok and r.error == "boom" and not r.fatal


def test_from_result_max_turns(tmp_path: Path) -> None:
    msg = result_msg(subtype="error_max_turns", result=None, errors=["hit max turns", "second"])
    r = ClaudeBackend._from_result(make_request(tmp_path), msg, [], [])
    assert not r.ok and r.subtype == "error_max_turns" and r.error == "hit max turns; second"


def test_from_result_subtype_as_error(tmp_path: Path) -> None:
    msg = result_msg(subtype="error_during_execution", result=None)
    r = ClaudeBackend._from_result(make_request(tmp_path), msg, [], [])
    assert r.error == "error_during_execution"


@pytest.mark.parametrize(("status", "fatal"), [(401, True), (403, True), (500, False), (None, False)])
def test_from_result_api_status(tmp_path: Path, status: int | None, fatal: bool) -> None:
    msg = result_msg(is_error=True, api_error_status=status)
    assert ClaudeBackend._from_result(make_request(tmp_path), msg, [], []).fatal is fatal


def test_from_exception_cli_not_found() -> None:
    class CLINotFoundError(Exception):
        pass

    r = ClaudeBackend._from_exception(CLINotFoundError("x"), "", 1.0, 2)
    assert r.fatal and r.subtype == "cli_not_found" and r.denied == 2


def test_from_exception_auth() -> None:
    r = ClaudeBackend._from_exception(RuntimeError("process exited"), "Error: Not logged in", 1.0, 0)
    assert r.fatal and r.error.startswith("not authenticated")


def test_from_exception_limit() -> None:
    r = ClaudeBackend._from_exception(RuntimeError("Claude AI usage limit reached"), "", 1.0, 0)
    assert not r.fatal and r.subtype == "plan_limit"
    assert r.error.startswith("stopped to protect your Claude plan")


def test_from_exception_other() -> None:
    r = ClaudeBackend._from_exception(ValueError("weird"), "tail", 1.0, 0)
    assert not r.fatal and r.subtype == "exception"
    assert r.error.startswith("ValueError: weird") and "tail" in r.error


# --------------------------------------------------------------------------- run() (no engine)


def patch_consume(monkeypatch: pytest.MonkeyPatch, behaviour: Any) -> None:
    async def fake(self: ClaudeBackend, req: AgentRequest, options: Any, on_event: Any) -> AgentResult:
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour

    monkeypatch.setattr(ClaudeBackend, "_consume", fake)


def run(req: AgentRequest) -> AgentResult:
    return asyncio.run(ClaudeBackend().run(req))


def test_run_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_consume(monkeypatch, TimeoutError())
    r = run(make_request(tmp_path))
    assert not r.ok and r.subtype == "timeout" and "timed out" in r.error


def test_run_real_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def hang(self: Any, req: Any, options: Any, on_event: Any) -> AgentResult:
        await asyncio.sleep(30)
        return AgentResult(True)

    monkeypatch.setattr(ClaudeBackend, "_consume", hang)
    r = run(make_request(tmp_path, timeout_s=0.2))
    assert r.subtype == "timeout"


def test_run_plan_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_consume(monkeypatch, PlanLimit("the five_hour plan limit is reached"))
    transcript = tmp_path / "t.jsonl"
    r = run(make_request(tmp_path, transcript=transcript))
    assert not r.ok and r.subtype == "plan_limit"
    assert r.error.startswith("stopped to protect your Claude plan")
    assert "plan-limit" in transcript.read_text(encoding="utf-8")
    assert r.resets_at is None  # the limit did not say when it resets


def test_run_plan_limit_carries_the_reset_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_consume(monkeypatch, PlanLimit("the five_hour plan limit is reached", resets_at=1_700_000_000.0))
    r = run(make_request(tmp_path))
    assert r.subtype == "plan_limit" and r.resets_at == 1_700_000_000.0


def test_run_generic_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_consume(monkeypatch, RuntimeError("kaboom"))
    r = run(make_request(tmp_path))
    assert not r.ok and r.subtype == "exception" and "kaboom" in r.error


def test_run_result_limit_text_becomes_plan_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_consume(monkeypatch, AgentResult(False, error="Claude AI usage limit reached", subtype="error"))
    r = run(make_request(tmp_path))
    assert r.subtype == "plan_limit" and r.error.startswith("stopped to protect your Claude plan")


def test_run_result_auth_text_becomes_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_consume(monkeypatch, AgentResult(False, error="Invalid API key", subtype="error"))
    r = run(make_request(tmp_path))
    assert r.fatal and r.error.startswith("not authenticated")


def test_run_success_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_consume(monkeypatch, AgentResult(True, text="hi", cost_usd=0.2, subtype="success"))
    transcript = tmp_path / "t.jsonl"
    r = run(make_request(tmp_path, transcript=transcript))
    assert r.ok and r.text == "hi" and r.seconds >= 0
    assert '"kind": "result"' in transcript.read_text(encoding="utf-8")


def test_run_missing_sdk_is_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_sdk(self: Any, req: Any, audit: Any = None) -> None:
        raise ImportError("No module named claude_agent_sdk")

    monkeypatch.setattr(ClaudeBackend, "build_options", no_sdk)
    r = run(make_request(tmp_path))
    assert r.fatal and "not installed" in r.error


# --------------------------------------------------------------------------- _consume with a fake stream


def fake_stream(monkeypatch: pytest.MonkeyPatch, messages: list[Any]) -> None:
    def query(prompt: str, options: Any):
        async def gen():
            for m in messages:
                yield m

        return gen()

    monkeypatch.setattr(sdk, "query", query)


def sdk_result(**kw: Any) -> Any:
    base: dict[str, Any] = dict(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="s",
        total_cost_usd=0.01, result="done",
    )  # fmt: skip
    base.update(kw)
    return sdk.ResultMessage(**base)


def rate_event(**kw: Any) -> Any:
    return sdk.RateLimitEvent(rate_limit_info=sdk.RateLimitInfo(**kw), uuid="u", session_id="s")


needs_rate_events = pytest.mark.skipif(not hasattr(sdk, "RateLimitEvent"), reason="SDK has no RateLimitEvent")


def consume(tmp_path: Path, events: list[tuple[str, str]] | None = None, **req_kw: Any) -> AgentResult:
    sink = (lambda k, t: events.append((k, t))) if events is not None else None
    return asyncio.run(ClaudeBackend().run(make_request(tmp_path, **req_kw), on_event=sink))


def test_consume_collects_text_and_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = [sdk.TextBlock(text="thinking"), sdk.ToolUseBlock(id="1", name="Read", input={"file_path": "a.py"})]
    fake_stream(monkeypatch, [sdk.AssistantMessage(content=content, model="m"), sdk_result()])
    events: list[tuple[str, str]] = []
    r = consume(tmp_path, events)
    assert r.ok and r.text == "done"
    assert events == [("text", "thinking"), ("tool", "Read a.py")]


def test_consume_without_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_stream(monkeypatch, [sdk.AssistantMessage(content=[sdk.TextBlock(text="partial")], model="m")])
    r = consume(tmp_path)
    assert not r.ok and "without a result" in r.error and r.text == "partial"


def test_consume_mcp_warnings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init = sdk.SystemMessage(
        subtype="init",
        data={"mcp_servers": [{"name": "grep", "status": "failed"}, {"name": "c7", "status": "connected"}]},
    )
    fake_stream(monkeypatch, [init, sdk_result()])
    assert consume(tmp_path).warnings == ["MCP server 'grep' is failed"]


@needs_rate_events
@pytest.mark.parametrize(
    "kw",
    [
        {"status": "rejected", "rate_limit_type": "five_hour"},
        {"status": "allowed", "rate_limit_type": "overage"},
        {"status": "allowed_warning", "rate_limit_type": "seven_day", "utilization": 0.95},
    ],
)
def test_consume_rate_limit_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kw: dict[str, Any]) -> None:
    after = sdk.AssistantMessage(content=[sdk.ToolUseBlock(id="1", name="Bash", input={"command": "x"})], model="m")
    fake_stream(monkeypatch, [rate_event(**kw), after, sdk_result()])
    events: list[tuple[str, str]] = []
    r = consume(tmp_path, events)
    assert not r.ok and r.subtype == "plan_limit"
    assert not any(k == "tool" for k, _ in events), "nothing may run after the limit event"


@needs_rate_events
@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"status": "rejected", "rate_limit_type": "five_hour", "resets_at": 1_700_000_000}, 1_700_000_000.0),
        ({"status": "allowed_warning", "rate_limit_type": "seven_day", "utilization": 0.95, "resets_at": 1_700_600_000},
         1_700_600_000.0),
        ({"status": "rejected", "rate_limit_type": "five_hour"}, None),
        ({"status": "allowed", "rate_limit_type": "overage", "resets_at": 1_700_000_000}, None),
    ],
)  # fmt: skip
def test_consume_rate_limit_reports_when_the_window_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kw: dict[str, Any], expected: float | None
) -> None:
    fake_stream(monkeypatch, [rate_event(**kw), sdk_result()])
    r = consume(tmp_path)
    assert r.subtype == "plan_limit" and r.resets_at == expected


@needs_rate_events
def test_consume_rate_limit_warning_below_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_stream(
        monkeypatch,
        [rate_event(status="allowed_warning", rate_limit_type="five_hour", utilization=0.5), sdk_result()],
    )
    events: list[tuple[str, str]] = []
    r = consume(tmp_path, events)
    assert r.ok
    assert events == [("limit", "plan usage warning: five_hour window at 50%")]


@needs_rate_events
def test_consume_other_model_window_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_stream(monkeypatch, [rate_event(status="rejected", rate_limit_type="seven_day_opus"), sdk_result()])
    assert consume(tmp_path, model="sonnet").ok


@pytest.mark.parametrize("error", ["rate_limit", "billing_error"])
def test_consume_assistant_error_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str) -> None:
    msg = sdk.AssistantMessage(content=[sdk.TextBlock(text="x")], model="m")
    msg.error = error
    fake_stream(monkeypatch, [msg, sdk_result()])
    r = consume(tmp_path)
    assert r.subtype == "plan_limit" and error in r.error


def test_backend_module_has_no_top_level_sdk_import() -> None:
    src = Path(backend_mod.__file__).read_text(encoding="utf-8")
    assert "\nimport claude_agent_sdk" not in src and "\nfrom claude_agent_sdk" not in src
