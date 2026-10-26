"""Regression tests for SPEC §5.2: decoded leaf texts, shortest number forms and numeric boundaries."""

import json

from causeway.tagger import needle_of
from causeway.transcript import to_ledger

BACKSLASH = chr(92)
WIN_PATH = "C:" + BACKSLASH + "Users" + BACKSLASH + "x" + BACKSLASH + "notes.md"


def user(text):
    return {"role": "user", "content": text}


def tool(call_id, text):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def assistant(*calls, content=None):
    return {"role": "assistant", "content": content, "tool_calls": list(calls)}


def causes_of(messages, decision_id):
    decisions = {r["id"]: r for r in to_ledger(messages) if r["kind"] == "decision"}
    return [(c["path"], c["tag"], c["source"], c["match"]) for c in decisions[decision_id]["causes"]]


def test_shortest_plain_form_of_numbers():
    assert needle_of(12.0) == "12"
    assert needle_of(-12.0) == "-12"
    assert needle_of(12.5) == "12.5"
    assert needle_of(3.5) == "3.5"
    assert needle_of(4242) == "4242"


def test_nonascii_path_from_an_earlier_decision_is_self():
    messages = [
        user("Go."),
        assistant(call("c1", "write_file", {"path": "café-notes.md", "mode": "w"})),
        tool("c1", "ok"),
        assistant(call("c2", "read_file", {"path": "café-notes.md"})),
    ]
    assert causes_of(messages, "d2") == [("path", "SELF", "d1", "exact")]


def test_windows_path_from_an_earlier_decision_is_exact_self():
    messages = [
        user("Go."),
        assistant(call("c1", "write_file", {"path": WIN_PATH, "mode": "w"})),
        tool("c1", "ok"),
        assistant(call("c2", "read_file", {"path": WIN_PATH})),
    ]
    assert causes_of(messages, "d2") == [("path", "SELF", "d1", "exact")]


def test_key_name_is_not_a_source():
    messages = [
        user("Go."),
        assistant(call("c1", "write_file", {"path": "a.md", "content": "x"})),
        tool("c1", "ok"),
        assistant(call("c2", "set_mode", {"mode": "content"})),
    ]
    assert causes_of(messages, "d2") == [("mode", "UNSOURCED", None, None)]


def test_boolean_leaves_are_not_sources():
    messages = [
        user("Go."),
        assistant(call("c1", "set_flags", {"verbose": True, "name": "alpha"})),
        tool("c1", "ok"),
        assistant(call("c2", "read_file", {"path": "true"})),
    ]
    assert causes_of(messages, "d2") == [("path", "UNSOURCED", None, None)]


def test_number_leaf_in_an_earlier_decision_text_is_self_at_a_boundary():
    messages = [
        user("Go."),
        assistant(call("c1", "list_dir", {"path": "."}), content="I will use port 42."),
        tool("c1", "ok"),
        assistant(call("c2", "read_chunk", {"offset": 42})),
    ]
    assert causes_of(messages, "d2") == [("offset", "SELF", "d1", "exact")]


def test_forty_two_does_not_match_inside_420():
    messages = [user("There are 420 items."), assistant(call("c1", "read_chunk", {"offset": 42}))]
    assert causes_of(messages, "d1") == [("offset", "UNSOURCED", None, None)]


def test_forty_two_does_not_match_inside_a_decimal():
    for text in ("port 3.42 is open", "rate 42.5 now"):
        messages = [user(text), assistant(call("c1", "read_chunk", {"offset": 42}))]
        assert causes_of(messages, "d1") == [("offset", "UNSOURCED", None, None)], text


def test_forty_two_matches_before_a_sentence_period():
    messages = [user("Use port 42."), assistant(call("c1", "read_chunk", {"offset": 42}))]
    assert causes_of(messages, "d1") == [("offset", "GOAL", "e1", "exact")]


def test_whole_float_twelve_point_zero_matches_twelve():
    messages = [user("wait 12 seconds"), assistant(call("c1", "sleep", {"seconds": 12.0}))]
    assert causes_of(messages, "d1") == [("seconds", "GOAL", "e1", "exact")]


def test_twelve_point_zero_does_not_match_inside_twelve_point_five():
    messages = [user("wait 12.5 seconds"), assistant(call("c1", "sleep", {"seconds": 12.0}))]
    assert causes_of(messages, "d1") == [("seconds", "UNSOURCED", None, None)]


def test_numbers_never_match_fuzzily():
    messages = [user("id 1234567890123 ok"), assistant(call("c1", "lookup", {"id": 123456789012}))]
    assert causes_of(messages, "d1") == [("id", "UNSOURCED", None, None)]
