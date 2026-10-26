"""Tests for causeway.transcript and the fixture transcripts (SPEC §4, §10)."""

import json
from pathlib import Path

import pytest

from causeway import ledger, tagger
from causeway.transcript import parse_args, to_ledger

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_PATHS = sorted(FIXTURES.glob("*.json"))


def load_fixture(path):
    return json.loads(path.read_text(encoding="utf-8"))


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def test_header_ids_steps_roles_and_names():
    messages = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "List the notes."},
        {"role": "assistant", "content": None, "tool_calls": [call("call_1", "list_dir", "{\"path\": \"notes\"}")]},
        {"role": "tool", "tool_call_id": "call_1", "content": "a.md\n"},
        {"role": "assistant", "content": "Done."},
    ]
    records = to_ledger(messages, run_id="r-7", model="m-1")

    assert records[0] == {"kind": "header", "run_id": "r-7", "model": "m-1", "source": "transcript", "causeway": "0.1.0"}
    events = [r for r in records if r["kind"] == "event"]
    assert [(e["id"], e["step"], e["role"]) for e in events] == [("e1", 1, "system"), ("e2", 1, "user"), ("e3", 2, "tool")]
    assert events[2]["name"] == "list_dir"
    assert events[2]["tool_call_id"] == "call_1"

    d1, d2 = [r for r in records if r["kind"] == "decision"]
    assert (d1["id"], d1["step"], d1["final"], d1["text"]) == ("d1", 1, False, None)
    assert d1["actions"] == [{"call_id": "call_1", "name": "list_dir", "args": {"path": "notes"}}]
    assert d1["tag"] == "GOAL"
    assert (d2["id"], d2["step"], d2["final"], d2["text"], d2["tag"]) == ("d2", 2, True, "Done.", "FINAL")
    assert d1["rivals"] is None and d1["echo_of"] is None


def test_content_parts_are_joined_and_non_text_parts_ignored():
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "Look"},
            {"type": "image_url", "image_url": {"url": "https://example.invalid/x.png"}},
            {"type": "text", "text": "at plan-v2.md"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "Reading"}, {"type": "text", "text": "now"}],
         "tool_calls": [call("c1", "read_file", "{\"path\": \"plan-v2.md\"}")]},
    ]
    records = to_ledger(messages)
    assert records[1]["text"] == "Look\nat plan-v2.md"
    decision = records[2]
    assert decision["text"] == "Reading\nnow"
    assert decision["tag"] == "GOAL"
    assert decision["causes"][0]["source"] == "e1"


def test_malformed_arguments_are_kept_as_raw_text():
    raw = "{\"path\": \"x.md\""
    messages = [
        {"role": "user", "content": "Go."},
        {"role": "assistant", "content": None, "tool_calls": [call("c1", "write_file", raw)]},
    ]
    decision = to_ledger(messages)[2]
    assert decision["actions"][0]["args"] == {"_raw": raw}
    assert decision["causes"][0]["path"] == "_raw"
    assert decision["tag"] == "UNSOURCED"


def test_parse_args_forms():
    assert parse_args("{\"a\": 1}") == {"a": 1}
    assert parse_args("") == {}
    assert parse_args("   ") == {}
    assert parse_args(None) == {}
    assert parse_args("[1, 2]") == {"_raw": "[1, 2]"}
    assert parse_args({"a": "b"}) == {"a": "b"}


def test_parallel_calls_form_one_decision_with_one_cause_per_action():
    messages = [
        {"role": "user", "content": "Compare drafts in drafts."},
        {"role": "assistant", "content": None, "tool_calls": [call("c1", "list_dir", "{\"path\": \"drafts\"}")]},
        {"role": "tool", "tool_call_id": "c1", "content": "alpha-notes.md\nbeta-notes.md"},
        {"role": "assistant", "content": None, "tool_calls": [
            call("c2", "read_file", "{\"path\": \"alpha-notes.md\"}"),
            call("c3", "read_file", "{\"path\": \"beta-notes.md\"}"),
        ]},
        {"role": "tool", "tool_call_id": "c2", "content": "# Alpha"},
        {"role": "tool", "tool_call_id": "c3", "content": "# Beta"},
    ]
    decision = to_ledger(messages)[4]
    assert decision["id"] == "d2"
    assert len(decision["actions"]) == 2
    assert [(c["action"], c["path"], c["tag"], c["source"]) for c in decision["causes"]] == [
        (0, "path", "IN", "e2"),
        (1, "path", "IN", "e2"),
    ]
    assert decision["tag"] == "IN"


def test_unknown_role_raises():
    with pytest.raises(ValueError):
        to_ledger([{"role": "function", "content": "x"}])


def test_output_is_byte_identical_across_runs(tmp_path):
    messages = load_fixture(FIXTURE_PATHS[0])["messages"]
    first = ledger.dumps(to_ledger(messages))
    second = ledger.dumps(to_ledger(messages))
    assert first == second
    assert first.endswith("\n") and "\r" not in first

    ledger.write(tmp_path / "a.jsonl", to_ledger(messages))
    ledger.write(tmp_path / "b.jsonl", to_ledger(messages))
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()


def test_write_and_read_round_trip_keeps_unicode_and_line_separators(tmp_path):
    messages = [
        {"role": "user", "content": "Open café.md then stop."},
        {"role": "assistant", "content": None, "tool_calls": [call("c1", "read_file", "{\"path\": \"café.md\"}")]},
    ]
    records = to_ledger(messages)
    path = tmp_path / "run.jsonl"
    ledger.write(path, records)
    assert ledger.read(path) == records
    assert b"\r\n" not in path.read_bytes()


def test_tagging_a_tagged_ledger_changes_nothing():
    messages = load_fixture(FIXTURE_PATHS[0])["messages"]
    records = to_ledger(messages)
    assert tagger.tag_ledger(records) == records


@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_fixture_expectations(path):
    fixture = load_fixture(path)
    decisions = {r["id"]: r for r in to_ledger(fixture["messages"]) if r["kind"] == "decision"}
    for key, expected in fixture["expect"].items():
        if key == "echo":
            for decision_id, target in expected.items():
                assert decisions[decision_id]["echo_of"] == target
        else:
            assert decisions[key]["tag"] == expected


def test_fixtures_cover_every_required_case():
    tags, matches = set(), set()
    echoed = multi_action = False
    for path in FIXTURE_PATHS:
        for record in to_ledger(load_fixture(path)["messages"]):
            if record["kind"] != "decision":
                continue
            tags.add(record["tag"])
            echoed = echoed or record["echo_of"] is not None
            multi_action = multi_action or len(record["actions"]) > 1
            for cause in record["causes"]:
                tags.add(cause["tag"])
                matches.add(cause["match"])
    assert {"IN", "CARRY·2", "GOAL", "SELF", "UNSOURCED", "LIT", "FINAL"} <= tags
    assert {"exact", "casefold", "fuzzy"} <= matches
    assert echoed and multi_action
