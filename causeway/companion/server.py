"""HTTP front of live mode (SPEC §11.3): the scene's static files, Server-Sent Events, and the run's controls.

Stdlib only. The server binds 127.0.0.1, so nothing listens on the network. One session at a time; its events are
kept so a new stream replays them before the live ones.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit

from causeway.companion.session import VERDICTS, Emit, Session

HOST = "127.0.0.1"
DEFAULT_PORT = 8770
PING_SECONDS = 15.0
MAX_BODY = 1_000_000
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".glb": "model/gltf-binary",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}
OCTET_STREAM = "application/octet-stream"

SessionFactory = Callable[[Emit, str | None], Session]


class Hub:
    """The current session's events in order, and a queue per open stream.

    reset() starts a new generation: the log is cleared, and events from an older session's threads are dropped.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._streams: list[queue.Queue] = []
        self._generation = 0

    def emitter(self) -> Emit:
        """An emit callback bound to the current generation."""
        with self._lock:
            generation = self._generation
        return lambda event: self.publish(event, generation)

    def publish(self, event: dict[str, Any], generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._events.append(event)
            for stream in self._streams:
                stream.put(event)

    def reset(self) -> Emit:
        """Clear the log and return an emitter for the new generation. Open streams stay subscribed."""
        with self._lock:
            self._generation += 1
            self._events.clear()
        return self.emitter()

    def subscribe(self) -> tuple[list[dict[str, Any]], queue.Queue]:
        """Every event so far, and a queue that receives each later one. Nothing is missed or repeated."""
        stream: queue.Queue = queue.Queue()
        with self._lock:
            self._streams.append(stream)
            return list(self._events), stream

    def unsubscribe(self, stream: queue.Queue) -> None:
        with self._lock:
            if stream in self._streams:
                self._streams.remove(stream)

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def open_streams(self) -> int:
        with self._lock:
            return len(self._streams)

    def close(self) -> None:
        """End every open stream."""
        with self._lock:
            for stream in self._streams:
                stream.put(None)


class LoopbackServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with daemon request threads. On Windows SO_REUSEADDR would let a second server bind a
    port already in use, so it is left off there and a taken port fails loudly."""

    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


class Companion:
    """One session at a time behind a ThreadingHTTPServer on 127.0.0.1.

    factory(emit, task) makes a session; task None means the scenario's own task. retire(session), when given, is
    called for each session that is replaced or shut down, after it has been stopped (to remove its sandbox).
    Port 0 picks a free port.
    """

    def __init__(
        self,
        factory: SessionFactory,
        static_dir: str | Path = STATIC_DIR,
        port: int = DEFAULT_PORT,
        retire: Callable[[Session], None] | None = None,
        ping_seconds: float = PING_SECONDS,
    ) -> None:
        self.factory = factory
        self.static_dir = Path(static_dir).resolve()
        self.retire = retire
        self.ping_seconds = ping_seconds
        self.hub = Hub()
        self._lock = threading.Lock()
        self.httpd = LoopbackServer((HOST, port), Handler)
        self.httpd.app = self  # type: ignore[attr-defined]
        try:
            self.session = factory(self.hub.emitter(), None)
        except BaseException:
            self.httpd.server_close()
            raise

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/"

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.httpd.serve_forever(poll_interval)

    def shutdown(self) -> None:
        """Stop serve_forever running in another thread, then close."""
        self.httpd.shutdown()
        self.close()

    def close(self) -> None:
        """Stop the session, end open streams and release the socket."""
        with self._lock:
            session = self.session
        session.stop()
        if self.retire is not None:
            self.retire(session)
        self.hub.close()
        self.httpd.server_close()

    def start(self, task: str | None) -> tuple[int, dict[str, Any]]:
        """POST /start. A finished session, or a different task, gets a fresh session first."""
        with self._lock:
            if self.session.running:
                return HTTPStatus.CONFLICT, {"error": "a run is already in progress"}
            if self.session.started or (task is not None and task != self.session.task):
                self._replace(task)
            self.session.start()
            return HTTPStatus.ACCEPTED, {"task": self.session.task}

    def reset(self) -> tuple[int, dict[str, Any]]:
        """POST /reset: stop any run and make a fresh session, whose hello reaches open streams."""
        with self._lock:
            self._replace(None)
        return HTTPStatus.OK, {"ok": True}

    def decide(self, gate_id: str, verdict: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            session = self.session
        if not session.decide(gate_id, verdict):
            return HTTPStatus.NOT_FOUND, {"error": f"no open gate {gate_id}"}
        return HTTPStatus.OK, {"ok": True}

    def why(self, question: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            session = self.session
        session.why(question)
        return HTTPStatus.ACCEPTED, {"ok": True}

    def ledger(self) -> list[dict[str, Any]]:
        with self._lock:
            session = self.session
        return session.ledger()

    def _replace(self, task: str | None) -> None:
        old = self.session
        old.stop()
        self.session = self.factory(self.hub.reset(), task)
        if self.retire is not None:
            self.retire(old)


def resolve_static(root: Path, raw: str) -> Path | None:
    """The file under root named by the URL path raw (after /static/), or None.

    raw is percent-decoded first. Backslashes, NUL, colons, absolute paths and `..` segments are refused, and
    the resolved file must still lie under root, so symlinks cannot leave it either.
    """
    rel = unquote(raw)
    if not rel or any(bad in rel for bad in ("\\", "\x00", ":")):
        return None
    if rel.startswith("/") or PureWindowsPath(rel).drive or ".." in rel.split("/"):
        return None
    root = root.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


def _json_body(raw: bytes) -> dict[str, Any] | None:
    """A request body as a JSON object. An empty body is {}. None for anything that is not a JSON object."""
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _text(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    return value if isinstance(value, str) and value.strip() else None


class Handler(BaseHTTPRequestHandler):
    """Routes for §11.3. Every response carries Cache-Control: no-store."""

    server_version = "causeway-companion"

    @property
    def app(self) -> Companion:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._file(resolve_static(self.app.static_dir, "index.html"))
        elif path.startswith("/static/"):
            self._file(resolve_static(self.app.static_dir, path[len("/static/"):]))
        elif path == "/events":
            self._events()
        elif path == "/ledger":
            self._json(HTTPStatus.OK, self.app.ledger())
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_HEAD(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._file(resolve_static(self.app.static_dir, "index.html"), head=True)
        elif path.startswith("/static/"):
            self._file(resolve_static(self.app.static_dir, path[len("/static/"):]), head=True)
        elif path == "/ledger":
            self._json(HTTPStatus.OK, self.app.ledger(), head=True)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"}, head=True)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in ("/start", "/decide", "/why", "/reset"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        body = self._body()
        if body is None:
            return
        try:
            status, payload = self._route(path, body)
        except Exception as err:  # e.g. the sandbox could not be copied; the server keeps serving
            status, payload = HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(err).__name__}: {err}"}
        self._json(status, payload)

    def _route(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if path == "/start":
            task = body.get("task")
            if task is not None and _text(body, "task") is None:
                return HTTPStatus.BAD_REQUEST, {"error": "task must be a non-empty string"}
            return self.app.start(task)
        if path == "/decide":
            gate_id, verdict = _text(body, "gate_id"), body.get("verdict")
            if gate_id is None or verdict not in VERDICTS:
                return HTTPStatus.BAD_REQUEST, {"error": "expected {\"gate_id\": string, \"verdict\": \"approve\" or \"deny\"}"}
            return self.app.decide(gate_id, verdict)
        if path == "/why":
            question = _text(body, "question")
            if question is None:
                return HTTPStatus.BAD_REQUEST, {"error": "expected {\"question\": string}"}
            return self.app.why(question)
        return self.app.reset()

    def _body(self) -> dict[str, Any] | None:
        """The JSON object sent, or None after answering 400 or 413."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad Content-Length"})
            return None
        if length > MAX_BODY:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
            return None
        body = _json_body(self.rfile.read(length))
        if body is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "body must be a JSON object"})
        return body

    def _json(self, status: int, payload: Any, head: bool = False) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json", head)

    def _file(self, target: Path | None, head: bool = False) -> None:
        if target is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"}, head)
            return
        self._send(HTTPStatus.OK, target.read_bytes(), CONTENT_TYPES.get(target.suffix.lower(), OCTET_STREAM), head)

    def _send(self, status: int, data: bytes, content_type: str, head: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _events(self) -> None:
        """Server-Sent Events: every event so far, then live ones, and a `: ping` comment every ping_seconds.

        A stream whose client has gone is dropped on the next write; the session never waits on it.
        """
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        backlog, stream = self.app.hub.subscribe()
        try:
            for event in backlog:
                self._frame(event)
            next_ping = time.monotonic() + self.app.ping_seconds
            while True:
                wait = next_ping - time.monotonic()
                if wait <= 0:
                    self.wfile.write(b": ping\n\n")
                    next_ping = time.monotonic() + self.app.ping_seconds
                    continue
                try:
                    event = stream.get(timeout=wait)
                except queue.Empty:
                    continue
                if event is None:
                    return
                self._frame(event)
        except (OSError, ValueError):  # BrokenPipe, ConnectionReset/Aborted, or a closed socket
            return
        finally:
            self.app.hub.unsubscribe(stream)

    def _frame(self, event: dict[str, Any]) -> None:
        self.wfile.write(b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n")
