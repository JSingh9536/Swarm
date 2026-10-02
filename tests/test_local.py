from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import swarm.local as local
from swarm.backend import AgentRequest
from swarm.guard import Policy
from swarm.local import MAX_SHELL_TIMEOUT, MODEL_LICENSES, LocalBackend, OllamaClient, ToolExecutor, model_license
from swarm.roles import Role


def make_request(tmp_path: Path, **over: Any) -> AgentRequest:
    role = Role("developer", "d", "p\n", ("Read", "Write", "Bash"), "m", 10, Path("developer.md"))
    fields: dict[str, Any] = dict(
        role=role, label="W1", prompt="do it", cwd=tmp_path, model="m", tools=["Read", "Write", "Bash"],
        allowed_tools=[], mcp_servers={}, read_only=False, max_turns=8, max_budget_usd=None,
        timeout_s=30, system_append="be careful",
    )  # fmt: skip
    fields.update(over)
    return AgentRequest(**fields)


# --------------------------------------------------------------------------- ToolExecutor (real FS + real Policy)


def executor(tmp_path: Path, read_only: bool = False) -> ToolExecutor:
    return ToolExecutor(Policy(tmp_path, read_only=read_only), tmp_path)


def test_read_and_write(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    ex = executor(tmp_path)
    assert ex.run("read_file", {"path": "a.txt"}) == "hello"
    assert "wrote" in ex.run("write_file", {"path": "sub/b.txt", "content": "x"})
    assert (tmp_path / "sub" / "b.txt").read_text(encoding="utf-8") == "x"
    assert ex.denied == 0


def test_edit(tmp_path: Path) -> None:
    (tmp_path / "c.py").write_text("x = 1\n", encoding="utf-8")
    ex = executor(tmp_path)
    assert "edited" in ex.run("edit_file", {"path": "c.py", "old": "x = 1", "new": "x = 2"})
    assert (tmp_path / "c.py").read_text(encoding="utf-8") == "x = 2\n"
    assert "not found" in ex.run("edit_file", {"path": "c.py", "old": "missing", "new": "y"})


def test_write_outside_project_is_blocked(tmp_path: Path) -> None:
    ex = executor(tmp_path)
    out = ex.run("write_file", {"path": "C:/Windows/evil.txt", "content": "x"})
    assert "blocked by guard" in out and ex.denied == 1


def test_secret_read_is_blocked(tmp_path: Path) -> None:
    ex = executor(tmp_path)
    assert "blocked by guard" in ex.run("read_file", {"path": str(Path.home() / ".ssh" / "id_rsa")})
    assert ex.denied == 1


def test_shell_gated_by_policy(tmp_path: Path) -> None:
    ex = executor(tmp_path)
    assert "blocked by guard" in ex.run("run_shell", {"command": "git push origin main"})


class _FakeProc:
    returncode = 0

    def __init__(self, capture: dict) -> None:
        self._cap = capture

    def communicate(self, timeout=None):  # type: ignore[no-untyped-def]
        self._cap["timeout"] = timeout
        return ("output here", "")


def test_run_shell_executes_through_bash_not_cmd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The guard parses bash; cmd.exe would strip `^`, so run_shell MUST invoke bash with an argv list.
    cap: dict = {}
    monkeypatch.setattr(local, "find_bash", lambda: "C:/git/bin/bash.exe")
    monkeypatch.setattr(local.subprocess, "Popen", lambda argv, **kw: cap.update(argv=argv, kw=kw) or _FakeProc(cap))
    out = executor(tmp_path).run("run_shell", {"command": "echo hi"})
    assert cap["argv"] == ["C:/git/bin/bash.exe", "-c", "echo hi"]
    assert cap["kw"].get("shell") in (None, False), "must never use shell=True (that is cmd.exe on Windows)"
    assert "exit 0" in out


def test_run_shell_uses_the_project_virtualenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    py = tmp_path / ".venv" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    cap: dict = {}
    monkeypatch.setattr(local, "find_bash", lambda: "C:/git/bin/bash.exe")
    monkeypatch.setattr(local.subprocess, "Popen", lambda argv, **kw: cap.update(argv=argv, kw=kw) or _FakeProc(cap))
    ex = executor(tmp_path)

    ex.run("run_shell", {"command": "python -m pytest -q"})
    assert cap["argv"][2].endswith("/.venv/Scripts/python.exe -m pytest -q")
    assert "\\" not in cap["argv"][2]  # bash would eat backslashes

    ex.run("run_shell", {"command": "pytest -q tests"})
    assert cap["argv"][2].endswith("/.venv/Scripts/python.exe -m pytest -q tests")

    ex.run("run_shell", {"command": "pytest -q && echo done"})  # shell operators: left exactly as approved
    assert cap["argv"][2] == "pytest -q && echo done"


def test_run_shell_fails_closed_without_bash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local, "find_bash", lambda: None)
    assert "no POSIX bash" in executor(tmp_path).run("run_shell", {"command": "echo hi"})


def test_run_shell_clamps_model_supplied_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap: dict = {}
    monkeypatch.setattr(local, "find_bash", lambda: "bash")
    monkeypatch.setattr(local.subprocess, "Popen", lambda argv, **kw: _FakeProc(cap))
    executor(tmp_path).run("run_shell", {"command": "echo hi", "timeout": 999999})
    assert cap["timeout"] <= MAX_SHELL_TIMEOUT


def test_read_only_executor_refuses_writes(tmp_path: Path) -> None:
    ex = executor(tmp_path, read_only=True)
    assert "blocked by guard" in ex.run("write_file", {"path": "a.txt", "content": "x"})


def test_grep_and_glob(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("import os\nTODO fix\n", encoding="utf-8")
    (tmp_path / "y.py").write_text("clean\n", encoding="utf-8")
    ex = executor(tmp_path)
    assert "x.py" in ex.run("grep", {"pattern": "TODO", "path": "."})
    globbed = ex.run("glob", {"pattern": "*.py", "path": "."})
    assert "x.py" in globbed and "y.py" in globbed


def test_unknown_tool(tmp_path: Path) -> None:
    assert "unknown tool" in executor(tmp_path).run("fly", {})


# --------------------------------------------------------------------------- OllamaClient (mocked HTTP)


class FakeResp:
    def __init__(self, body: dict[str, Any]) -> None:
        self._b = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._b

    def __enter__(self) -> FakeResp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


def patch_urlopen(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any] | Exception) -> list[Any]:
    calls: list[Any] = []

    def urlopen(req: Any, timeout: float = 0) -> FakeResp:
        calls.append(req)
        if isinstance(body, Exception):
            raise body
        return FakeResp(body)

    monkeypatch.setattr(local.urllib.request, "urlopen", urlopen)
    return calls


def test_client_is_up_and_models(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_urlopen(monkeypatch, {"models": [{"name": "qwen2.5-coder:latest"}, {"name": "mistral:latest"}]})
    c = OllamaClient("http://localhost:11434")
    assert c.is_up() is True
    assert c.models() == ["qwen2.5-coder:latest", "mistral:latest"]


def test_client_down(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    patch_urlopen(monkeypatch, urllib.error.URLError("refused"))
    c = OllamaClient()
    assert c.is_up() is False and c.models() == []


def test_client_chat_parses_message(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_urlopen(monkeypatch, {"message": {"role": "assistant", "content": "hi"}})
    msg = OllamaClient().chat("m", [{"role": "user", "content": "x"}])
    assert msg == {"role": "assistant", "content": "hi"}


# --------------------------------------------------------------------------- LocalBackend loop (fake client)


class FakeClient:
    def __init__(self, messages: list[dict[str, Any]], up: bool = True) -> None:
        self._messages = list(messages)
        self._up = up
        self.base_url = "http://localhost:11434"
        self.chats: list[dict[str, Any]] = []

    def is_up(self) -> bool:
        return self._up

    def chat(self, model: str, messages: list[dict[str, Any]], tools: Any = None, fmt: Any = None,
             timeout: float | None = None) -> dict[str, Any]:  # fmt: skip
        self.chats.append({"tools": tools, "fmt": fmt})
        return self._messages.pop(0) if self._messages else {"content": ""}


def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"function": {"name": name, "arguments": args}}


def run_local(req: AgentRequest, messages: list[dict[str, Any]], up: bool = True) -> Any:
    be = LocalBackend(model="m")
    be.client = FakeClient(messages, up=up)  # type: ignore[assignment]
    return be._run_sync(req, None), be.client


def test_backend_runs_tool_loop(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    msgs = [
        {"content": "let me read it", "tool_calls": [call("read_file", {"path": "a.txt"})]},
        {"content": "the file says hello world"},
    ]
    res, _ = run_local(make_request(tmp_path), msgs)
    assert res.ok and res.cost_usd == 0.0 and res.turns == 2
    assert "hello world" in res.text


def test_backend_structured_output(tmp_path: Path) -> None:
    schema = {"type": "object", "properties": {"status": {"type": "string"}}}
    msgs = [
        {"content": "done"},  # no tool calls → loop ends (read-only role: prose without tools is a valid answer)
        {"content": json.dumps({"status": "done"})},  # the follow-up structured call
    ]
    res, client = run_local(make_request(tmp_path, output_schema=schema, read_only=True), msgs)
    assert res.ok and res.structured == {"status": "done"}
    assert client.chats[-1]["fmt"] == schema  # the final call was constrained to the schema


def test_backend_missing_structured(tmp_path: Path) -> None:
    schema = {"type": "object"}
    msgs = [{"content": "done"}, {"content": "not json at all"}]
    res, _ = run_local(make_request(tmp_path, output_schema=schema), msgs)
    assert not res.ok and res.subtype == "no_structured_output"


def test_backend_ollama_down(tmp_path: Path) -> None:
    res, _ = run_local(make_request(tmp_path), [], up=False)
    assert not res.ok and res.fatal and res.subtype == "ollama_down" and "ollama pull" in res.error


def test_backend_counts_guard_denials(tmp_path: Path) -> None:
    msgs = [
        {"content": "pushing", "tool_calls": [call("run_shell", {"command": "git push"})]},
        {"content": "ok, I will not push"},
    ]
    res, _ = run_local(make_request(tmp_path), msgs)
    assert res.denied == 1 and res.ok


def test_backend_read_only_role_gets_no_write_tools(tmp_path: Path) -> None:
    msgs = [{"content": "nothing to do"}]
    _, client = run_local(make_request(tmp_path, read_only=True), msgs)
    names = {t["function"]["name"] for t in client.chats[0]["tools"]}
    assert "write_file" not in names and "edit_file" not in names and "read_file" in names


def test_backend_max_turns(tmp_path: Path) -> None:
    # the model keeps calling a tool forever; the loop must stop at max_turns
    forever = {"content": "again", "tool_calls": [call("glob", {"pattern": "*"})]}
    res, client = run_local(make_request(tmp_path, max_turns=3), [forever] * 10)
    assert res.turns == 3 and len(client.chats) == 3


def test_model_license_is_conservative() -> None:
    # qwen2.5-coder's licence varies by size, so the note must warn, not assert Apache blindly
    assert "verify" in model_license("qwen2.5-coder").lower()
    assert "verify" in model_license("qwen2.5-coder:7b").lower()
    assert "Apache" in model_license("mistral")
    assert "verify" in model_license("llama3.1").lower()  # unknown family
    # the custom-licence model must not be advertised as permissive
    assert "deepseek-coder-v2" not in MODEL_LICENSES


def test_chat_requests_a_real_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama defaults to a 4096-token context and truncates silently; the client must ask for more."""
    from swarm import local

    sent: dict = {}
    client = OllamaClient("http://localhost:1")
    monkeypatch.setattr(client, "_post", lambda path, payload, timeout=None: sent.update(payload) or {"message": {}})

    monkeypatch.delenv("SWARM_OLLAMA_NUM_CTX", raising=False)
    client.chat("m", [{"role": "user", "content": "hi"}])
    assert sent["options"]["num_ctx"] == local.DEFAULT_NUM_CTX >= 8192

    monkeypatch.setenv("SWARM_OLLAMA_NUM_CTX", "8192")
    client.chat("m", [{"role": "user", "content": "hi"}])
    assert sent["options"]["num_ctx"] == 8192

    monkeypatch.setenv("SWARM_OLLAMA_NUM_CTX", "12")  # too small: clamped up
    assert local.num_ctx() == local.MIN_NUM_CTX
    monkeypatch.setenv("SWARM_OLLAMA_NUM_CTX", "lots")  # not a number: default
    assert local.num_ctx() == local.DEFAULT_NUM_CTX


TOOLS = {"read_file", "write_file", "run_shell"}


def test_calls_from_text_bare_json_object() -> None:
    from swarm.local import calls_from_text

    text = '{"name": "write_file", "arguments": {"path": "hello.py", "content": "def hello():\n    return 1\n"}}'
    calls = calls_from_text(text, TOOLS)
    assert [c["function"]["name"] for c in calls] == ["write_file"]
    assert calls[0]["function"]["arguments"]["path"] == "hello.py"


def test_calls_from_text_fences_tags_arrays_and_several() -> None:
    from swarm.local import calls_from_text

    fenced = 'I will read it.\n```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```'
    assert calls_from_text(fenced, TOOLS)[0]["function"]["arguments"] == {"path": "a.py"}
    tagged = '<tool_call>\n{"name": "run_shell", "arguments": {"command": "ls"}}\n</tool_call>'
    assert calls_from_text(tagged, TOOLS)[0]["function"]["name"] == "run_shell"
    array = '[{"name": "read_file", "arguments": {"path": "a"}}, {"name": "read_file", "parameters": {"path": "b"}}]'
    assert [c["function"]["arguments"]["path"] for c in calls_from_text(array, TOOLS)] == ["a", "b"]
    two = '{"name": "read_file", "arguments": {"path": "a"}}\n{"name": "read_file", "arguments": {"path": "b"}}'
    assert len(calls_from_text(two, TOOLS)) == 2
    string_args = '{"name": "read_file", "arguments": "{\\"path\\": \\"a\\"}"}'
    assert calls_from_text(string_args, TOOLS)[0]["function"]["arguments"] == {"path": "a"}


def test_calls_from_text_ignores_prose_unknown_tools_and_bad_shapes() -> None:
    from swarm.local import MAX_TEXT_CALLS, calls_from_text

    assert calls_from_text("All done. The file now has {curly} braces in prose.", TOOLS) == []
    assert calls_from_text('{"name": "delete_everything", "arguments": {}}', TOOLS) == []
    assert calls_from_text('{"name": "read_file", "arguments": ["not", "a", "dict"]}', TOOLS) == []
    assert calls_from_text('{"status": "ok", "summary": "a report, not a call"}', TOOLS) == []
    many = "\n".join('{"name": "read_file", "arguments": {"path": "x"}}' for _ in range(50))
    assert len(calls_from_text(many, TOOLS)) == MAX_TEXT_CALLS


def test_text_tool_call_is_executed_through_the_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A call written as text must run through the same executor and policy as a structured one."""
    replies = iter([
        {"content": '{"name": "write_file", "arguments": {"path": "hello.py", "content": "x = 1\n"}}'},
        {"content": '{"name": "write_file", "arguments": {"path": "C:/Windows/swarm_escape.py", "content": "x"}}'},
        {"content": "Done."},
    ])
    backend = LocalBackend("m")
    monkeypatch.setattr(backend.client, "is_up", lambda: True)
    monkeypatch.setattr(backend.client, "chat", lambda *a, **k: next(replies))
    result = backend._run_sync(make_request(tmp_path, prompt="write hello.py"), None)
    assert (tmp_path / "hello.py").read_text() == "x = 1\n"
    assert result.denied == 1  # the out-of-project write was refused by the guard, not executed
    assert result.turns == 3 and result.text == "Done."


def test_claiming_done_without_any_tool_call_is_challenged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A small model that reports 'done, verified' before touching anything is told so and must then do the work."""
    from swarm.local import NUDGE

    seen: list[list[dict]] = []
    replies = iter([
        {"content": "STATUS: done. Changed: hello.py. Verified: tests pass"},
        {"content": '{"name": "write_file", "arguments": {"path": "hello.py", "content": "x = 1"}}'},
        {"content": "STATUS: done"},
    ])

    def fake_chat(model, messages, **kw):
        seen.append(list(messages))
        return next(replies)

    backend = LocalBackend("m")
    monkeypatch.setattr(backend.client, "is_up", lambda: True)
    monkeypatch.setattr(backend.client, "chat", fake_chat)
    result = backend._run_sync(make_request(tmp_path), None)
    assert (tmp_path / "hello.py").exists()
    assert seen[1][-1]["content"] == NUDGE  # the false "done" was challenged
    assert "Verified: tests pass" not in result.text  # and is not passed on as the agent's answer


def test_nudges_are_bounded_and_skipped_for_read_only_roles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from swarm.local import MAX_NUDGES

    count = {"n": 0}

    def always_prose(model, messages, **kw):
        count["n"] += 1
        return {"content": "I think it is fine."}

    backend = LocalBackend("m")
    monkeypatch.setattr(backend.client, "is_up", lambda: True)
    monkeypatch.setattr(backend.client, "chat", always_prose)
    backend._run_sync(make_request(tmp_path), None)
    assert count["n"] == MAX_NUDGES + 1  # bounded: never loops forever

    count["n"] = 0
    backend._run_sync(make_request(tmp_path, read_only=True), None)
    assert count["n"] == 1  # a reviewer may answer in prose without using a tool
