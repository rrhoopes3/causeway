"""Proxy mode records an outside agent's chat completions (SPEC §9 slice 5).

The upstream is a local fake. Nothing calls the Meta Model API, and the bearer
token in these tests is a fixture, not a real key.
"""

import http.client
import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx

from causeway.cli import main
from causeway.ledger import dumps, read
from causeway.proxy import Proxy, _outbound_headers
from causeway.transcript import to_ledger

KEY = "test-key-proxy"
MISSING = "error: no such file: sensor_board.jpg"
SYSTEM = (
    "You are a file assistant working in a sandbox folder. Use the tools to look at files and do the task."
)
USER = "Summarize the notes folder for me."
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    }
    for name in ("list_dir", "read_file", "write_file")
]


def _call(call_id, name, args, content=None):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    }


def _completion(message):
    finish = "tool_calls" if message.get("tool_calls") else "stop"
    return {"id": "chatcmpl-proxy", "choices": [{"index": 0, "message": message, "finish_reason": finish}]}


class _HeldStream:
    """SSE body in two chunks. The second is held until `release` is set."""

    def __init__(self, first: bytes, rest: bytes):
        self.first = first
        self.rest = rest
        self.release = threading.Event()
        self.released_in_time = False


class _Upstream(ThreadingHTTPServer):
    """Scripted chat API. `responses` are (status, JSON body) pairs, consumed in order.

    A payload may instead be a `_HeldStream`, which is written as text/event-stream.
    """

    def __init__(self, responses):
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)
        self.responses = list(responses)
        self.seen = []
        self.sent = []


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        server: _Upstream = self.server  # type: ignore[assignment]
        if urlsplit(self.path).path.rstrip("/") != "/v1/chat/completions":
            self._reply(404, b'{"error":{"message":"not found"}}')
            return
        server.seen.append({
            "path": urlsplit(self.path).path,
            "authorization": self.headers.get("Authorization"),
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": json.loads(raw) if raw else None,
            "raw": raw,
        })
        if not server.responses:
            self._reply(500, b'{"error":{"message":"no scripted response"}}')
            return
        status, payload = server.responses.pop(0)
        if isinstance(payload, _HeldStream):
            self._held_stream(server, payload)
            return
        encoded = json.dumps(payload).encode("utf-8")
        server.sent.append(encoded)
        self._reply(status, encoded)

    def _held_stream(self, server: _Upstream, payload: _HeldStream):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self._chunk(payload.first)
        payload.released_in_time = payload.release.wait(3)
        self._chunk(payload.rest)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        server.sent.append(payload.first + payload.rest)

    def _chunk(self, payload: bytes):
        self.wfile.write(f"{len(payload):X}\r\n".encode("ascii"))
        self.wfile.write(payload)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _reply(self, status, encoded):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        return


@contextmanager
def _session(tmp_path, responses, **kwargs):
    upstream = _Upstream(responses)
    thread = threading.Thread(target=upstream.serve_forever, name="fake-upstream", daemon=True)
    thread.start()
    host, port = upstream.server_address[:2]
    proxy = Proxy(f"http://{host}:{port}/v1", tmp_path / "run.jsonl", timeout=5, **kwargs)
    try:
        with proxy.listening() as bound:
            yield proxy, upstream, bound
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


def test_fake_upstream_round_trips_a_tool_conversation_and_tags_the_ledger(tmp_path):
    """An agent whose base_url is the proxy gets the upstream replies back, and the ledger is tagged.

    The second read of sensor_board.jpg follows `error: no such file: sensor_board.jpg`. That echo is not a
    source, so the retry stays UNSOURCED. The other decisions cover GOAL, IN, CARRY·1, SELF and FINAL.
    """
    replies = [
        _call("c1", "list_dir", {"path": "notes"}),
        _call("c2", "read_file", {"path": "draft.md"}, "I will save the result to digest-2026.md after reading the draft."),
        _call("c3", "read_file", {"path": "ideas.md"}),
        _call("c4", "write_file", {"path": "digest-2026.md", "mode": "w"}),
        _call("c5", "read_file", {"path": "sensor_board.jpg"}),
        _call("c6", "read_file", {"path": "sensor_board.jpg"}),
        {"role": "assistant", "content": "Wrote digest-2026.md with a summary of the drafts."},
    ]
    results = [
        "draft.md\nideas.md\n",
        "# Draft\n- parser works\n",
        "# Ideas\n",
        "wrote 18 bytes to digest-2026.md",
        MISSING,
        MISSING,
        None,
    ]
    scripted = [(200, _completion(reply)) for reply in replies]
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER},
    ]
    posted = []
    with _session(tmp_path, scripted, run_id="r-proxy", model="muse-spark-1.3", watch=["write_file"]) as (
        _proxy, upstream, (host, port),
    ):
        with httpx.Client(base_url=f"http://{host}:{port}/v1", timeout=5) as agent:
            for index, (reply, result) in enumerate(zip(replies, results, strict=True)):
                body = {"model": "muse-spark-1.3", "messages": list(messages), "tools": TOOLS}
                posted.append(body)
                response = agent.post("/chat/completions", json=body, headers={"Authorization": f"Bearer {KEY}"})
                assert response.status_code == 200
                assert response.content == upstream.sent[index]
                assert response.json() == _completion(reply)
                messages.append(response.json()["choices"][0]["message"])
                if result is not None:
                    messages.append({"role": "tool", "tool_call_id": reply["tool_calls"][0]["id"], "content": result})

    assert [item["path"] for item in upstream.seen] == ["/v1/chat/completions"] * len(replies)
    assert [item["authorization"] for item in upstream.seen] == [f"Bearer {KEY}"] * len(replies)
    assert [item["body"]["messages"] for item in upstream.seen] == [item["messages"] for item in posted]
    assert all(item["body"]["tools"] == TOOLS and item["body"]["model"] == "muse-spark-1.3" for item in upstream.seen)
    assert upstream.seen[1]["body"]["messages"][-1] == {
        "role": "tool", "tool_call_id": "c1", "content": "draft.md\nideas.md\n",
    }
    assert upstream.seen[5]["body"]["messages"][-1]["content"] == MISSING

    path = tmp_path / "run.jsonl"
    records = read(path)
    assert [(record["id"], record["tag"]) for record in records if record["kind"] == "decision"] == [
        ("d1", "GOAL"),
        ("d2", "IN"),
        ("d3", "CARRY\u00b71"),
        ("d4", "SELF"),
        ("d5", "UNSOURCED"),
        ("d6", "UNSOURCED"),
        ("d7", "FINAL"),
    ]
    retry = next(record for record in records if record.get("id") == "d6")
    assert retry["echo_of"] == "d5"
    assert retry["causes"] == [{
        "action": 0, "path": "path", "value": "sensor_board.jpg", "tag": "UNSOURCED", "source": None, "match": None,
    }]
    echoes = [
        record["text"] for record in records
        if record["kind"] == "event" and record["role"] == "tool" and record["text"].startswith("error: no such file:")
    ]
    assert echoes == [MISSING, MISSING]
    assert KEY not in path.read_text(encoding="utf-8")

    expected = to_ledger(messages, run_id="r-proxy", model="muse-spark-1.3", watch=["write_file"])
    expected[0]["source"] = "proxy"
    assert path.read_bytes() == dumps(expected).encode("utf-8")


def test_upstream_error_is_forwarded_without_the_key_or_a_ledger(tmp_path):
    x_api_key = "test-x-api-key"
    body = {"error": {"message": f"invalid key {KEY} {x_api_key}"}}
    with _session(tmp_path, [(401, body)]) as (_proxy, upstream, (host, port)):
        response = httpx.post(
            f"http://{host}:{port}/v1/chat/completions",
            json={"model": "muse-spark-1.3", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": f"Bearer {KEY}", "x-api-key": x_api_key},
            timeout=5,
        )
    assert response.status_code == 401
    assert KEY not in response.text
    assert x_api_key not in response.text
    assert "[redacted]" in response.text
    assert upstream.seen[0]["authorization"] == f"Bearer {KEY}"
    assert upstream.seen[0]["headers"]["x-api-key"] == x_api_key
    assert not (tmp_path / "run.jsonl").exists()


def test_unknown_paths_are_not_forwarded(tmp_path):
    with _session(tmp_path, []) as (_proxy, upstream, (host, port)):
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=5) as agent:
            missing = agent.post("/v1/models", json={"unused": True})
            other = agent.get("/v1/chat/completions")
    assert missing.status_code == 404
    assert other.status_code == 404
    assert upstream.seen == []
    assert not (tmp_path / "run.jsonl").exists()


def _sse(payload):
    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"


def test_stream_reaches_upstream_and_the_missing_file_retry_stays_unsourced(tmp_path):
    """A stream:true request is forwarded, not rejected locally, and the ledger is tagged.

    The upstream holds the second chunk until the client has read the first, so a proxy
    that buffered the whole body would miss the release. The assembled tool call retries
    sensor_board.jpg after `error: no such file: sensor_board.jpg`, which stays UNSOURCED.
    """
    args = json.dumps({"path": "sensor_board.jpg"})
    head, tail = args[:8], args[8:]
    content = "Noted. Done."
    first = _sse({
        "choices": [{
            "index": 0,
            "delta": {
                "role": "assistant",
                "content": "Noted. ",
                "tool_calls": [{
                    "index": 0,
                    "id": "c6",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": head},
                }],
            },
        }],
    })
    rest = _sse({
        "choices": [{
            "index": 0,
            "delta": {
                "content": "Done.",
                "tool_calls": [{"index": 0, "function": {"arguments": tail}}],
            },
        }],
    }) + _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}) + b"data: [DONE]\n\n"
    held = _HeldStream(first, rest)
    prior = _call("c5", "read_file", {"path": "sensor_board.jpg"})
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER},
        prior,
        {"role": "tool", "tool_call_id": "c5", "content": MISSING},
    ]
    reply = _call("c6", "read_file", {"path": "sensor_board.jpg"}, content)
    with _session(tmp_path, [(200, held)], run_id="r-proxy", model="muse-spark-1.3") as (
        _proxy, upstream, (host, port),
    ):
        with httpx.Client(timeout=10) as agent:
            with agent.stream(
                "POST",
                f"http://{host}:{port}/v1/chat/completions",
                json={"model": "muse-spark-1.3", "messages": messages, "stream": True},
                headers={"Authorization": f"Bearer {KEY}", "OpenAI-Project": "proj-stream-test"},
            ) as response:
                assert response.status_code != 400
                assert response.status_code == 200
                received = bytearray()
                for chunk in response.iter_bytes():
                    if chunk and not held.release.is_set():
                        held.release.set()
                    received.extend(chunk)
    assert held.released_in_time, "proxy buffered the stream until the upstream finished"
    assert upstream.seen, "stream request never reached upstream"
    assert upstream.seen[0]["body"]["stream"] is True
    assert upstream.seen[0]["headers"]["openai-project"] == "proj-stream-test"
    assert bytes(received) == first + rest
    assert b"omit stream" not in received
    path = tmp_path / "run.jsonl"
    records = read(path)
    retry = next(record for record in records if record.get("id") == "d2")
    assert retry["tag"] == "UNSOURCED"
    assert retry["echo_of"] == "d1"
    assert retry["causes"] == [{
        "action": 0, "path": "path", "value": "sensor_board.jpg", "tag": "UNSOURCED", "source": None, "match": None,
    }]
    expected = to_ledger(messages + [reply], run_id="r-proxy", model="muse-spark-1.3")
    expected[0]["source"] = "proxy"
    assert path.read_bytes() == dumps(expected).encode("utf-8")
    assert KEY not in path.read_text(encoding="utf-8")


def test_provider_headers_reach_upstream_and_hop_by_hop_headers_do_not(tmp_path):
    """Project, organization, and provider API-key headers are forwarded. Hop-by-hop headers are not."""
    reply = _completion({"role": "assistant", "content": "ok"})
    org = "org-test"
    project = "proj-test"
    x_api_key = "test-x-api-key"
    api_key = "test-api-key-value"
    sent = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {KEY}",
        "OpenAI-Organization": org,
        "OpenAI-Project": project,
        "x-api-key": x_api_key,
        "api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "test-beta",
        "Keep-Alive": "timeout=9",
        "Upgrade": "websocket",
        "TE": "trailers",
        "Trailers": "x-test-trailer",
        "Connection": "close, x-hop-secret",
        "X-Hop-Secret": "should-not-forward",
        "Host": "evil.example",
    }
    raw = json.dumps({"model": "muse-spark-1.3", "messages": [{"role": "user", "content": "Hi"}]}).encode("utf-8")
    with _session(tmp_path, [(200, reply)]) as (_proxy, upstream, (host, port)):
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("POST", "/v1/chat/completions", body=raw, headers=sent)
        response = connection.getresponse()
        downloaded = response.read()
        status = response.status
        connection.close()
    assert status == 200
    assert downloaded == upstream.sent[0]
    headers = upstream.seen[0]["headers"]
    assert headers["openai-organization"] == org
    assert headers["openai-project"] == project
    assert headers["x-api-key"] == x_api_key
    assert headers["api-key"] == api_key
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["anthropic-beta"] == "test-beta"
    assert headers["authorization"] == f"Bearer {KEY}"
    blob = "\n".join(f"{key}: {value}" for key, value in headers.items())
    assert "evil.example" not in blob
    assert "should-not-forward" not in blob
    assert "timeout=9" not in blob
    assert "x-test-trailer" not in blob
    assert "websocket" not in blob.lower()
    assert "x-hop-secret" not in blob.lower()
    ledger = (tmp_path / "run.jsonl").read_text(encoding="utf-8")
    assert x_api_key not in ledger
    assert api_key not in ledger


def test_outbound_headers_drop_hop_by_hop_and_keep_provider_headers():
    """Content-Length and Transfer-Encoding are not copied; a missing Authorization uses the proxy key."""
    outbound, secrets = _outbound_headers(
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {KEY}",
            "OpenAI-Organization": "org-test",
            "OpenAI-Project": "proj-test",
            "x-api-key": "test-x-api-key",
            "api-key": "test-api-key-value",
            "anthropic-version": "2023-06-01",
            "Connection": "close, x-hop-secret",
            "Keep-Alive": "timeout=9",
            "Transfer-Encoding": "chunked",
            "Upgrade": "websocket",
            "Host": "evil.example",
            "Content-Length": "999",
            "TE": "trailers",
            "Trailers": "x-test-trailer",
            "X-Hop-Secret": "should-not-forward",
            "X-Bad": "nope\r\nX-Other: yes",
        },
        api_key=None,
    )
    lowered = {key.lower(): value for key, value in outbound.items()}
    assert lowered["content-type"] == "application/json"
    assert lowered["authorization"] == f"Bearer {KEY}"
    assert lowered["openai-organization"] == "org-test"
    assert lowered["openai-project"] == "proj-test"
    assert lowered["x-api-key"] == "test-x-api-key"
    assert lowered["api-key"] == "test-api-key-value"
    assert lowered["anthropic-version"] == "2023-06-01"
    for name in (
        "connection", "keep-alive", "transfer-encoding", "upgrade", "host",
        "content-length", "te", "trailers", "trailer", "x-hop-secret", "x-bad", "accept-encoding",
    ):
        assert name not in lowered
    assert set(secrets) == {KEY, "test-x-api-key", "test-api-key-value"}
    assert "org-test" not in secrets

    fallback, fallback_secrets = _outbound_headers({"X-Request-Id": "req-test"}, "test-meta-key-value")
    assert fallback["Authorization"] == "Bearer test-meta-key-value"
    assert fallback["Content-Type"] == "application/json"
    assert fallback["X-Request-Id"] == "req-test"
    assert fallback_secrets == ["test-meta-key-value"]


def test_local_upstream_does_not_receive_the_meta_key(tmp_path, monkeypatch):
    meta_key = "test-meta-key-should-not-leak"
    monkeypatch.setenv("MODEL_API_KEY", meta_key)
    reply = _completion({"role": "assistant", "content": "ok"})
    with _session(tmp_path, [(200, reply)]) as (_proxy, upstream, (host, port)):
        response = httpx.post(
            f"http://{host}:{port}/v1/chat/completions",
            json={"model": "local", "messages": [{"role": "user", "content": "Hi"}]},
            timeout=5,
        )
    assert response.status_code == 200
    assert upstream.seen[0]["authorization"] is None
    assert meta_key.encode() not in upstream.seen[0]["raw"]
    assert meta_key not in response.text
    assert meta_key not in (tmp_path / "run.jsonl").read_text(encoding="utf-8")


def test_proxy_command_rejects_a_listen_address_without_a_port(capsys):
    code = main(["proxy", "--upstream", "http://127.0.0.1:9/v1", "-o", "run.jsonl", "--listen", "localhost"])
    assert code == 1
    assert "HOST:PORT" in capsys.readouterr().err
