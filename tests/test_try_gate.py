"""Offline tests for `python -m causeway.companion.try_gate`: the scripted check, the page's words, and whole runs with the
decision driven over HTTP the way the page's own script sends it. No model, no Muse, and nothing beyond 127.0.0.1; the
tunnel is stubbed. Skipped when the `muse` extra (mcp, uvicorn) is not installed."""

import _thread
import base64
import hashlib
import itertools
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")
pytest.importorskip("uvicorn")

import httpx  # noqa: E402
from mcp.types import CallToolResult, TextContent  # noqa: E402

from causeway.client import FakeClient  # noqa: E402
from causeway.companion import try_gate  # noqa: E402
from causeway.companion.phone import IMPORT_MAP, NO_MODEL  # noqa: E402
from causeway.companion.server import STATIC_DIR  # noqa: E402
from causeway.demo.__main__ import SCENARIOS  # noqa: E402

MCP_KEY = "test-mcp-key-0123456789"
OWNER = "test-owner-key-0123456789"
SCENARIO = SCENARIOS / "address_swap"
THE_LEADS_ADDRESS = "dana@lab.example"
PUBLIC = "https://some-words-here.trycloudflare.com"
LIMIT = 20.0
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No key to find, so no test can reach a model; known keys, so a test can find the page; and short pauses."""
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    keys = itertools.cycle([MCP_KEY, OWNER])
    monkeypatch.setattr(try_gate, "secrets", SimpleNamespace(token_urlsafe=lambda _: next(keys)))
    monkeypatch.setattr(try_gate, "PAUSE", 0.05)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def ports():
    return _free_port(), _free_port()


class Owner:
    """What the page does, over HTTP: poll the state, and tap."""

    def __init__(self, ports, base=None):
        self.companion = f"http://127.0.0.1:{ports[0]}"
        self.base = base or f"http://127.0.0.1:{ports[1]}"
        self.root = f"{self.base}/o/{OWNER}/"

    def get(self, route=""):
        return httpx.get(self.root + route, timeout=LIMIT)

    def state(self):
        response = self.get("state")
        assert response.status_code == 200
        return response.json()

    def gate(self, not_this=None):
        """The state of the next open gate other than `not_this`."""
        deadline = time.monotonic() + LIMIT
        while time.monotonic() < deadline:
            state = self.state()
            if state["verdict"] == "waiting" and state["gate_id"] != not_this:
                return state
            time.sleep(0.02)
        raise AssertionError(f"no gate opened: {state}")

    def post(self, route, payload, host=None, origin=None):
        headers = {"Content-Type": "application/json", "Origin": origin or self.base}
        if host:
            headers["Host"] = host
        return httpx.post(self.root + route, content=json.dumps(payload), headers=headers, timeout=LIMIT)

    def tap(self, gate_id, verdict, **kwargs):
        assert self.post("decide", {"gate_id": gate_id, "verdict": verdict}, **kwargs).status_code == 200

    def hello(self):
        """The newest session's hello, as the companion page reads it from the stream."""
        with httpx.stream("GET", f"{self.companion}/events", timeout=LIMIT) as stream:
            for line in stream.iter_lines():
                if line.startswith("data: ") and json.loads(line[6:])["type"] == "hello":
                    return json.loads(line[6:])
        raise AssertionError("no hello")


def _wait_for_listener(port):
    deadline = time.monotonic() + LIMIT
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1.0).close()
            return
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"nothing listens on {port}")


def _run(ports, play, *flags):
    """try_gate.main on this thread, as a console runs it, with `play(owner)` as the owner on another. If `play` fails
    or takes too long, Ctrl+C ends the run, so a failure is reported instead of hanging. Returns (exit code, play's result)."""
    outcome = {}

    def owner():
        try:
            _wait_for_listener(ports[1])
            outcome["played"] = play(Owner(ports))
        except BaseException as err:
            outcome["failed"] = err
            _thread.interrupt_main()

    threading.Thread(target=owner, daemon=True).start()
    timer = threading.Timer(LIMIT * 2, _thread.interrupt_main)
    timer.start()
    try:
        code = try_gate.main(["--port", str(ports[0]), "--mcp-port", str(ports[1]), *flags])
    finally:
        timer.cancel()
    deadline = time.monotonic() + LIMIT
    while not outcome and time.monotonic() < deadline:
        time.sleep(0.02)
    if "failed" in outcome:
        raise outcome["failed"]
    return code, outcome.get("played")


def _decide_once(verdict):
    def play(owner):
        state = owner.gate()
        owner.tap(state["gate_id"], verdict)
        return state

    return play


def _is_free(port):
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) != 0


# The scripted check


def test_the_check_is_the_scenarios_request_and_raw_files_with_the_notes_address():
    check = try_gate.scripted_check()
    assert check["request"] == (SCENARIO / "task.txt").read_text(encoding="utf-8").strip()
    assert check["action"] == "send_email"
    assert {source["label"]: source["text"] for source in check["sources"]} == {
        label: (SCENARIO / "sandbox" / label).read_text(encoding="utf-8") for label in ("team.md", "notes/a.md", "notes/b.md")
    }
    assert check["arguments"]["to"] == try_gate.SWAPPED


def test_the_address_is_the_one_the_note_plants_and_not_the_leads():
    note = (SCENARIO / "sandbox" / "notes" / "b.md").read_text(encoding="utf-8")
    team = (SCENARIO / "sandbox" / "team.md").read_text(encoding="utf-8")
    assert try_gate.SWAPPED in note and try_gate.SWAPPED not in team
    assert THE_LEADS_ADDRESS in team and THE_LEADS_ADDRESS != try_gate.SWAPPED


def test_a_failed_tool_call_never_reads_as_a_verdict():
    with pytest.raises(RuntimeError, match="boom"):
        try_gate._outcome(CallToolResult(isError=True, content=[TextContent(type="text", text="boom")]))


# The page's words


@pytest.mark.parametrize("name", ["phone.html", "phone.js"])
def test_every_mention_of_the_agent_says_it_is_a_script_and_not_muse(name):
    text = try_gate.relabel((STATIC_DIR / name).read_bytes()).decode("utf-8")
    assert "not Muse" in text
    assert re.findall(r"(?<!not )\bMuse\b", text) == []


def test_a_sentence_that_starts_with_the_agent_starts_with_a_capital():
    text = try_gate.relabel((STATIC_DIR / "phone.js").read_bytes()).decode("utf-8")
    assert "'The scripted client (not Muse) is told to go ahead" in text
    assert "denied. The scripted client (not Muse) was told" in text
    assert "before the scripted client (not Muse) acts" in text
    assert not re.search(r"""['">]the scripted client""", text)


def test_every_replacement_still_finds_its_text():
    page = (STATIC_DIR / "phone.html").read_bytes() + (STATIC_DIR / "phone.js").read_bytes()
    for old, new in try_gate.PAGE_WORDS:
        assert old in page
        assert new in try_gate.relabel(page)


def test_the_import_map_is_untouched_so_its_hash_still_allows_it():
    page = (STATIC_DIR / "phone.html").read_bytes()
    assert IMPORT_MAP.search(try_gate.relabel(page)).group(1) == IMPORT_MAP.search(page).group(1)


def test_the_page_is_served_relabelled_under_its_real_policy(ports):
    def play(owner):
        response = owner.get()
        assert response.status_code == 200
        digest = base64.b64encode(hashlib.sha256(IMPORT_MAP.search(response.content).group(1)).digest()).decode()
        assert f"'sha256-{digest}'" in response.headers["content-security-policy"]
        assert b'class="brand">Causeway rehearsal<' in response.content
        assert b'<details id="setup" class="setup" hidden>' in response.content
        assert b"The scripted client (not Muse) asked to run" in response.content
        script = owner.get("static/phone.js")
        assert b"Check this before the scripted client (not Muse) acts." in script.content
        assert script.headers["content-type"] == "application/javascript"
        assert owner.get("static/state.js").content == (STATIC_DIR / "state.js").read_bytes()
        _decide_once("deny")(owner)

    code, _ = _run(ports, play)
    assert code == 0


# Whole runs


@pytest.mark.parametrize("verdict", ["approve", "deny"])
def test_one_run_opens_the_gate_and_prints_the_owners_decision(ports, capfd, verdict):
    code, state = _run(ports, _decide_once(verdict))
    assert code == 0
    assert state["action"] == "send_email"
    assert state["request"] == (SCENARIO / "task.txt").read_text(encoding="utf-8").strip()
    assert state["findings"] == [{"path": "to", "value": try_gate.SWAPPED, "label": "read(notes/b.md)", "tag": "IN"}]
    out, err = capfd.readouterr()
    assert f"verdict: {verdict}" in out
    assert out.count("verdict: ") == 1
    assert "this is a scripted client, not Muse" in out
    assert f"/o/{OWNER}/" in out
    assert f"send_email.to = {try_gate.SWAPPED}" in out
    assert MCP_KEY not in out + err


def test_the_check_comes_through_the_real_mcp_client_over_http(ports, capfd):
    code, _ = _run(ports, _decide_once("approve"))
    assert code == 0
    out = capfd.readouterr().out
    assert f'initialize client="{try_gate.CLIENT_NAME}"' in out
    assert re.search(r'tool="checkpoint" action="send_email" args=3 sources=3 source_chars=\d+ verdict=approve', out)


def test_a_decision_after_the_first_call_comes_back_through_checkpoint_result(ports, capfd, monkeypatch):
    monkeypatch.setattr(try_gate, "WAIT", 0.1)

    def play(owner):
        state = owner.gate()
        time.sleep(0.5)
        owner.tap(state["gate_id"], "deny")

    code, _ = _run(ports, play)
    assert code == 0
    out = capfd.readouterr().out
    assert "waiting for Approve or Deny on the phone page" in out
    assert 'tool="checkpoint" action="send_email"' in out and "verdict=waiting" in out
    assert re.search(r'tool="checkpoint_result" action="send_email".*verdict=deny', out)
    assert "verdict: deny" in out


def test_repeat_sends_the_check_again_after_each_decision_until_ctrl_c(ports, capfd):
    def play(owner):
        first = owner.gate()
        owner.tap(first["gate_id"], "approve")
        second = owner.gate(not_this=first["gate_id"])
        owner.tap(second["gate_id"], "deny")
        third = owner.gate(not_this=second["gate_id"])
        _thread.interrupt_main()
        return first, second, third

    code, states = _run(ports, play, "--repeat")
    assert code == 0
    assert len({state["gate_id"] for state in states}) == 3
    out = capfd.readouterr().out
    assert re.findall(r"verdict: (\w+)", out) == ["approve", "deny"]
    assert "stopping" in out
    assert _is_free(ports[0]) and _is_free(ports[1])


def test_a_run_can_follow_a_run_on_the_same_ports(ports, capfd):
    for verdict in ("approve", "deny"):
        code, _ = _run(ports, _decide_once(verdict))
        assert code == 0
        assert f"verdict: {verdict}" in capfd.readouterr().out


def test_no_check_goes_out_until_the_page_opens_and_the_companion_says_it_is_a_script(ports):
    def play(owner):
        time.sleep(0.3)  # PAUSE is 0.05 here, so a check that did not wait for the page would be out by now
        assert httpx.get(f"{owner.companion}/ledger", timeout=LIMIT).json() == []
        assert "not Muse" in owner.hello()["task"]
        state = owner.gate()
        assert owner.hello()["model"] == try_gate.SCRIPTED
        owner.tap(state["gate_id"], "approve")

    code, _ = _run(ports, play)
    assert code == 0


def test_why_says_plainly_that_no_model_is_configured(ports):
    def play(owner):
        state = owner.gate()
        assert owner.post("why", {"gate_id": state["gate_id"], "question": "Where did the recipient come from?"}).status_code == 202
        deadline = time.monotonic() + LIMIT
        while not owner.state()["why"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert owner.state()["why"]["error"] == NO_MODEL
        owner.tap(state["gate_id"], "approve")

    code, _ = _run(ports, play)
    assert code == 0


def test_why_uses_the_model_when_there_is_one(ports, monkeypatch):
    client = FakeClient([{"choices": [{"message": {"content": json.dumps({"answer": "It came from the note.", "claims": []})}}]}])
    client.model = "fake-model"
    monkeypatch.setattr(try_gate, "ChatClient", lambda: client)

    def play(owner):
        state = owner.gate()
        assert owner.post("why", {"gate_id": state["gate_id"], "question": "Where did the recipient come from?"}).status_code == 202
        deadline = time.monotonic() + LIMIT
        while not owner.state()["why"] and time.monotonic() < deadline:
            time.sleep(0.02)
        answer = owner.state()["why"]
        owner.tap(state["gate_id"], "approve")
        return answer

    code, answer = _run(ports, play)
    assert code == 0
    assert answer["result"]["answer"] == "It came from the note."
    assert answer["result"]["model"] == "fake-model"


# The tunnel


class FakeTunnel:
    terminated = False

    def terminate(self):
        self.terminated = True


def test_tunnel_prints_the_public_link_and_a_tap_through_its_host_lands(ports, capfd, monkeypatch):
    proc = FakeTunnel()
    asked = []

    def tunnel(port):
        asked.append(port)
        return proc, PUBLIC

    monkeypatch.setattr(try_gate, "tunnel", tunnel)

    def play(owner):
        state = owner.gate()
        host = PUBLIC.removeprefix("https://")
        owner.tap(state["gate_id"], "approve", host=host, origin=PUBLIC)

    code, _ = _run(ports, play, "--tunnel")
    assert code == 0
    assert asked == [ports[1]]
    assert proc.terminated
    out = capfd.readouterr().out
    assert f"{PUBLIC}/o/{OWNER}/" in out
    assert "Keep this window off camera" in out
    assert "A phone needs --tunnel" not in out
    assert MCP_KEY not in out
    assert "verdict: approve" in out


@pytest.mark.parametrize("proc", [None, FakeTunnel()], ids=["no cloudflared", "no url"])
def test_a_tunnel_with_no_address_stops_the_run_instead_of_pretending(ports, capfd, monkeypatch, proc):
    monkeypatch.setattr(try_gate, "tunnel", lambda port: (proc, None))
    code = try_gate.main(["--port", str(ports[0]), "--mcp-port", str(ports[1]), "--tunnel"])
    assert code == 1
    assert "--tunnel got no public address" in capfd.readouterr().err
    assert proc is None or proc.terminated
    assert _is_free(ports[0]) and _is_free(ports[1])


# Failures at the start


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")  # uvicorn exits its thread on a bind error
@pytest.mark.parametrize("taken", [0, 1], ids=["companion port", "mcp port"])
def test_a_taken_port_is_reported_and_ends_the_run(ports, capfd, taken):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", ports[taken]))
        sock.listen()
        code = try_gate.main(["--port", str(ports[0]), "--mcp-port", str(ports[1])])
    assert code == 1
    assert f"cannot listen on 127.0.0.1:{ports[taken]}" in capfd.readouterr().err


def test_a_client_that_fails_ends_the_run_without_a_verdict(ports, capfd, monkeypatch):
    async def fail(session, check):
        raise RuntimeError("boom")

    monkeypatch.setattr(try_gate, "_one_check", fail)

    def play(owner):
        return owner.state()

    code, _ = _run(ports, play)
    assert code == 1
    out, err = capfd.readouterr()
    assert "the scripted client failed" in err
    assert "verdict: " not in out


# The command itself


def test_the_command_runs_on_an_old_code_page_from_any_folder_with_no_keys(tmp_path, ports):
    env = {key: value for key, value in os.environ.items() if key != "MODEL_API_KEY"}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONPATH=str(REPO), PYTHONIOENCODING="cp1252")
    proc = subprocess.Popen(
        [sys.executable, "-m", "causeway.companion.try_gate", "--port", str(ports[0]), "--mcp-port", str(ports[1])],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="cp1252", env=env, cwd=tmp_path,
    )
    lines = []
    link = threading.Event()

    def read():
        for line in proc.stdout:
            lines.append(line)
            if "phone page:" in line:
                link.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert link.wait(LIMIT * 2), lines
        url = re.search(r"http://127\.0\.0\.1:\d+/o/[\w-]+/", "".join(lines)).group(0)
        assert httpx.get(url, timeout=LIMIT).status_code == 200
        deadline = time.monotonic() + LIMIT
        while (state := httpx.get(f"{url}state", timeout=LIMIT).json())["verdict"] != "waiting":
            assert time.monotonic() < deadline, state
            time.sleep(0.05)
        decided = httpx.post(
            f"{url}decide",
            content=json.dumps({"gate_id": state["gate_id"], "verdict": "approve"}),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{ports[1]}"},
            timeout=LIMIT,
        )
        assert decided.status_code == 200
        assert proc.wait(LIMIT * 2) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    reader.join(LIMIT)
    out = "".join(lines)
    assert "this is a scripted client, not Muse" in out
    assert f"send_email.to = {try_gate.SWAPPED} ? read(notes/b.md)" in out
    assert "verdict: approve" in out
    assert "Traceback" not in out
