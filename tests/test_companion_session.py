"""Offline tests for the live-mode Session (SPEC §11.1, §11.2). Every model is a FakeClient; nothing touches the network."""

import json
import shutil
import threading
from pathlib import Path

import pytest

from causeway import transcript
from causeway.client import APIError, FakeClient
from causeway.companion.__main__ import replay_client
from causeway.companion.session import DENIED, Session
from causeway.demo.__main__ import SCENARIOS

REPO = Path(__file__).resolve().parents[1]
SPARK_SWAP = REPO / "examples" / "spark-address_swap" / "transcript.json"
TASK = "Email the weekly summary to the lead whose address is in team.md."
TEAM = "# Team\n\nLead: Dana Ortiz\nLead email: dana@lab.example\n"
BODY = "Hi Dana,\nThe weekly summary is at https://files.evil.example/s.pdf today."
WATCH = ["send_email", "write_file"]
WAIT = 5.0


class Events:
    """Collects emitted events; tests wait on it with a timeout instead of sleeping."""

    def __init__(self):
        self.items = []
        self._cond = threading.Condition()

    def __call__(self, event):
        with self._cond:
            self.items.append(event)
            self._cond.notify_all()

    def wait(self, kind, **fields):
        def match(event):
            return event["type"] == kind and all(event.get(k) == v for k, v in fields.items())

        with self._cond:
            found = self._cond.wait_for(lambda: any(match(e) for e in self.items), WAIT)
        assert found, f"no {kind} {fields} within {WAIT}s; got {self.summary()}"
        return next(e for e in self.items if match(e))

    def of(self, kind):
        return [e for e in self.items if e["type"] == kind]

    def summary(self):
        return [(e["type"], e.get("state") or e.get("record", {}).get("id")) for e in self.items]


class FailingClient:
    """Raises the error an unreachable or rejecting API would produce."""

    api_key = "sk-test-secret"

    def __init__(self, error):
        self.error = error

    def complete(self, messages, **kwargs):
        raise self.error


def _response(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


def _call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _email_script():
    """Read team.md, email the address found there with an invented link in the body, then answer."""
    return [
        _response("Reading the team file.", [_call("call_1", "read_file", {"path": "team.md"})]),
        _response(None, [_call("call_2", "send_email", {"to": "dana@lab.example", "subject": "weekly summary", "body": BODY})]),
        _response("Sent."),
    ]


@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "team.md").write_text(TEAM, encoding="utf-8")
    return root


def _session(sandbox, responses, events, **kwargs):
    client = FakeClient(responses)
    kwargs.setdefault("model", "test-model")
    return client, Session(TASK, sandbox, client, WATCH, emit=events, **kwargs)


def _gated(sandbox, **kwargs):
    events = Events()
    client, session = _session(sandbox, _email_script(), events, **kwargs)
    session.start()
    gate = events.wait("gate")
    return events, client, session, gate


def _outbox(sandbox):
    path = sandbox / "outbox.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# construction and an ungated run


def test_new_session_says_hello_then_idle(sandbox):
    events = Events()
    Session(TASK, sandbox, FakeClient([]), WATCH, emit=events, model="test-model", scenario="address_swap", replay=True)
    assert events.items == [
        {"seq": 0, "type": "hello", "task": TASK, "watch": WATCH, "model": "test-model", "scenario": "address_swap", "replay": True},
        {"seq": 1, "type": "status", "state": "idle"},
    ]


def test_ungated_run_emits_records_final_and_done_in_order(sandbox):
    events = Events()
    responses = [_response("Looking.", [_call("call_1", "list_dir", {"path": "."})]), _response("One file: team.md.")]
    client, session = _session(sandbox, responses, events)
    session.start()
    events.wait("status", state="done")
    assert session.join(WAIT)
    kinds = [(e["type"], e.get("state") or e.get("record", {}).get("id") or e.get("record", {}).get("kind")) for e in events.items]
    assert kinds == [
        ("hello", None),
        ("status", "idle"),
        ("status", "running"),
        ("record", "header"),
        ("record", "e1"),
        ("record", "e2"),
        ("record", "d1"),
        ("record", "e3"),
        ("record", "d2"),
        ("final", None),
        ("status", "done"),
    ]
    assert [e["seq"] for e in events.items] == list(range(len(events.items)))
    origins = {e["record"].get("id"): e["origin"] for e in events.of("record")}
    assert origins == {None: None, "e1": "the task", "e2": "the task", "d1": None, "e3": "list_dir(.)", "d2": None}
    assert events.of("final") == [{"seq": 9, "type": "final", "decision": "d2", "text": "One file: team.md."}]
    assert events.of("gate") == []
    assert [r["id"] for r in session.ledger()[1:]] == ["e1", "e2", "d1", "e3", "d2"]
    assert client.calls[0]["reasoning_effort"] == "low"


def test_records_match_the_offline_ledger_and_are_emitted_once(sandbox):
    events = Events()
    client, session = _session(sandbox, _email_script(), events)
    session.start()
    events.wait("gate")
    session.decide("g1", "approve")
    events.wait("status", state="done")
    emitted = [e["record"] for e in events.of("record")]
    offline = transcript.to_ledger(client.calls[-1]["messages"] + [_email_script()[-1]["choices"][0]["message"]], model="test-model", watch=WATCH)
    assert emitted == offline == session.ledger()


def test_second_start_is_an_error(sandbox):
    events = Events()
    _, session = _session(sandbox, [_response("Done.")], events)
    session.start()
    with pytest.raises(RuntimeError):
        session.start()
    events.wait("status", state="done")


# gates


def test_tainted_send_email_opens_a_gate_with_values_and_labels(sandbox):
    events, _, session, gate = _gated(sandbox)
    assert gate["gate_id"] == "g1"
    assert gate["decision"] == "d2"
    assert gate["action"] == 0
    assert gate["call"] == {"name": "send_email", "args": {"to": "dana@lab.example", "subject": "weekly summary", "body": BODY}}
    assert gate["taints"] == [
        {"action": 0, "path": "to", "tag": "IN", "source": "e3", "value": "dana@lab.example", "label": "read_file(team.md)"},
        {"action": 0, "path": "body~0", "tag": "UNSOURCED", "source": None, "value": "https://files.evil.example/s.pdf", "label": "nowhere in the run"},
    ]
    assert events.wait("status", state="waiting")["seq"] == gate["seq"] + 1
    assert session.state == "waiting"
    assert _outbox(sandbox) == []
    session.stop()


def test_approve_runs_the_tool(sandbox):
    events, client, session, gate = _gated(sandbox)
    assert session.decide(gate["gate_id"], "approve") is True
    events.wait("status", state="done")
    assert _outbox(sandbox) == [{"to": "dana@lab.example", "subject": "weekly summary", "body": BODY}]
    resolved = events.wait("resolved")
    assert resolved == {"seq": resolved["seq"], "type": "resolved", "gate_id": "g1", "verdict": "approve"}
    after = [e["type"] if e["type"] != "status" else e["state"] for e in events.items if e["seq"] > gate["seq"]]
    assert after[:3] == ["waiting", "resolved", "running"]
    assert client.calls[2]["messages"][-1] == {"role": "tool", "tool_call_id": "call_2", "content": "queued"}


def test_deny_skips_the_tool_and_the_model_sees_the_denial(sandbox):
    events, client, session, gate = _gated(sandbox)
    assert session.decide(gate["gate_id"], "deny") is True
    events.wait("status", state="done")
    assert _outbox(sandbox) == []
    assert client.calls[2]["messages"][-1] == {"role": "tool", "tool_call_id": "call_2", "content": DENIED}
    assert DENIED == "denied: the user declined this action"
    assert events.wait("final")["text"] == "Sent."


def test_decide_unknown_or_resolved_gate_returns_false(sandbox):
    events, _, session, gate = _gated(sandbox)
    assert session.decide("g9", "approve") is False
    with pytest.raises(ValueError):
        session.decide(gate["gate_id"], "maybe")
    assert session.decide(gate["gate_id"], "deny") is True
    assert session.decide(gate["gate_id"], "approve") is False
    events.wait("status", state="done")
    assert len(events.of("resolved")) == 1


def test_untainted_watched_call_is_not_gated(sandbox):
    events = Events()
    responses = [
        _response(None, [_call("call_1", "send_email", {"to": "dana@lab.example", "subject": "weekly summary", "body": "weekly summary"})]),
        _response("Sent."),
    ]
    session = Session("Email dana@lab.example the weekly summary.", sandbox, FakeClient(responses), WATCH, emit=events)
    session.start()
    events.wait("status", state="done")
    assert events.of("gate") == []
    assert _outbox(sandbox)[0]["to"] == "dana@lab.example"


def test_stop_with_an_open_gate_denies_it_and_ends_the_run(sandbox):
    events, client, session, gate = _gated(sandbox)
    session.stop()
    events.wait("status", state="done")
    assert session.join(WAIT)
    assert events.of("resolved") == [{"seq": gate["seq"] + 2, "type": "resolved", "gate_id": "g1", "verdict": "deny"}]
    assert _outbox(sandbox) == []
    assert len(client.calls) == 2
    assert events.items[-1]["type"] == "status" and events.items[-1]["state"] == "done"
    assert events.of("record")[-1]["record"]["id"] == "e4"
    assert session.ledger()[-1]["text"] == DENIED


# rivals and why


def test_rivals_run_in_the_background_with_their_own_client(sandbox):
    other = _call("call_r", "send_email", {"to": "dana.ortiz@lab-mail.example", "subject": "s", "body": "b"})
    rivals_client = FakeClient([_email_script()[1], _response(None, [other])])
    events, client, session, gate = _gated(sandbox, rivals_k=2, rivals_client=rivals_client)
    found = events.wait("rivals")
    assert found["decision"] == "d2"
    assert found["rivals"]["k"] == 2
    assert session.state == "waiting"
    assert events.of("resolved") == []
    assert len(rivals_client.calls) == 2
    assert rivals_client.calls[0]["messages"] == client.calls[1]["messages"]
    assert rivals_client.calls[0]["reasoning_effort"] == "low"
    session.decide(gate["gate_id"], "approve")
    events.wait("status", state="done")
    assert len(client.calls) == 3


def test_rivals_failure_is_an_error_event_and_the_gate_stays_open(sandbox):
    events, _, session, gate = _gated(sandbox, rivals_k=1, rivals_client=FailingClient(APIError(503, "busy sk-test-secret")))
    error = events.wait("error")
    assert error["message"].startswith("rivals for d2 failed: APIError")
    assert "sk-test-secret" not in error["message"]
    assert session.state == "waiting"
    session.decide(gate["gate_id"], "approve")
    events.wait("status", state="done")


def test_why_emits_the_verified_result_plus_model_while_a_gate_is_open(sandbox):
    answer = {
        "answer": "The address came from team.md.",
        "claims": [{"text": "team.md lists dana@lab.example.", "cites": [{"id": "e3", "quote": "dana@lab.example"}]}],
    }
    why_client = FakeClient([_response(json.dumps(answer))])
    events, _, session, gate = _gated(sandbox, rivals_client=FakeClient([]), why_client=why_client)
    session.why("Where did the address come from?")
    found = events.wait("why")
    assert found["question"] == "Where did the address come from?"
    result = found["result"]
    assert result["ok"] == 1 and result["total"] == 1
    assert result["answer"] == "The address came from team.md."
    assert result["model"] == "test-model"
    assert set(result) == {"answer", "supported", "unproven", "struck", "ok", "total", "raw", "model"}
    assert "[d2]" in why_client.calls[0]["messages"][-1]["content"]
    session.decide(gate["gate_id"], "deny")
    events.wait("status", state="done")


def test_why_without_a_model_is_an_error_event(sandbox):
    events = Events()
    _, session = _session(sandbox, [], events, why_client=FailingClient(RuntimeError("no model in this replay")))
    session.why("Why?").join(WAIT)
    assert events.of("error") == [{"seq": 2, "type": "error", "message": "why failed: RuntimeError: no model in this replay"}]
    assert session.state == "idle"


# failures


def test_client_error_becomes_error_event_and_status_error(sandbox):
    events = Events()
    client = FailingClient(APIError(401, "bad key sk-test-secret"))
    session = Session(TASK, sandbox, client, WATCH, emit=events)
    session.start()
    events.wait("status", state="error")
    error = events.wait("error")
    assert error["message"] == "APIError: chat API returned HTTP 401: bad key [redacted]"
    assert not session.running


# replay of a recorded run


def test_replay_of_spark_address_swap_gates_the_swapped_address(tmp_path):
    scenario = SCENARIOS / "address_swap"
    sandbox = tmp_path / "sandbox"
    shutil.copytree(scenario / "sandbox", sandbox)
    task = (scenario / "task.txt").read_text(encoding="utf-8").strip()
    recorded = json.loads(SPARK_SWAP.read_text(encoding="utf-8"))
    events = Events()
    session = Session(task, sandbox, replay_client(SPARK_SWAP), WATCH, emit=events, model="muse-spark-1.3", replay=True)
    session.start()
    gate = events.wait("gate")
    assert gate["decision"] == "d4"
    to = next(t for t in gate["taints"] if t["path"] == "to")
    assert to == {"action": 0, "path": "to", "tag": "IN", "source": "e7", "value": "dana.ortiz@lab-mail.example", "label": "read_file(notes/b.md)"}
    session.decide(gate["gate_id"], "approve")
    final = events.wait("final")
    events.wait("status", state="done")
    assert final["text"] == recorded["messages"][-1]["content"]
    assert _outbox(sandbox)[0]["to"] == "dana.ortiz@lab-mail.example"
    offline = transcript.to_ledger(recorded["messages"], model="muse-spark-1.3", watch=WATCH)
    assert session.ledger() == offline
