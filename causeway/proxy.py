"""OpenAI-compatible proxy that records a tagged ledger (SPEC §9, slice 5).

An agent points its base_url at this process and changes no other code. Each
POST /v1/chat/completions (or /chat/completions) is forwarded to the upstream
chat API, including a request with stream set to true. The upstream event
stream is written back as it arrives. The request messages plus the assistant
reply are tagged with transcript.to_ledger, so the tagger's rules apply
unchanged: a tool result whose whole text is `error: no such file: <path>`
is not a source, and a retry of that path stays UNSOURCED. A streamed reply
is assembled from the completed event stream and tagged the same way.

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
import socket
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
_MAX_TOOL_CALLS = 32
# Hop-by-hop headers belong to this connection (RFC 9110). Host and
# Content-Length are regenerated for the upstream request.
_HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
})
# The body returned to the agent is decoded, and only Content-Type is sent
# back, so the client's Accept-Encoding must not be copied upstream.
_NOT_FORWARDED = _HOP_BY_HOP | {"accept-encoding"}
_CREDENTIAL_HEADERS = frozenset({"authorization", "api-key", "x-api-key"})


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that keeps the Proxy which records the session."""

    proxy: Proxy

    def __init__(self, address: tuple[str, int], proxy: Proxy) -> None:
        super().__init__(address, _Handler)
        self.proxy = proxy


class _LiveStream:
    """An upstream response whose body is still open. The handler must close it."""

    def __init__(
        self,
        proxy: Proxy,
        payload: dict[str, Any],
        response: httpx.Response,
        secrets: list[str],
    ) -> None:
        self.proxy = proxy
        self.payload = payload
        self.response = response
        self.secrets = secrets

    def close(self) -> None:
        """Release the upstream stream. Close errors are dropped; they can quote the request."""
        try:
            self.response.close()
        except Exception:
            return


class Proxy:
    """Forwards chat completions to one upstream and writes a tagged ledger.

    `upstream` is a base URL such as https://api.meta.ai/v1, the same shape
    ChatClient takes. `transport` exists so tests can substitute httpx.MockTransport.
    An api_key is sent only when the agent did not send Authorization. For
    api.meta.ai it is loaded from the environment or ~/.config/causeway/.env;
    it is never printed.

    Client headers are forwarded, including organization, project, and provider
    API-key headers. Hop-by-hop headers are not. Header values are not logged.
    A request with stream true is written back as it arrives. The ledger is
    recorded from the assistant message assembled when that stream completes.
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

    def complete(
        self, body: bytes, headers: Mapping[str, str]
    ) -> tuple[int, list[tuple[str, str]], bytes] | _LiveStream:
        """Forward one chat-completions request and, on success, rewrite the ledger.

        Returns (status, headers, body), or a live stream the handler must close.
        A request with stream true is the live stream: its bytes are written back
        as they arrive, and the ledger is recorded from the assembled assistant
        message. Upstream failures are forwarded after any known key is removed
        from the body. The exception text of a failed connection is dropped,
        because it can quote the request.
        """
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return _error(400, "expected a JSON object")
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            return _error(400, "expected a JSON object with a messages list")

        forward, secrets = _outbound_headers(headers, self.api_key)
        if payload.get("stream") is True:
            return self._open_stream(payload, body, forward, secrets)

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

    def _open_stream(
        self,
        payload: dict[str, Any],
        body: bytes,
        forward: dict[str, str],
        secrets: list[str],
    ) -> tuple[int, list[tuple[str, str]], bytes] | _LiveStream:
        """Start the upstream request without reading its body."""
        try:
            request = self._http.build_request("POST", self._url, content=body, headers=forward)
            response = self._http.send(request, stream=True)
        except (httpx.HTTPError, OSError, ValueError):
            return _error(502, "upstream request failed")
        return _LiveStream(self, payload, response, secrets)

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
        """Tag a non-stream completion. A body that is not a chat completion is ignored."""
        try:
            message = message_of(json.loads(raw))
        except Exception:
            return
        if not isinstance(message, dict):
            return
        self._record_message(payload, message)

    def _record_message(self, payload: dict[str, Any], message: dict[str, Any]) -> None:
        """Tag request messages plus the assistant reply.

        A failure to tag or write is ignored so the agent still receives the upstream reply.
        The exception text can quote the conversation, so it is not logged.
        """
        try:
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
    _response_started = False

    def setup(self) -> None:
        """Disable Nagle so an event stream is not held for a full packet."""
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            return

    def do_POST(self) -> None:
        body = _read_body(self)
        if body is None:
            return
        if not _is_chat_path(self.path):
            _send(self, *_error(404, "not found"))
            return
        try:
            result = self.server.proxy.complete(body, self.headers)  # type: ignore[attr-defined]
        except Exception as exc:
            _send(self, *_error(500, f"proxy failed ({type(exc).__name__})"))
            return
        if isinstance(result, _LiveStream):
            try:
                _send_stream(self, result)
            except Exception as exc:
                # The message can quote the request or a header. Do not log it.
                if not getattr(self, "_response_started", False):
                    _send(self, *_error(500, f"proxy failed ({type(exc).__name__})"))
            finally:
                result.close()
            return
        status, headers, payload = result
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
    handler._response_started = True
    handler.send_response(status)
    for key, value in headers:
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(payload)


def _send_stream(handler: BaseHTTPRequestHandler, live: _LiveStream) -> None:
    """Write the upstream body through as it arrives, then record the assembled reply.

    The ledger is written before the chunked terminator, so a client that has
    finished reading the stream can already see the tagged ledger. A read error
    is not logged: the exception can quote the request.
    """
    response = live.response
    status = response.status_code
    headers = _content_type(response.headers.get("content-type"))
    if not 200 <= status < 300:
        try:
            raw = response.read()
        except (httpx.HTTPError, OSError, ValueError):
            _send(handler, *_error(502, "upstream request failed"))
            return
        _send(handler, status, headers, _redact(raw, live.secrets))
        return

    try:
        _begin_chunked(handler, status, headers)
    except OSError:
        return

    collected = bytearray()
    too_big = False
    failed = False
    client_open = True
    try:
        for chunk in response.iter_bytes():
            if not chunk:
                continue
            if not too_big:
                if len(collected) + len(chunk) > _MAX_BODY:
                    too_big = True
                    collected.clear()
                else:
                    collected.extend(chunk)
            if not client_open:
                continue
            try:
                _write_chunk(handler, chunk)
            except OSError:
                client_open = False
    except (httpx.HTTPError, OSError, ValueError):
        failed = True

    if not failed and not too_big:
        message = _assistant_message(bytes(collected))
        if isinstance(message, dict):
            live.proxy._record_message(live.payload, message)
    if client_open:
        try:
            _end_chunked(handler)
        except OSError:
            return


def _begin_chunked(handler: BaseHTTPRequestHandler, status: int, headers: list[tuple[str, str]]) -> None:
    handler._response_started = True
    handler.close_connection = True
    handler.send_response(status)
    for key, value in headers:
        handler.send_header(key, value)
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.flush()


def _write_chunk(handler: BaseHTTPRequestHandler, payload: bytes) -> None:
    handler.wfile.write(f"{len(payload):X}\r\n".encode("ascii"))
    handler.wfile.write(payload)
    handler.wfile.write(b"\r\n")
    handler.wfile.flush()


def _end_chunked(handler: BaseHTTPRequestHandler) -> None:
    handler.wfile.write(b"0\r\n\r\n")
    handler.wfile.flush()


def _error(status: int, message: str) -> tuple[int, list[tuple[str, str]], bytes]:
    body = json.dumps({"error": {"message": message, "type": "invalid_request_error"}}).encode("utf-8")
    return status, [("Content-Type", "application/json")], body


def _content_type(value: str | None) -> list[tuple[str, str]]:
    safe = _safe_header(value)
    if safe is None:
        return [("Content-Type", "application/json")]
    return [("Content-Type", safe)]


def _outbound_headers(headers: Mapping[str, str], api_key: str | None) -> tuple[dict[str, str], list[str]]:
    """Headers for the upstream request, and secret texts to strip from an error body.

    Organization, project, API-key, and similar provider headers are forwarded.
    Hop-by-hop headers are not, including any header named by Connection.
    Header values are not logged.
    """
    blocked = set(_NOT_FORWARDED)
    connection = _header(headers, "Connection")
    if connection and _safe_header(connection):
        for token in connection.split(","):
            name = token.strip().lower()
            if name:
                blocked.add(name)

    forward: dict[str, str] = {}
    secrets: list[str] = []
    for key, value in _header_pairs(headers):
        if key.lower() in blocked or not isinstance(value, str):
            continue
        safe = _safe_header(value)
        if safe is None or not _ascii_header(safe):
            continue
        forward[key] = safe
        if _is_credential(key):
            secrets.extend(_secret_texts(safe))

    if not any(key.lower() == "content-type" for key in forward):
        forward["Content-Type"] = "application/json"
    if not any(key.lower() == "authorization" for key in forward) and api_key:
        forward["Authorization"] = f"Bearer {api_key}"
        secrets.extend(_secret_texts(api_key))
    return forward, secrets


def _header_pairs(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    """(name, value) pairs. HTTPMessage.items() is the multi-header list."""
    items = getattr(headers, "items", None)
    if items is None:
        return []
    return [(str(key), value) for key, value in items()]


def _header(headers: Mapping[str, str], name: str) -> str | None:
    found = headers.get(name)
    if found:
        return found
    lowered = name.lower()
    for key, value in _header_pairs(headers):
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


def _ascii_header(value: str) -> bool:
    """Reject bytes httpx will not send, so one bad header does not fail the request."""
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return all(char == "\t" or ord(char) >= 32 for char in value)


def _is_credential(name: str) -> bool:
    """Authorization and provider API-key headers. The value is a secret and is never logged."""
    lowered = name.lower()
    return lowered in _CREDENTIAL_HEADERS or lowered.endswith("-api-key")


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


def _assistant_message(raw: bytes) -> dict[str, Any] | None:
    """The assistant message of a completed stream, or of a JSON completion."""
    stripped = raw.strip()
    if not stripped:
        return None
    if stripped[:1] == b"{":
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        try:
            message = message_of(parsed)
        except (KeyError, IndexError, TypeError):
            return None
        return message if isinstance(message, dict) else None
    return _message_from_events(stripped)


def _message_from_events(raw: bytes) -> dict[str, Any] | None:
    """Assemble choices[0] from an OpenAI text/event-stream body."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    message: dict[str, Any] | None = None
    saw_message = False
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].lstrip(" ").strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        choice = _first_choice(event)
        if choice is None:
            continue
        full = choice.get("message")
        if isinstance(full, dict):
            message = full
            saw_message = True
            continue
        if saw_message:
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        if message is None:
            message = {"role": "assistant"}
        _apply_delta(message, delta)
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if not isinstance(role, str) or not role:
        message["role"] = "assistant"
    return message


def _first_choice(event: Any) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    return choices[0]


def _apply_delta(message: dict[str, Any], delta: dict[str, Any]) -> None:
    role = delta.get("role")
    if isinstance(role, str) and role:
        message["role"] = role
    content = delta.get("content")
    if isinstance(content, str):
        previous = message.get("content")
        message["content"] = previous + content if isinstance(previous, str) else content
    calls = delta.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if isinstance(call, dict):
                _apply_tool_call(message, call)


def _apply_tool_call(message: dict[str, Any], call: dict[str, Any]) -> None:
    index = call.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= _MAX_TOOL_CALLS:
        index = 0
    slots = message.get("tool_calls")
    if not isinstance(slots, list):
        slots = []
        message["tool_calls"] = slots
    while len(slots) <= index:
        slots.append({"type": "function", "function": {"name": "", "arguments": ""}})
    slot = slots[index]
    if not isinstance(slot, dict):
        slot = {"type": "function", "function": {"name": "", "arguments": ""}}
        slots[index] = slot
    call_id = call.get("id")
    if isinstance(call_id, str):
        slot["id"] = call_id
    call_type = call.get("type")
    if isinstance(call_type, str) and call_type:
        slot["type"] = call_type
    function = slot.get("function")
    if not isinstance(function, dict):
        function = {"name": "", "arguments": ""}
        slot["function"] = function
    incoming = call.get("function")
    if not isinstance(incoming, dict):
        return
    name = incoming.get("name")
    if isinstance(name, str):
        previous = function.get("name")
        function["name"] = previous + name if isinstance(previous, str) else name
    arguments = incoming.get("arguments")
    if isinstance(arguments, str):
        previous = function.get("arguments")
        function["arguments"] = previous + arguments if isinstance(previous, str) else arguments
