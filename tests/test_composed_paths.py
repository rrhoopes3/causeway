"""Tests for composed paths (SPEC §5.4b2): the parts level, plus the real Muse Spark run."""

import json
from pathlib import Path

from causeway import tagger
from causeway.transcript import to_ledger

FIXTURES = Path(__file__).parent / "fixtures"
BACKSLASH = chr(92)
WIN_PATH = "C:" + BACKSLASH + "proj" + BACKSLASH + "notes" + BACKSLASH + "a.md"


def user(text):
    return {"role": "user", "content": text}


def tool(call_id, text):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def calls(*triples):
    return {"role": "assistant", "content": None, "tool_calls": [call(*triple) for triple in triples]}


def decisions_of(records):
    return {r["id"]: r for r in records if r["kind"] == "decision"}


def summary(causes):
    return [(c["path"], c["tag"], c["source"], c["match"]) for c in causes]


def causes_of(messages, decision_id, watch=()):
    return decisions_of(to_ledger(messages, watch=watch))[decision_id]["causes"]


def test_listed_then_read_path_is_in_by_parts():
    messages = [
        user("Summarize the project notes."),
        calls(("c1", "list_dir", {"path": "notes/"})),
        tool("c1", "a.md\nb.md"),
        calls(("c2", "read_file", {"path": "notes/a.md"})),
    ]
    assert summary(causes_of(messages, "d1")) == [("path", "GOAL", "e1", "parts")]
    assert summary(causes_of(messages, "d2")) == [("path", "IN", "e2", "parts")]


def test_path_from_an_earlier_listing_is_carry_by_parts():
    messages = [
        user("Summarize the project notes."),
        calls(("c1", "list_dir", {"path": "notes/"})),
        tool("c1", "a.md\nb.md"),
        calls(("c2", "read_file", {"path": "notes/a.md"})),
        tool("c2", "# A"),
        calls(("c3", "read_file", {"path": "notes/b.md"})),
    ]
    assert summary(causes_of(messages, "d3")) == [("path", "CARRY·1", "e2", "parts")]


def test_windows_path_built_from_goal_words_and_a_listing_takes_the_basename_source():
    messages = [
        user("Summarize the proj notes."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "a.md\nb.md"),
        calls(("c2", "read_file", {"path": WIN_PATH})),
    ]
    assert summary(causes_of(messages, "d2")) == [("path", "IN", "e2", "parts")]


def test_exact_level_wins_when_the_whole_path_appears_verbatim():
    messages = [
        user("Read notes/a.md and summarize."),
        calls(("c1", "list_dir", {"path": "notes/"})),
        tool("c1", "a.md\nb.md"),
        calls(("c2", "read_file", {"path": "notes/a.md"})),
    ]
    assert summary(causes_of(messages, "d2")) == [("path", "GOAL", "e1", "exact")]


def test_invented_basename_stays_unsourced():
    messages = [
        user("Summarize the project notes."),
        calls(("c1", "list_dir", {"path": "notes/"})),
        tool("c1", "a.md"),
        calls(("c2", "read_file", {"path": "notes/zzz-invented.md"})),
    ]
    assert summary(causes_of(messages, "d2")) == [("path", "UNSOURCED", None, None)]


def test_invented_middle_segment_stays_unsourced():
    messages = [
        user("Summarize the project notes."),
        calls(("c1", "list_dir", {"path": "notes/"})),
        tool("c1", "a.md"),
        calls(("c2", "read_file", {"path": "notes/zzz/a.md"})),
    ]
    assert summary(causes_of(messages, "d2")) == [("path", "UNSOURCED", None, None)]


def test_dot_and_short_segments_do_not_block_a_match():
    messages = [
        user("Summarize the project notes."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "a.md"),
        calls(("c2", "read_file", {"path": "../notes/a.md"}), ("c3", "read_file", {"path": "./x/a.md"})),
    ]
    assert summary(causes_of(messages, "d2")) == [
        ("path", "IN", "e2", "parts"),
        ("path", "IN", "e2", "parts"),
    ]


def test_path_atom_in_prose_is_traced_by_parts():
    body = "Saved the summary from notes/a.md into the folder today."
    messages = [
        user("Summarize the project notes."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "a.md"),
        calls(("c2", "send_email", {"body": body})),
    ]
    assert summary(causes_of(messages, "d2")) == [
        ("body", "PROSE", None, None),
        ("body~0", "IN", "e2", "parts"),
    ]


def test_invented_path_atom_in_prose_stays_unsourced():
    body = "Saved the summary from notes/zzz-invented.md into the folder today."
    messages = [
        user("Summarize the project notes."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "a.md"),
        calls(("c2", "send_email", {"body": body})),
    ]
    assert summary(causes_of(messages, "d2")) == [
        ("body", "PROSE", None, None),
        ("body~0", "UNSOURCED", None, None),
    ]


def test_missing_file_echo_does_not_source_a_retried_or_composed_path():
    """A tool error that only echoes a missing path is not evidence the path existed (§5.2, §5.4b2)."""
    messages = [
        user("Find the sensor board."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "notes/\nteam.md"),
        calls(("c2", "segment_image", {"path": "sensor_board.jpg", "prompt": "sensor board"})),
        tool("c2", "error: no such file: sensor_board.jpg"),
        calls(("c3", "read_file", {"path": "sensor_board.jpg"})),
        tool("c3", "error: no such file: sensor_board.jpg"),
        calls(("c4", "segment_image", {"path": "notes/sensor_board.jpg", "prompt": "sensor board"})),
    ]
    records = to_ledger(messages)
    decisions = decisions_of(records)
    assert summary(decisions["d2"]["causes"]) == [
        ("path", "UNSOURCED", None, None),
        ("prompt", "GOAL", "e1", "exact"),
    ]
    assert decisions["d2"]["tag"] == "UNSOURCED"
    assert summary(decisions["d3"]["causes"]) == [("path", "UNSOURCED", None, None)]
    assert decisions["d3"]["tag"] == "UNSOURCED"
    assert summary(decisions["d4"]["causes"]) == [
        ("path", "UNSOURCED", None, None),
        ("prompt", "GOAL", "e1", "exact"),
    ]
    assert decisions["d4"]["tag"] == "UNSOURCED"
    assert tagger.tag_ledger(records) == records


def test_missing_file_echo_casefold_does_not_source_the_retried_path():
    messages = [
        user("Go."),
        calls(("c1", "read_file", {"path": "Sensor_Board.JPG"})),
        tool("c1", "error: no such file: sensor_board.jpg"),
        calls(("c2", "read_file", {"path": "sensor_board.jpg"})),
    ]
    assert summary(causes_of(messages, "d1")) == [("path", "UNSOURCED", None, None)]
    assert summary(causes_of(messages, "d2")) == [("path", "UNSOURCED", None, None)]


def test_listing_still_sources_exact_and_parts_beside_a_missing_echo():
    messages = [
        user("Summarize the project notes."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "a.md\nnotes/"),
        calls(("c2", "read_file", {"path": "missing.jpg"})),
        tool("c2", "error: no such file: missing.jpg"),
        calls(("c3", "read_file", {"path": "a.md"})),
        tool("c3", "ok"),
        calls(("c4", "read_file", {"path": "notes/a.md"})),
    ]
    decisions = decisions_of(to_ledger(messages))
    assert summary(decisions["d3"]["causes"]) == [("path", "CARRY·1", "e2", "exact")]
    assert summary(decisions["d4"]["causes"]) == [("path", "CARRY·2", "e2", "parts")]


def test_file_body_and_write_result_still_source_a_path():
    """A file body and a write confirmation stay sources. Only the pure missing-file echo does not."""
    body = "error: no such file: sensor_board.jpg\nThe board photo is filed under that name."
    messages = [
        user("Take notes."),
        calls(("c1", "read_file", {"path": "notes/a.md"})),
        tool("c1", body),
        calls(("c2", "read_file", {"path": "sensor_board.jpg"})),
        tool("c2", "error: no such file: inventory.txt"),
        calls(("c3", "write_file", {"path": "inventory.txt", "content": "lab notes"})),
        tool("c3", "wrote 9 bytes to inventory.txt"),
        calls(("c4", "read_file", {"path": "inventory.txt"})),
    ]
    decisions = decisions_of(to_ledger(messages))
    assert summary(decisions["d2"]["causes"]) == [("path", "IN", "e2", "exact")]
    assert summary(decisions["d3"]["causes"]) == [
        ("path", "UNSOURCED", None, None),
        ("content", "UNSOURCED", None, None),
    ]
    assert decisions["d3"]["tag"] == "UNSOURCED"
    assert summary(decisions["d4"]["causes"]) == [("path", "IN", "e4", "exact")]


def test_real_spark_run_has_no_unsourced_leaf_and_send_is_traced():
    fixture = json.loads((FIXTURES / "real_spark_injection.json").read_text(encoding="utf-8"))
    records = to_ledger(fixture["messages"], watch=["send_email"])
    decisions = decisions_of(records)

    assert all(cause["tag"] != "UNSOURCED" for r in records if r["kind"] == "decision" for cause in r["causes"])
    assert summary(decisions["d2"]["causes"]) == [
        ("path", "IN", "e3", "exact"),
        ("path", "IN", "e3", "exact"),
    ]
    assert summary(decisions["d3"]["causes"]) == [
        ("path", "IN", "e4", "parts"),
        ("path", "IN", "e4", "parts"),
    ]
    assert decisions["d4"]["tag"] == "IN"
    assert decisions["d4"]["watch"] == [
        {"action": 0, "path": "body~0", "tag": "IN", "source": "e6"},
        {"action": 0, "path": "body~2", "tag": "IN", "source": "e7"},
        {"action": 0, "path": "to", "tag": "CARRY·1", "source": "e5"},
    ]
    assert decisions["d5"]["tag"] == "FINAL"
