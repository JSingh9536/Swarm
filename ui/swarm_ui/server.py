"""The local HTTP server: static UI + a small token-protected JSON API. Loopback only."""

from __future__ import annotations

import argparse
import json
import re
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from swarm_ui import __version__, monitor, runs, security
from swarm_ui.jobs import JobError, JobManager

UI_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = UI_DIR / "web"
DEFAULT_ROOT = UI_DIR.parent  # the swarm checkout
MAX_BODY = 16 * 1024
MAX_DRAIN = 1024 * 1024

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/viz.js": ("viz.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
_RUN_FILE = re.compile(r"^/api/runs/([^/]+)/([^/]+)/file/([^/]+)$")
_RUN_EVENTS = re.compile(r"^/api/runs/([^/]+)/([^/]+)/events$")
_RUN = re.compile(r"^/api/runs/([^/]+)/([^/]+)$")
_JOB_LOG = re.compile(r"^/api/jobs/([^/]+)/log$")
_JOB_STOP = re.compile(r"^/api/jobs/([^/]+)/stop$")


class App:
    def __init__(
        self, root: Path, data_dir: Path, token: str, jobs: JobManager | None = None,
        ollama: Callable[[], dict[str, Any]] | None = None,
    ) -> None:  # fmt: skip
        self.root = root.resolve()
        self.workspace = self.root / "workspace"
        self.token = token
        self.port = 0
        self.jobs = jobs or JobManager(self.root, data_dir)
        self.ollama = ollama or monitor.OllamaProbe()


class Handler(BaseHTTPRequestHandler):
    server_version = "swarm-ui"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet; no request data in logs
        pass

    # ---- plumbing

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in security.SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _gate(self, api: bool) -> bool:
        """Host/Origin checks for everything; bearer token for the API. Returns False after replying."""
        if not security.host_ok(self.headers.get("Host"), self.app.port):
            self._error(403, "bad host")
            return False
        if not security.origin_ok(self.headers.get("Origin"), self.app.port):
            self._error(403, "bad origin")
            return False
        if api and not security.token_ok(self.headers.get("Authorization"), self.app.token):
            self._error(401, "missing or wrong token")
            return False
        return True

    def _body(self) -> dict[str, Any] | None:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if ctype != "application/json" or not 0 <= length <= MAX_BODY:
            # Read a rejected body off the socket (bounded) so the client gets the reply instead of a reset;
            # anything larger is not worth reading: answer and drop the connection.
            if 0 < length <= MAX_DRAIN:
                self.rfile.read(length)
            else:
                self.close_connection = True
            self._error(400, "expected a small application/json body")
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._error(400, "invalid JSON")
            return None
        if not isinstance(data, dict):
            self._error(400, "expected a JSON object")
            return None
        return data

    # ---- routes

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        path = url.path
        if path in STATIC:
            if not self._gate(api=False):
                return
            name, ctype = STATIC[path]
            try:
                self._send(200, (WEB_DIR / name).read_bytes(), ctype)
            except OSError:
                self._error(404, "not found")
            return
        if not path.startswith("/api/"):
            return self._error(404, "not found") if self._gate(api=False) else None
        if not self._gate(api=True):
            return
        app = self.app
        if path == "/api/overview":
            data = runs.overview(app.workspace)
            job = app.jobs.current()
            data.update(job=job.public() if job else None, jobs=app.jobs.history()[:10], version=__version__)
            return self._json(200, data)
        if path == "/api/monitor":
            return self._json(200, monitor.snapshot(app.root, app.workspace, app.ollama))
        if path == "/api/monitor/events":
            query = parse_qs(url.query)
            try:
                item, since = int(query.get("item", [""])[0]), int(query.get("since", ["0"])[0])
            except ValueError:
                return self._error(400, "item and since must be numbers")
            data = monitor.item_events(app.root, item, query.get("run", [None])[0], since)
            return self._json(200, data) if data else self._error(404, "not found")
        if m := _RUN_FILE.match(path):
            text = runs.artifact(app.workspace, *m.groups())
            return self._json(200, {"text": text}) if text is not None else self._error(404, "not found")
        if m := _RUN_EVENTS.match(path):
            try:
                since = int(parse_qs(url.query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            data = runs.events(app.workspace, *m.groups(), since)
            return self._json(200, data) if data else self._error(404, "not found")
        if m := _RUN.match(path):
            info = runs.detail(app.workspace, *m.groups())
            return self._json(200, info) if info else self._error(404, "not found")
        if m := _JOB_LOG.match(path):
            job = app.jobs.get(m.group(1))
            if not job:
                return self._error(404, "not found")
            try:
                offset = int(parse_qs(url.query).get("offset", ["0"])[0])
            except ValueError:
                offset = 0
            return self._json(200, {**app.jobs.tail(job, offset), "job": job.public()})
        self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if not self._gate(api=True):
            return
        app = self.app
        if path == "/api/jobs":
            spec = self._body()
            if spec is None:
                return
            try:
                job = app.jobs.start(spec)
            except JobError as exc:
                return self._error(400, str(exc))
            except OSError as exc:
                return self._error(500, f"could not start swarm: {exc.strerror or exc}")
            return self._json(201, job.public())
        if m := _JOB_STOP.match(path):
            job = app.jobs.stop(m.group(1))
            return self._json(200, job.public()) if job else self._error(404, "not found")
        self._error(404, "not found")


def make_server(app: App, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((security.require_loopback(host), port), Handler)
    srv.daemon_threads = True
    srv.app = app  # type: ignore[attr-defined]
    app.port = srv.server_address[1]
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="swarm-ui", description="Local control panel for the swarm.")
    ap.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (0 = pick a free one)")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="swarm checkout (default: the parent folder)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = ap.parse_args(argv)

    root = args.root.resolve()
    data_dir = UI_DIR / ".data"
    app = App(root, data_dir, security.new_token())
    try:
        srv = make_server(app, "127.0.0.1", args.port)
    except OSError as exc:
        print(f"Cannot listen on 127.0.0.1:{args.port}: {exc.strerror or exc}")
        return 2
    url = f"http://127.0.0.1:{app.port}/#token={app.token}"
    print(f"swarm-ui {__version__}  (local only)\n  workspace: {app.workspace}\n  open: {url}", flush=True)
    print("  Ctrl+C to stop", flush=True)
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        app.jobs.shutdown()
    return 0
