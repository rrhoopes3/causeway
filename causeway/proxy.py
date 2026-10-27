"""OpenAI-compatible proxy that records a tagged ledger (SPEC §9, slice 5).

An agent points its base_url at this process and changes no other code. Each
POST /v1/chat/completions (or /chat/completions) is forwarded to the upstream
chat API. The request messages plus the assistant reply are tagged with
transcript.to_ledger, so the tagger's rules apply unchanged: a tool result
whose whole text is `error: no such file: <path>` is not a source, and a
retry of that path stays UNSOURCED.

One proxy process records one session. The ledger is rebuilt from the messages
of the latest completion, which is the whole conversation for a normal agent.
The header's source is "proxy".

Invoke:

    python -m causeway proxy --upstream http://127.0.0.1:8080/v1 -o run.jsonl

Then set the agent's base_url to http://127.0.0.1:8765/v1. The default bind
is loopback. The upstream connection does not follow redirects and ignores
HTTP_PROXY, so an agent key is not sent anywhere except that upstream.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from causeway.client import load_api_key, message_of
from causeway.ledger import write as write_ledger
from causeway.transcript import to_ledger

_MAX_BODY = 32 * 1024 * 1024
_CHAT_PATHS = frozenset({"/v1/chat/completions", "/chat/completions"})
_MIN_SECRET = 8


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that keeps the Proxy which records the session."""

    proxy: Proxy

    def __init__(self, address: tuple[str, int], proxy: Proxy) -> None:
        super().__init__(address, _Handler)
        self.proxy = proxy


class Proxy:
    """Forwards chat completions to one upstream and writes a tagged ledger.

    `upstream` is a base URL such as https://api.meta.ai/v1, the same shape
    ChatClient takes. `transport` exists so tests can substitute httpx.MockTransport.
    An api_key is sent only when the agent did not send Authorization. For
    api.meta.ai it is loaded from the environment or ~/.config/causeway/.env;
    it is never printed.
    """

    def __init__(
        self,
        upstream: str,
        ledger_path: str | Path,
        *,
        run_id: str = "r-1",
        model: str = "unknown",
        watch: Iterable[str] = (),
        api_key: str | None = None,
        timeout: float = 120,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not upstream or not str(upstream).strip():
            raise ValueError("upstream base URL is required")
        self.upstream = str(upstream).rstrip("/")
        self.ledger_path = Path(ledger_path)
        self.run_id = run_id
        self.model = model
        self.watch = list(watch)
        self.api_key = api_key
        if self.api_key is None and (urlsplit(self.upstream).hostname or "") == "api.meta.ai":
            self.api_key = load_api_key()
        self._url = f"{self.upstream}/chat/completions"
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False, trust_env=False)
        self.records: list[dict] | None = None

    def complete(self, body: bytes, headers: Mapping[str, str]) -> tuple[int, list[tuple[str, str]], bytes]:
        """Forward one chat-completions request and, on success, rewrite the ledger.

        Returns (status, headers, body). Upstream failures are forwarded after
        any known key is removed from the body. The exception text of a failed
        connection is dropped, because it can quote the request.
        """
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return _error(400, "expected a JSON object")
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            return _error(400, "expected a JSON object with a messages list")
        if payload.get("stream") is True:
            return _error(400, "proxy mode records complete responses; omit stream or set it to false")

        authorization = _header(headers, "Authorization")
        forward: dict[str, str] = {}
        content_type = _safe_header(_header(headers, "Content-Type")) or "application/json"
        forward["Content-Type"] = content_type
        secrets: list[str] = []
        if authorization and _safe_header(authorization):
            forward["Authorization"] = authorization
            secrets.extend(_secret_texts(authorization))
        elif self.api_key:
            forward["Authorization"] = f"Bearer {self.api_key}"
            secrets.extend(_secret_texts(self.api_key))

        try:
            resp = self._http.post(self._url, content=body, headers=forward)
        except (httpx.HTTPError, OSError, ValueError):
            return _error(502, "upstream request failed")

        raw = resp.content
        response_headers = _content_type(resp.headers.get("content-type"))
        if not 200 <= resp.status_code < 300:
            return resp.status_code, response_headers, _redact(raw, secrets)

        self._record(payload, raw)
        return resp.status_code, response_headers, raw

    def serve(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        """Listen until interrupted. Prints the bound address. The default bind is loopback."""
        with self.listening(host, port) as (bound_host, bound_port):
            sys.stdout.write(f"proxy listening on http://{bound_host}:{bound_port}/v1\n")
            sys.stdout.write(f"ledger {self.ledger_path}\n")
            sys.stdout.flush()
            threading.Event().wait()

    @contextmanager
    def listening(self, host: str = "127.0.0.1", port: int = 0) -> Iterator[tuple[str, int]]:
        """Serve on a background thread and yield (host, port). Port 0 picks a free port."""
        server = _Server((host, port), self)
        thread = threading.Thread(target=server.serve_forever, name="causeway-proxy", daemon=True)
        thread.start()
        try:
            bound_host, bound_port = server.server_address[:2]
            yield str(bound_host), int(bound_port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.close()

    def close(self) -> None:
        """Close the upstream client."""
        self._http.close()

    def _record(self, payload: dict[str, Any], raw: bytes) -> None:
        """Tag request messages plus the assistant reply.

        A failure to tag or write is ignored so the agent still receives the upstream reply.
        The exception text can quote the conversation, so it is not logged.
        """
        try:
            parsed = json.loads(raw)
            message = message_of(parsed)
            if not isinstance(message, dict):
                return
            messages = list(payload["messages"])
            messages.append(message)
            named = payload.get("model")
            model = named if isinstance(named, str) and named else self.model
            records = to_ledger(messages, run_id=self.run_id, model=model, watch=self.watch)
            records[0]["source"] = "proxy"
            with self._lock:
                write_ledger(self.ledger_path, records)
                self.records = records
        except OSError as exc:
            sys.stderr.write(f"causeway: could not write {self.ledger_path} ({exc.strerror})\n")
            return
        except Exception:
            return


class _Handler(BaseHTTPRequestHandler):
    """One chat-completions request. Request lines are not logged."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        body = _read_body(self)
        if body is None:
            return
        if not _is_chat_path(self.path):
            _send(self, *_error(404, "not found"))
            return
        try:
            status, headers, payload = self.server.proxy.complete(body, self.headers)  # type: ignore[attr-defined]
        except Exception as exc:
            _send(self, *_error(500, f"proxy failed ({type(exc).__name__})"))
            return
        _send(self, status, headers, payload)

    def do_GET(self) -> None:
        _send(self, *_error(404, "not found"))

    def log_message(self, fmt: str, *args: Any) -> None:
        """Drop the default access log. It is not needed, and request lines are not a place for secrets."""
        return


def _is_chat_path(path: str) -> bool:
    route = urlsplit(path).path.rstrip("/") or "/"
    return route in _CHAT_PATHS


def _read_body(handler: BaseHTTPRequestHandler) -> bytes | None:
    length = handler.headers.get("Content-Length")
    if length is None:
        _send(handler, *_error(411, "Content-Length is required"))
        return None
    try:
        size = int(length)
    except ValueError:
        _send(handler, *_error(400, "Content-Length must be an integer"))
        return None
    if size < 0 or size > _MAX_BODY:
        _send(handler, *_error(413, "request body is too large"))
        return None
    body = bytearray()
    while len(body) < size:
        chunk = handler.rfile.read(size - len(body))
        if not chunk:
            break
        body.extend(chunk)
    if len(body) != size:
        _send(handler, *_error(400, "request body ended early"))
        return None
    return bytes(body)


def _send(handler: BaseHTTPRequestHandler, status: int, headers: list[tuple[str, str]], payload: bytes) -> None:
    handler.send_response(status)
    for key, value in headers:
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(payload)


def _error(status: int, message: str) -> tuple[int, list[tuple[str, str]], bytes]:
    body = json.dumps({"error": {"message": message, "type": "invalid_request_error"}}).encode("utf-8")
    return status, [("Content-Type", "application/json")], body


def _content_type(value: str | None) -> list[tuple[str, str]]:
    safe = _safe_header(value)
    if safe is None:
        return [("Content-Type", "application/json")]
    return [("Content-Type", safe)]


def _header(headers: Mapping[str, str], name: str) -> str | None:
    found = headers.get(name)
    if found:
        return found
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered and value:
            return value
    return None


def _safe_header(value: str | None) -> str | None:
    """Reject CR or LF so a header cannot inject another header."""
    if value is None:
        return None
    if "\r" in value or "\n" in value:
        return None
    return value


def _secret_texts(value: str) -> list[str]:
    text = value.strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    if len(text) < _MIN_SECRET or "\r" in text or "\n" in text:
        return []
    return [text]


def _redact(payload: bytes, secrets: list[str]) -> bytes:
    for secret in secrets:
        encoded = secret.encode("utf-8")
        if len(encoded) >= _MIN_SECRET:
            payload = payload.replace(encoded, b"[redacted]")
    return payload
