"""Tests for the watch list and taint check (SPEC §5.5)."""

import json
from pathlib import Path

import pytest

from causeway import ledger, tagger
from causeway.transcript import to_ledger

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_PATHS = sorted(FIXTURES.glob("*.json"))


def load_messages(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["messages"]


def decisions_of(records):
    return {r["id"]: r for r in records if r["kind"] == "decision"}


def call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_injection_watch_entries_show_where_each_send_came_from():
    records = to_ledger(load_messages("injection.json"), watch=["send_email"])
    decisions = decisions_of(records)

    assert decisions["d4"]["watch"] == [
        {"action": 0, "path": "to", "tag": "CARRY·1", "source": "e4"},
        {"action": 0, "path": "body", "tag": "IN", "source": "e5"},
    ]
    assert decisions["d5"]["watch"] == [
        {"action": 0, "path": "to", "tag": "CARRY·1", "source": "e5"},
        {"action": 0, "path": "body", "tag": "UNSOURCED", "source": None},
    ]


def test_decisions_without_a_watched_action_get_no_watch_key():
    decisions = decisions_of(to_ledger(load_messages("injection.json"), watch=["send_email"]))
    for decision_id in ("d1", "d2", "d3", "d6"):
        assert "watch" not in decisions[decision_id]


def test_lit_only_watched_action_is_clean():
    decisions = decisions_of(to_ledger(load_messages("injection.json"), watch=["list_dir"]))
    assert decisions["d1"]["watch"] == []
    assert "watch" not in decisions["d4"]


def test_watch_on_a_tool_never_called_changes_nothing():
    messages = load_messages("injection.json")
    assert to_ledger(messages, watch=["delete_file"]) == to_ledger(messages)
    assert ledger.dumps(to_ledger(messages, watch=["delete_file"])) == ledger.dumps(to_ledger(messages))


@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_empty_watch_keeps_every_ledger_byte_identical(path):
    messages = json.loads(path.read_text(encoding="utf-8"))["messages"]
    plain = ledger.dumps(to_ledger(messages))
    assert ledger.dumps(to_ledger(messages, watch=())) == plain
    assert ledger.dumps(to_ledger(messages, watch=["never_called"])) == plain
    assert not any("watch" in record for record in to_ledger(messages, watch=()))


def test_goal_sourced_argument_is_clean_so_watch_is_empty():
    messages = [
        {"role": "user", "content": "Email dana@lab.example the summary."},
        {"role": "assistant", "content": None, "tool_calls": [call("c1", "send_email", {"to": "dana@lab.example"})]},
    ]
    decision = decisions_of(to_ledger(messages, watch=["send_email"]))["d1"]
    assert decision["causes"] == [
        {"action": 0, "path": "to", "value": "dana@lab.example", "tag": "GOAL", "source": "e1", "match": "exact"},
    ]
    assert decision["watch"] == []


def test_only_watched_actions_of_a_parallel_decision_are_checked():
    messages = [
        {"role": "user", "content": "Go."},
        {"role": "assistant", "content": None, "tool_calls": [call("c1", "list_dir", {"path": "."})]},
        {"role": "tool", "tool_call_id": "c1", "content": "dana@lab.example\nteam.md"},
        {"role": "assistant", "content": None, "tool_calls": [
            call("c2", "read_file", {"path": "team.md"}),
            call("c3", "send_email", {"to": "dana@lab.example"}),
        ]},
    ]
    decision = decisions_of(to_ledger(messages, watch=["send_email"]))["d2"]
    assert decision["watch"] == [{"action": 1, "path": "to", "tag": "IN", "source": "e2"}]


def test_self_taint_has_the_decision_as_its_source():
    messages = [
        {"role": "user", "content": "Go."},
        {"role": "assistant", "content": "Plan: send the report to ops-alpha@corp.example",
         "tool_calls": [call("c1", "list_dir", {"path": "."})]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": None, "tool_calls": [call("c2", "send_email", {"to": "ops-alpha@corp.example"})]},
    ]
    decision = decisions_of(to_ledger(messages, watch=["send_email"]))["d2"]
    assert decision["watch"] == [{"action": 0, "path": "to", "tag": "SELF", "source": "d1"}]


def test_watch_accepts_any_iterable_of_names():
    messages = load_messages("injection.json")
    expected = to_ledger(messages, watch=["send_email"])
    assert to_ledger(messages, watch=("send_email",)) == expected
    assert to_ledger(messages, watch={"send_email"}) == expected
    assert to_ledger(messages, watch=(name for name in ["send_email"])) == expected


def test_retagging_drops_a_stale_watch_key_and_same_watch_is_stable():
    messages = load_messages("injection.json")
    watched = to_ledger(messages, watch=["send_email"])
    assert tagger.tag_ledger(watched) == to_ledger(messages)
    assert tagger.tag_ledger(watched, watch=["send_email"]) == watched
