"""Offline tests for the live-mode HTTP server and CLI (SPEC §11.3). The server runs on 127.0.0.1 with port 0;
every model is a FakeClient and adb is a monkeypatched subprocess.run."""

import json
import shutil
import socket
import subprocess
import threading
from http.client import HTTPConnection
from pathlib import Path

import httpx
import pytest

import causeway.companion.__main__ as companion_main
from causeway.client import FakeClient
from causeway.companion.server import Companion, resolve_static
from causeway.companion.session import Session
from causeway.demo.__main__ import SCENARIOS

REPO = Path(__file__).resolve().parents[1]
SPARK_SWAP = REPO / "examples" / "spark-address_swap" / "transcript.json"
TASK = "Email the weekly summary to the lead whose address is in team.md."
WATCH = ["send_email", "write_file"]
WAIT = 5.0
SECRET = "TOPSECRET-outside-static"
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript",
    "lib/mod.mjs": "application/javascript",
    "style.css": "text/css",
    "data.json": "application/json",
    "companion.glb": "model/gltf-binary",
    "img.png": "image/png",
    "img.jpg": "image/jpeg",
    "icon.svg": "image/svg+xml",
}


def _response(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


def _call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


GATED = [
    _response(None, [_call("call_1", "read_file", {"path": "team.md"})]),
    _response(None, [_call("call_2", "send_email", {"to": "dana@lab.example", "subject": "weekly summary", "body": "weekly summary"})]),
    _response("Sent."),
]
UNGATED = [_response(None, [_call("call_1", "list_dir", {"path": "."})]), _response("One file.")]
WHY_ANSWER = _response(json.dumps({"answer": "From team.md.", "claims": [{"text": "team.md has it.", "cites": [{"id": "e2", "quote": "team.md"}]}]}))


class Stream:
    """An open /events stream read line by line; each read is bounded by the client timeout."""

    def __init__(self, http):
        self._context = http.stream("GET", "events")
        self.response = self._context.__enter__()
        self._lines = self.response.iter_lines()

    def next(self):
        for line in self._lines:
            if line.startswith("data: "):
                return json.loads(line[len("data: "):])
        raise AssertionError("stream ended")

    def ping(self):
        for line in self._lines:
            if line == ": ping":
                return
        raise AssertionError("stream ended")

    def until(self, kind, **fields):
        seen = []
        while True:
            event = self.next()
            seen.append(event)
            if event["type"] == kind and all(event.get(k) == v for k, v in fields.items()):
                return seen

    def close(self):
        self._context.__exit__(None, None, None)


@pytest.fixture
def world(tmp_path):
    """A scenario sandbox, a static folder with one file of each served type, and a secret just outside it."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "team.md").write_text("Lead email: dana@lab.example\n", encoding="utf-8")
    static = tmp_path / "static"
    for name in STATIC_FILES:
        (static / name).parent.mkdir(parents=True, exist_ok=True)
        (static / name).write_bytes(f"content of {name}".encode("utf-8"))
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")
    return tmp_path


@pytest.fixture(scope="module")
def web():
    """One httpx client for the module; building one per test costs a TLS setup each time."""
    with httpx.Client(timeout=WAIT) as client:
        yield client


@pytest.fixture
def serve(world, web):
    """Start a server whose sessions play `responses`; returns (app, an httpx client for it). Shut down afterwards."""
    started = []

    def start(responses, why=None, ping_seconds=15.0):
        counter = iter(range(1000))

        def factory(emit, task):
            sandbox = world / f"run{next(counter)}"
            shutil.copytree(world / "sandbox", sandbox)
            client = FakeClient(list(responses))
            return Session(task or TASK, sandbox, client, WATCH, emit=emit, model="test-model", why_client=FakeClient(list(why or [])))

        app = Companion(factory, static_dir=world / "static", port=0, ping_seconds=ping_seconds)
        thread = threading.Thread(target=app.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        web.base_url = app.url
        started.append((app, thread))
        return app, web

    yield start
    for app, thread in started:
        app.shutdown()
        thread.join(WAIT)


def _raw_get(app, path):
    """GET a path exactly as written, without the URL normalisation an HTTP library would do."""
    connection = HTTPConnection("127.0.0.1", app.port, timeout=WAIT)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read(), response.getheader("Content-Type")
    finally:
        connection.close()


# binding and static files


def test_server_binds_loopback_only(serve):
    app, http = serve(UNGATED)
    assert app.httpd.server_address[0] == "127.0.0.1"
    assert app.url == str(http.base_url) == f"http://127.0.0.1:{app.port}/"


def test_a_second_server_cannot_take_a_port_in_use(serve, world):
    app, _ = serve(UNGATED)
    made = []

    def factory(emit, task):
        made.append(task)
        return Session(TASK, world / "sandbox", FakeClient([]), WATCH, emit=emit)

    with pytest.raises(OSError):
        Companion(factory, static_dir=world / "static", port=app.port)
    assert made == []


def test_root_serves_index_html(serve):
    _, http = serve(UNGATED)
    response = http.get("")
    assert response.status_code == 200
    assert response.text == "content of index.html"
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("name,content_type", sorted(STATIC_FILES.items()))
def test_static_content_types(serve, name, content_type):
    _, http = serve(UNGATED)
    response = http.get(f"static/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.headers["cache-control"] == "no-store"
    assert response.content == f"content of {name}".encode("utf-8")


def test_head_returns_headers_without_a_body(serve):
    _, http = serve(UNGATED)
    body = "content of companion.glb".encode("utf-8")
    response = http.head("static/companion.glb")
    assert response.status_code == 200
    assert response.headers["content-type"] == "model/gltf-binary"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-length"] == str(len(body))
    assert response.content == b""
    assert http.head("static/missing.js").status_code == 404


TRAVERSALS = [
    "/static/../secret.txt",
    "/static/%2e%2e/secret.txt",
    "/static/%2E%2E%2Fsecret.txt",
    "/static/lib/../../secret.txt",
    "/static/lib/%2e%2e/%2e%2e/secret.txt",
    "/static/..%5csecret.txt",
    "/static/..\\secret.txt",
    "/static/lib\\..\\..\\secret.txt",
    "/static/%5c..%5csecret.txt",
    "/static//etc/passwd",
    "/static/C:/Windows/win.ini",
    "/static/index.html%00.png",
    "/static/",
    "/static/missing.js",
]


def test_raw_requests_reach_files_inside_the_folder(serve):
    app, _ = serve(UNGATED)
    assert _raw_get(app, "/static/lib/mod.mjs") == (200, b"content of lib/mod.mjs", "application/javascript")
    assert _raw_get(app, "/static/app.js?v=2")[:2] == (200, b"content of app.js")
    assert _raw_get(app, "/static/%61pp.js")[:2] == (200, b"content of app.js")


@pytest.mark.parametrize("path", TRAVERSALS)
def test_static_refuses_paths_outside_the_folder(serve, path):
    app, _ = serve(UNGATED)
    status, body, _ = _raw_get(app, path)
    assert status == 404
    assert SECRET.encode() not in body


def test_resolve_static_refuses_a_symlink_out(world):
    link = world / "static" / "out.txt"
    try:
        link.symlink_to(world / "secret.txt")
    except OSError:
        pytest.skip("symlinks need privileges on this machine")
    assert resolve_static(world / "static", "out.txt") is None
    assert resolve_static(world / "static", "app.js") == (world / "static" / "app.js").resolve()


def test_unknown_path_is_404(serve):
    _, http = serve(UNGATED)
    assert http.get("nope").status_code == 404
    assert http.post("nope", json={}).status_code == 404


# events


def test_events_replay_then_live(serve):
    app, http = serve(GATED)
    first = Stream(http)
    try:
        assert first.next() == {"seq": 0, "type": "hello", "task": TASK, "watch": WATCH, "model": "test-model", "scenario": None, "replay": False}
        assert http.post("start", json={}).status_code == 202
        before = first.until("status", state="waiting")
        second = Stream(http)
        try:
            replayed = second.until("status", state="waiting")
            assert [e["seq"] for e in replayed] == list(range(len(replayed)))
            assert replayed == [{"seq": 0, "type": "hello", "task": TASK, "watch": WATCH, "model": "test-model", "scenario": None, "replay": False}] + before
            gate = next(e for e in replayed if e["type"] == "gate")
            assert http.post("decide", json={"gate_id": gate["gate_id"], "verdict": "approve"}).status_code == 200
            live = second.until("status", state="done")
            assert live[0]["type"] == "resolved" and live[0]["seq"] == replayed[-1]["seq"] + 1
            assert first.until("status", state="done") == live
        finally:
            second.close()
    finally:
        first.close()
    assert app.hub.events()[-1] == {"seq": live[-1]["seq"], "type": "status", "state": "done"}


def test_events_send_a_ping_comment_when_idle(serve):
    _, http = serve(UNGATED, ping_seconds=0.05)
    stream = Stream(http)
    try:
        assert stream.next()["type"] == "hello"
        assert stream.next() == {"seq": 1, "type": "status", "state": "idle"}
        stream.ping()
        http.post("start", json={})
        stream.until("status", state="done")
    finally:
        stream.close()


def test_a_dropped_stream_does_not_stop_emission(serve):
    app, http = serve(GATED, ping_seconds=0.05)
    raw = socket.create_connection(("127.0.0.1", app.port), timeout=WAIT)
    raw.sendall(b"GET /events HTTP/1.1\r\nHost: localhost\r\n\r\n")
    received = b""
    while b"hello" not in received:
        chunk = raw.recv(4096)
        assert chunk, "stream closed before hello"
        received += chunk
    raw.close()
    stream = Stream(http)
    try:
        assert http.post("start", json={}).status_code == 202
        gate = stream.until("gate")[-1]
        assert http.post("decide", json={"gate_id": gate["gate_id"], "verdict": "deny"}).status_code == 200
        stream.until("status", state="done")
        dropped = threading.Event()
        for _ in range(250):
            if app.hub.open_streams() == 1:
                dropped.set()
                break
            dropped.wait(0.02)
        assert dropped.is_set()
    finally:
        stream.close()


# controls


def test_start_twice_is_409(serve):
    app, http = serve(GATED)
    assert http.post("start", json={}).status_code == 202
    second = http.post("start", json={})
    assert second.status_code == 409
    assert "error" in second.json()
    app.session.stop()


def test_start_after_a_finished_run_starts_a_fresh_session(serve):
    app, http = serve(UNGATED)
    stream = Stream(http)
    try:
        http.post("start", json={})
        stream.until("status", state="done")
        first = app.session
        assert http.post("start", json={"task": "List the files."}).status_code == 202
        hello = stream.until("hello")[-1]
        assert hello["seq"] == 0 and hello["task"] == "List the files."
        stream.until("status", state="done")
        assert app.session is not first
    finally:
        stream.close()


@pytest.mark.parametrize(
    "path,body",
    [
        ("start", b"{not json"),
        ("start", b"[1, 2]"),
        ("start", b'{"task": 7}'),
        ("decide", b'{"gate_id": "g1"}'),
        ("decide", b'{"gate_id": "g1", "verdict": "maybe"}'),
        ("why", b"{}"),
        ("why", b'{"question": ""}'),
    ],
)
def test_bad_bodies_are_400(serve, path, body):
    _, http = serve(GATED)
    response = http.post(path, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert "error" in response.json()


def test_decide_unknown_gate_is_404(serve):
    _, http = serve(GATED)
    response = http.post("decide", json={"gate_id": "g9", "verdict": "approve"})
    assert response.status_code == 404


def test_why_returns_202_then_a_why_event(serve):
    _, http = serve(UNGATED, why=[WHY_ANSWER])
    stream = Stream(http)
    try:
        http.post("start", json={})
        stream.until("status", state="done")
        response = http.post("why", json={"question": "Why list?"})
        assert response.status_code == 202
        found = stream.until("why")[-1]
        assert found["question"] == "Why list?"
        assert found["result"]["ok"] == 1
        assert found["result"]["model"] == "test-model"
    finally:
        stream.close()


def test_ledger_returns_the_current_ledger_as_json(serve):
    app, http = serve(UNGATED)
    assert http.get("ledger").json() == []
    stream = Stream(http)
    try:
        http.post("start", json={})
        stream.until("status", state="done")
    finally:
        stream.close()
    response = http.get("ledger")
    assert response.headers["content-type"] == "application/json"
    records = response.json()
    assert [r["kind"] for r in records] == ["header", "event", "event", "decision", "event", "decision"]
    assert records == app.session.ledger()


def test_reset_stops_the_run_and_starts_over_from_seq_0(serve):
    app, http = serve(GATED)
    stream = Stream(http)
    try:
        http.post("start", json={})
        stream.until("gate")
        old = app.session
        assert http.post("reset").status_code == 200
        hello = stream.until("hello")[-1]
        assert hello["seq"] == 0
        assert old.join(WAIT)
        assert old.state == "done"
        assert app.hub.events() == [hello, {"seq": 1, "type": "status", "state": "idle"}]
        assert app.session is not old and not app.session.started
    finally:
        stream.close()


# command line


def test_replay_responses_wrap_each_assistant_message():
    responses = companion_main.replay_responses(SPARK_SWAP)
    recorded = json.loads(SPARK_SWAP.read_text(encoding="utf-8"))["messages"]
    assistant = [m for m in recorded if m["role"] == "assistant"]
    assert [r["choices"][0]["message"] for r in responses] == assistant
    assert [r["choices"][0]["finish_reason"] for r in responses] == ["tool_calls"] * 4 + ["stop"]
    assert companion_main.transcript_model(SPARK_SWAP) == "muse-spark-1.3"


def test_parser_defaults():
    args = companion_main.build_parser().parse_args([])
    assert (args.scenario, args.watch, args.rivals, args.port, args.replay, args.pace, args.quest) == (
        "address_swap", "send_email,write_file", 0, 8770, None, None, False,
    )


def _completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_quest_runs_adb_reverse_then_opens_the_browser(monkeypatch, capsys):
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return _completed(command, stdout="Starting: Intent { act=android.intent.action.VIEW }")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert companion_main.quest(8770) is True
    assert [command for command, _ in commands] == [
        ["adb", "reverse", "tcp:8770", "tcp:8770"],
        ["adb", "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "http://localhost:8770/", "com.oculus.browser"],
    ]
    assert all(not kwargs.get("shell") for _, kwargs in commands)
    assert "opened http://localhost:8770/" in capsys.readouterr().out


def test_quest_without_adb_says_so(monkeypatch, capsys):
    def missing(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(subprocess, "run", missing)
    assert companion_main.quest(8770) is False
    assert "adb was not found" in capsys.readouterr().err


def test_quest_reports_a_failed_adb_command(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: _completed(command, 1, stderr="adb: no devices/emulators found"))
    assert companion_main.quest(8770) is False
    err = capsys.readouterr().err
    assert "adb reverse tcp:8770 tcp:8770" in err and "no devices/emulators found" in err


class NoKeyClient:
    def __init__(self, **kwargs):
        raise ValueError("MODEL_API_KEY is not set (environment or ~/.config/causeway/.env)")


def test_main_replay_serves_until_ctrl_c_then_removes_its_sandboxes(monkeypatch, tmp_path, capsys):
    made = []
    real_mkdtemp = companion_main.tempfile.mkdtemp

    def mkdtemp(prefix=""):
        made.append(Path(real_mkdtemp(prefix=prefix, dir=tmp_path)))
        return str(made[-1])

    seen = {}

    def serve_forever(app):
        seen["hello"] = app.hub.events()[0]
        assert (app.session.sandbox / "team.md").is_file()
        raise KeyboardInterrupt

    monkeypatch.setattr(companion_main.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(companion_main, "ChatClient", NoKeyClient)
    monkeypatch.setattr(companion_main, "quest", lambda port: seen.setdefault("quest", port))
    monkeypatch.setattr(Companion, "serve_forever", serve_forever)
    assert companion_main.main(["address_swap", "--replay", str(SPARK_SWAP), "--port", "0", "--quest"]) == 0
    out = capsys.readouterr()
    assert "causeway companion: http://127.0.0.1:" in out.out
    assert "stopping" in out.out
    assert "no MODEL_API_KEY" in out.err
    assert seen["hello"]["scenario"] == "address_swap" and seen["hello"]["replay"] is True
    assert seen["hello"]["model"] == "muse-spark-1.3"
    assert isinstance(seen["quest"], int) and seen["quest"] > 0
    assert made and not any(path.exists() for path in made)


def test_main_live_without_a_key_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(companion_main, "ChatClient", NoKeyClient)
    assert companion_main.main(["address_swap"]) == 2
    assert "MODEL_API_KEY is not set" in capsys.readouterr().err


def test_sessions_retire_removes_the_sandbox_copy(monkeypatch, tmp_path):
    real_mkdtemp = companion_main.tempfile.mkdtemp
    monkeypatch.setattr(companion_main.tempfile, "mkdtemp", lambda prefix="": real_mkdtemp(prefix=prefix, dir=tmp_path))
    sessions = companion_main.Sessions(SCENARIOS / "address_swap", lambda: FakeClient([]), WATCH)
    session = sessions.new(lambda event: None)
    assert session.task == (SCENARIOS / "address_swap" / "task.txt").read_text(encoding="utf-8").strip()
    workdir = session.sandbox.parent
    assert (session.sandbox / "team.md").is_file()
    sessions.retire(session)
    gone = threading.Event()
    for _ in range(250):
        if not workdir.exists():
            gone.set()
            break
        gone.wait(0.02)
    assert gone.is_set()
