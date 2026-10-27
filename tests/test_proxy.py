"""Proxy mode records an outside agent's chat completions (SPEC §9 slice 5).

The upstream is a local fake. Nothing calls the Meta Model API, and the bearer
token in these tests is a fixture, not a real key.
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx

from causeway.cli import main
from causeway.ledger import dumps, read
from causeway.proxy import Proxy
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


class _Upstream(ThreadingHTTPServer):
    """Scripted chat API. `responses` are (status, JSON body) pairs, consumed in order."""

    def __init__(self, responses):
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)
        self.responses = list(responses)
        self.seen = []
        self.sent = []


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
            "body": json.loads(raw) if raw else None,
            "raw": raw,
        })
        if not server.responses:
            self._reply(500, b'{"error":{"message":"no scripted response"}}')
            return
        status, payload = server.responses.pop(0)
        encoded = json.dumps(payload).encode("utf-8")
        server.sent.append(encoded)
        self._reply(status, encoded)

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
    body = {"error": {"message": f"invalid key {KEY}"}}
    with _session(tmp_path, [(401, body)]) as (_proxy, upstream, (host, port)):
        response = httpx.post(
            f"http://{host}:{port}/v1/chat/completions",
            json={"model": "muse-spark-1.3", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": f"Bearer {KEY}"},
            timeout=5,
        )
    assert response.status_code == 401
    assert KEY not in response.text
    assert "[redacted]" in response.text
    assert upstream.seen[0]["authorization"] == f"Bearer {KEY}"
    assert not (tmp_path / "run.jsonl").exists()


def test_stream_and_unknown_paths_are_not_forwarded(tmp_path):
    with _session(tmp_path, []) as (_proxy, upstream, (host, port)):
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=5) as agent:
            streamed = agent.post(
                "/v1/chat/completions",
                json={"model": "m", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            )
            missing = agent.post("/v1/models", json={"unused": True})
            other = agent.get("/v1/chat/completions")
    assert streamed.status_code == 400
    assert "stream" in streamed.json()["error"]["message"]
    assert missing.status_code == 404
    assert other.status_code == 404
    assert upstream.seen == []
    assert not (tmp_path / "run.jsonl").exists()


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
