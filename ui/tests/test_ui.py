"""Offline tests: security checks, argv validation, run parsing, and the HTTP server on a loopback port."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from swarm_ui import monitor, runs, security
from swarm_ui.jobs import JobError, JobManager, build_argv
from swarm_ui.server import App, make_server

TOKEN = "t" * 43


# ------------------------------------------------------------------ security primitives
def test_host_and_origin_checks():
    assert security.host_ok("127.0.0.1:8765", 8765) and security.host_ok("localhost:8765", 8765)
    assert not security.host_ok("evil.example:8765", 8765)
    assert not security.host_ok("127.0.0.1:9999", 8765) and not security.host_ok(None, 8765)
    assert security.origin_ok(None, 8765) and security.origin_ok("http://127.0.0.1:8765", 8765)
    assert not security.origin_ok("http://evil.example", 8765)
    assert not security.origin_ok("https://127.0.0.1:8765", 8765)
    assert not security.origin_ok("http://127.0.0.1:1", 8765) and not security.origin_ok("null", 8765)


def test_token_check_and_loopback_only():
    assert security.token_ok(f"Bearer {TOKEN}", TOKEN)
    assert not security.token_ok("Bearer nope", TOKEN) and not security.token_ok(TOKEN, TOKEN)
    assert not security.token_ok(None, TOKEN)
    assert security.require_loopback("localhost") == "127.0.0.1"
    for bad in ("0.0.0.0", "192.168.1.5", "::", ""):
        with pytest.raises(ValueError):
            security.require_loopback(bad)


# ------------------------------------------------------------------ argv building
@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "workspace" / "proj").mkdir(parents=True)
    return tmp_path


def test_build_argv_accepts_good_requests(root):
    spec = {"kind": "run", "text": "build x", "budget": 2, "model": "haiku", "rounds": 1, "commit": True}
    argv = build_argv(root, spec)
    assert argv[1:4] == ["-m", "swarm", "run"] and "--yes" in argv and "--nice" in argv
    assert argv[-2:] == ["--", "build x"] and "--commit" in argv and argv[argv.index("--budget") + 1] == "2"
    argv = build_argv(root, {"kind": "audit", "project": "proj", "fix": True})
    assert "--fix" in argv and str(root / "workspace" / "proj") in argv


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "rm"}, {"kind": "run", "text": ""}, {"kind": "run", "text": "x" * 4001},
        {"kind": "run", "text": "x", "budget": 99}, {"kind": "run", "text": "x", "budget": 0},
        {"kind": "run", "text": "x", "budget": True}, {"kind": "run", "text": "x", "model": "gpt"},
        {"kind": "run", "text": "x", "rounds": 50}, {"kind": "run", "text": "x", "project": "../etc"},
        {"kind": "run", "text": "x", "project": "missing"}, {"kind": "review"}, {"kind": "run", "text": 5},
    ],
)
def test_build_argv_rejects_bad_requests(root, spec):
    with pytest.raises(JobError):
        build_argv(root, spec)


def test_text_starting_with_dash_cannot_become_an_option(root):
    argv = build_argv(root, {"kind": "research", "text": "--project C:/secret"})
    assert argv[-2:] == ["--", "--project C:/secret"]


class FakeProc:
    pid = 1

    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code


def test_one_job_at_a_time_and_stop(root, tmp_path, monkeypatch):
    procs = []

    def spawn(argv, cwd, fh):
        fh.write(b"hello \x1b[31mred\x1b[0m\n")
        procs.append(FakeProc())
        return procs[-1]

    monkeypatch.setattr("swarm_ui.jobs._kill_tree", lambda p: setattr(p, "code", 1))
    jm = JobManager(root, tmp_path / "data", spawn=spawn)
    job = jm.start({"kind": "research", "text": "q"})
    with pytest.raises(JobError, match="already running"):
        jm.start({"kind": "research", "text": "q2"})
    assert jm.tail(job, 0)["text"] == "hello red\n"
    assert jm.stop(job.id).status == "stopped"
    procs[0].code = 0
    assert jm.current() is None
    assert jm.start({"kind": "research", "text": "q3"}).id != job.id
    assert jm.get("../../x") is None


# ------------------------------------------------------------------ run artifacts
def make_run(root: Path, project="proj", run="20260101-120000", summary=True) -> Path:
    d = root / "workspace" / project / ".swarm" / "runs" / run
    d.mkdir(parents=True)
    (d / "progress.md").write_text("- 12:00:00 PHASE plan\n- 12:00:01 architect [plan] ok ($0.25)\n", encoding="utf-8")
    (d / "task.md").write_text("do the thing\n", encoding="utf-8")
    if summary:
        data = {"status": "success", "task": "do the thing", "cost_usd": 1.5, "seconds": 30,
                "gate": [{"command": "pytest", "ok": True}]}
        (d / "summary.json").write_text(json.dumps(data), encoding="utf-8")
    return d


def test_overview_and_detail(root):
    make_run(root)
    make_run(root, run="20260101-130000", summary=False)
    ov = runs.overview(root / "workspace")
    assert ov["projects"] == ["proj"] and ov["run_count"] == 2
    by = {r["run"]: r for r in ov["runs"]}
    assert by["20260101-120000"]["status"] == "success" and by["20260101-120000"]["cost_usd"] == 1.5
    assert by["20260101-130000"]["cost_usd"] == 0.25
    d = runs.detail(root / "workspace", "proj", "20260101-120000")
    assert d["events"][0]["text"] == "PHASE plan" and "summary.json" in d["files"] and d["gates"][0]["ok"]


def test_artifact_whitelist_blocks_traversal(root):
    make_run(root)
    ws = root / "workspace"
    (root / "secret.txt").write_text("nope", encoding="utf-8")
    assert runs.artifact(ws, "proj", "20260101-120000", "task.md").startswith("do the thing")
    for name in ("../../../../../secret.txt", "..", "secret.txt", "gate-1.txt", "task.md/../x"):
        assert runs.artifact(ws, "proj", "20260101-120000", name) is None
    assert runs.artifact(ws, "..", "20260101-120000", "task.md") is None
    assert runs.artifact(ws, "proj", "../../x", "task.md") is None


# ------------------------------------------------------------------ live server
@pytest.fixture
def server(root, tmp_path):
    make_run(root)
    app = App(root, tmp_path / "data", TOKEN)
    srv = make_server(app)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield app
    srv.shutdown()
    srv.server_close()


def call(app, path, *, method="GET", token=TOKEN, host=None, origin=None, body=None, ctype="application/json"):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", method=method, data=body)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if host:
        req.add_header("Host", host)
    if origin:
        req.add_header("Origin", origin)
    if body is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_server_binds_loopback_only(server):
    assert make_server.__module__  # sanity
    with pytest.raises(ValueError):
        make_server(server, host="0.0.0.0")


def test_static_needs_no_token_but_has_security_headers(server):
    status, headers, body = call(server, "/", token=None)
    assert status == 200 and b"Swarm Control" in body
    assert "default-src 'none'" in headers["Content-Security-Policy"] and headers["X-Content-Type-Options"] == "nosniff"
    assert call(server, "/app.js", token=None)[0] == 200
    assert call(server, "/../pyproject.toml", token=None)[0] == 404
    assert call(server, "/swarm_ui/server.py", token=None)[0] == 404


def test_api_requires_token_host_and_origin(server):
    assert call(server, "/api/overview", token=None)[0] == 401
    assert call(server, "/api/overview", token="wrong")[0] == 401
    assert call(server, "/api/overview", host="evil.example")[0] == 403
    assert call(server, "/api/overview", origin="http://evil.example")[0] == 403
    status, _, body = call(server, "/api/overview")
    assert status == 200 and json.loads(body)["run_count"] == 1


def test_api_run_routes(server):
    status, _, body = call(server, "/api/runs/proj/20260101-120000")
    assert status == 200 and json.loads(body)["status"] == "success"
    assert call(server, "/api/runs/proj/20260101-120000/file/task.md")[0] == 200
    assert call(server, "/api/runs/proj/20260101-120000/file/..%2F..%2Fsecret.txt")[0] == 404
    assert call(server, "/api/runs/..%2F/20260101-120000")[0] == 404
    assert call(server, "/api/nope")[0] == 404


def test_post_validation(server):
    post = lambda b, **kw: call(server, "/api/jobs", method="POST", body=b, **kw)  # noqa: E731
    assert post(b'{"kind":"run","text":"x"}', token=None)[0] == 401
    assert post(b'{"kind":"run","text":"x"}', origin="http://evil.example")[0] == 403
    assert post(b'{"kind":"run","text":"x"}', ctype="text/plain")[0] == 400
    assert post(b"not json")[0] == 400
    assert post(b"[]")[0] == 400
    assert post(b'{"kind":"bash","text":"calc"}')[0] == 400
    assert post(b"{" + b" " * 20000 + b"}")[0] == 400
    assert call(server, "/api/jobs/abc/stop", method="POST", body=b"{}")[0] == 404


def test_a_reply_that_ignores_the_body_leaves_the_connection_usable(server):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    auth = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    try:
        # each of these is answered without the route reading the body; the next request on the same
        # connection must still be parsed as a request, not as the leftover body
        for headers, expected in (({"Content-Type": "application/json"}, 401), (auth, 404)):
            conn.request("POST", "/api/jobs/abc/stop", body=b'{"pad":"' + b"x" * 4000 + b'"}', headers=headers)
            r = conn.getresponse()
            r.read()
            assert r.status == expected
        conn.request("GET", "/api/overview", headers=auth)
        r = conn.getresponse()
        assert r.status == 200 and json.loads(r.read())["projects"] == ["proj"]
    finally:
        conn.close()


def test_job_lifecycle_over_http(root, tmp_path):
    jm = JobManager(root, tmp_path / "d2", spawn=lambda a, c, fh: (fh.write(b"line\n"), FakeProc())[1])
    app = App(root, tmp_path / "d2", TOKEN, jobs=jm)
    srv = make_server(app)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        status, _, body = call(app, "/api/jobs", method="POST", body=b'{"kind":"research","text":"q"}')
        job = json.loads(body)
        assert status == 201 and job["status"] == "running"
        assert call(app, "/api/jobs", method="POST", body=b'{"kind":"research","text":"q"}')[0] == 400
        _, _, body = call(app, f"/api/jobs/{job['id']}/log?offset=0")
        assert json.loads(body)["text"] == "line\n"
        assert json.loads(call(app, "/api/overview")[2])["job"]["id"] == job["id"]
    finally:
        srv.shutdown()
        srv.server_close()
    time.sleep(0)


def test_events_live_and_synthesized(root):
    ws = root / "workspace"
    d = make_run(root)
    # no events.jsonl: rebuilt from progress.md
    syn = runs.events(ws, "proj", "20260101-120000")
    assert syn["synthetic"] and [e["kind"] for e in syn["events"]] == ["phase", "agent_start", "agent_end"]
    # real stream, paged by index, bad lines skipped
    lines = [{"kind": "phase", "name": "plan"}, {"kind": "agent_start", "role": "architect"}]
    (d / "events.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
    first = runs.events(ws, "proj", "20260101-120000", 0)
    assert not first["synthetic"] and first["next"] == 2
    assert runs.events(ws, "proj", "20260101-120000", 1)["events"][0]["role"] == "architect"
    assert runs.events(ws, "proj", "20260101-120000", 2)["events"] == []
    assert runs.events(ws, "..", "20260101-120000") is None


def test_events_route(server):
    status, _, body = call(server, "/api/runs/proj/20260101-120000/events?since=0")
    assert status == 200 and "events" in json.loads(body)
    assert call(server, "/api/runs/proj/20260101-120000/events?since=x")[0] == 200
    assert call(server, "/api/runs/proj/20260101-120000/events", token=None)[0] == 401


# ------------------------------------------------------------------ monitor (queue, autopilot, live run)
OLLAMA_UP = lambda: {"up": True, "models": [{"name": "qwen2.5-coder", "vram_mb": 5500}]}  # noqa: E731
OLLAMA_DOWN = lambda: {"up": False, "models": []}  # noqa: E731


def queue_item(project: Path | str = "", **kw) -> dict:
    return {"id": 1, "state": "todo", "tier": 2, "project": str(project), "task": "do it", "check": "tests", **kw}


def write_queue(root: Path, *items: dict) -> None:
    (root / "queue.json").write_text(json.dumps(list(items)), encoding="utf-8")


def external_run(tmp_path: Path, events: list[dict], run="20260102-100000") -> Path:
    """A project outside workspace/ with one run, the way the autopilot leaves it."""
    project = tmp_path / "elsewhere" / "app"
    d = project / ".swarm" / "runs" / run
    d.mkdir(parents=True)
    (d / "progress.md").write_text("- 10:00:00 PHASE plan\n", encoding="utf-8")
    (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return project


def test_monitor_is_idle_and_still_loads_with_nothing_there(root):
    snap = monitor.snapshot(root, root / "workspace", OLLAMA_DOWN, now=1000.0)
    assert snap["state"] == "idle" and snap["live"] is None and snap["queue"] == [] and snap["status"] is None
    assert snap["ollama"] == {"up": False, "models": []} and snap["log"]["lines"] == []
    assert snap["counts"] == {"todo": 0, "doing": 0, "done": 0, "blocked": 0}
    # broken files are an empty view, never an error
    (root / "queue.json").write_text("{not json", encoding="utf-8")
    (root / "autopilot-state.json").write_text("[]", encoding="utf-8")
    assert monitor.snapshot(root, root / "workspace", OLLAMA_UP, now=1000.0)["state"] == "idle"


def test_monitor_shows_the_running_item_role_engine_and_gates(root, tmp_path):
    now = time.time()  # the run folder's file times are real, so the clock has to be too
    project = external_run(tmp_path, [
        {"t": now - 60, "kind": "phase", "name": "implement", "detail": ""},
        {"t": now - 50, "kind": "agent_start", "role": "architect", "label": "plan", "model": "sonnet"},
        {"t": now - 40, "kind": "agent_end", "role": "architect", "ok": True},
        {"t": now - 30, "kind": "gate", "label": "after W1", "state": "FAIL"},
        {"t": now - 20, "kind": "gate", "label": "after W2", "state": "PASS"},
        {"t": now - 10, "kind": "agent_start", "role": "developer", "label": "W3 edit", "model": "sonnet"},
        {"t": now - 5, "kind": "activity", "role": "developer", "what": "tool", "text": "edit_file ['a.py']"},
    ])  # fmt: skip
    write_queue(root, queue_item(project, id=17, state="doing", tier=1), queue_item(id=18, task="next one"))
    state = {"last_item": 13, "current": {"item": 17, "backend": "local", "why": "PC is idle", "started_at": now - 70}}
    (root / "autopilot-state.json").write_text(json.dumps(state), encoding="utf-8")
    snap = monitor.snapshot(root, root / "workspace", OLLAMA_UP, now=now)
    live = snap["live"]
    assert snap["state"] == "working" and live["item"]["id"] == 17 and live["engine"] == "local"
    assert live["project"] == "app" and live["run"]["phase"] == "implement"
    assert [a["role"] for a in live["run"]["active"]] == ["developer"]
    assert [(g["label"], g["ok"]) for g in live["run"]["gates"]] == [("after W1", False), ("after W2", True)]
    assert live["run"]["activity"].startswith("developer") and snap["next"]["id"] == 18
    assert snap["counts"]["doing"] == 1 and snap["recent"][0]["item"] == 17
    # the same item long after its run stopped writing is "quiet", not "working"
    assert monitor.snapshot(root, root / "workspace", OLLAMA_UP, now=now + monitor.STALL_S + 60)["state"] == "quiet"


def test_monitor_waiting_state_and_panel_runs(root):
    write_queue(root, queue_item(id=10))
    (root / "autopilot-state.json").write_text(
        json.dumps({"claude_paused_until": 2000.0, "reason": "plan limit"}), encoding="utf-8"
    )
    snap = monitor.snapshot(root, root / "workspace", OLLAMA_UP, now=1000.0)
    assert snap["state"] == "waiting" and snap["autopilot"]["reason"] == "plan limit"
    assert monitor.snapshot(root, root / "workspace", OLLAMA_UP, now=3000.0)["state"] == "idle"  # reset passed
    # a run started from the panel (under workspace/, no queue item) also counts as live work
    make_run(root, summary=False)
    snap = monitor.snapshot(root, root / "workspace", OLLAMA_UP)
    assert snap["state"] == "working" and snap["live"]["item"] is None and snap["live"]["project"] == "proj"


def test_monitor_log_status_and_ollama_probe(root):
    (root / "workspace" / "autopilot-2026-10-01.log").write_text(
        "item 13: ran on local -> \x1b[31mneeds_attention\x1b[0m\n\nexit=1\n", encoding="utf-8"
    )
    assert monitor.log(root)["lines"] == ["item 13: ran on local -> needs_attention", "exit=1"]
    (root / "docs").mkdir()
    snap = {"generated_at": 5.0, "load": {"mode": "full", "reasons": []}, "secret": "x",
            "machine": {"cpu": {"percent": 6.2}, "gpus": [{"name": "g", "temp_c": 56.0, "serial": "x"}]},
            "claude": {"by_day": [{"day": "2026-10-01", "output": 9, "cache_read": 1}]}}  # fmt: skip
    (root / "docs" / "status.json").write_text(json.dumps(snap), encoding="utf-8")
    st = monitor.status(root)
    assert st["load"]["mode"] == "full" and st["cpu_percent"] == 6.2 and st["gpus"][0]["temp_c"] == 56.0
    assert "secret" not in st and "serial" not in st["gpus"][0] and st["claude"]["by_day"][0]["output"] == 9

    calls = []

    def fetch() -> bytes:
        calls.append(1)
        return json.dumps({"models": [{"name": "m", "size_vram": 3 * 2**20}]}).encode()

    probe = monitor.OllamaProbe(fetch)
    assert probe(now=100.0) == {"up": True, "models": [{"name": "m", "vram_mb": 3}]}
    probe(now=102.0)
    assert len(calls) == 1  # cached

    def refused() -> bytes:
        raise ConnectionRefusedError

    assert monitor.OllamaProbe(refused)(now=1.0) == {"up": False, "models": []}


def test_monitor_routes(root, tmp_path):
    project = external_run(tmp_path, [{"t": 1.0, "kind": "phase", "name": "plan"}])
    write_queue(root, queue_item(project, id=17, state="done"), queue_item("C:/nowhere/at/all", id=3))
    app = App(root, tmp_path / "d3", TOKEN, ollama=OLLAMA_DOWN)
    srv = make_server(app)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        status, _, body = call(app, "/api/monitor")
        data = json.loads(body)
        assert status == 200 and data["state"] == "idle" and data["ollama"]["up"] is False
        assert "elsewhere" not in body.decode()  # project folders are shown by name, never as a path
        assert call(app, "/api/monitor", token=None)[0] == 401
        status, _, body = call(app, "/api/monitor/events?item=17&since=0")
        assert status == 200 and json.loads(body)["run"] == "20260102-100000"
        assert call(app, "/api/monitor/events?item=17&run=20260102-100000")[0] == 200
        assert call(app, "/api/monitor/events?item=17&run=..%2F..%2Fx")[0] == 404
        assert call(app, "/api/monitor/events?item=17&run=20990101-000000")[0] == 404
        assert call(app, "/api/monitor/events?item=3")[0] == 404  # project folder does not exist
        assert call(app, "/api/monitor/events?item=99")[0] == 404
        assert call(app, "/api/monitor/events?item=..%2Fx")[0] == 400
        assert call(app, "/api/monitor/events?item=17", token=None)[0] == 401
    finally:
        srv.shutdown()
        srv.server_close()
