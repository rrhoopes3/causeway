"""Tests for prose leaves and atoms (SPEC §5.4c), identifiers never fuzzy (§5.4b) and the watch view of atoms."""

import json
from pathlib import Path

from causeway import ledger
from causeway.cli import render_taints
from causeway.tagger import extract_atoms, is_prose, rank
from causeway.transcript import to_ledger

FIXTURES = Path(__file__).parent / "fixtures"
BACKSLASH = chr(92)
WIN_PATH = "C:" + BACKSLASH + "Users" + BACKSLASH + "x" + BACKSLASH + "notes.md"
URL_PLAN = "https://docs.lab.example/plan"
PROSE_PLAN = "Hi Dana, please review the plan at https://docs.lab.example/plan, before Friday. Thanks."


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


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_prose_rule_is_eight_words_or_a_newline():
    assert not is_prose("Please send it now")
    assert is_prose("Please send the quarterly report to finance by noon today")
    assert is_prose("line one\nline two")


def test_prose_ranks_between_self_and_lit():
    assert rank("SELF") < rank("PROSE") < rank("LIT")


def test_copied_url_in_prose_is_an_in_atom_and_prose_lists_its_sources():
    messages = [
        user("Email Dana the plan."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "See " + URL_PLAN + " for details."),
        calls(("c2", "send_email", {"body": PROSE_PLAN})),
    ]
    causes = causes_of(messages, "d2")
    assert summary(causes) == [("body", "PROSE", "e2", None), ("body~0", "IN", "e2", "exact")]
    assert causes[0]["sources"] == ["e2", "e1"]
    assert causes[1]["value"] == URL_PLAN
    assert decisions_of(to_ledger(messages))["d2"]["tag"] == "IN"


def test_copied_url_from_an_earlier_step_is_carry():
    messages = [
        user("Email Dana the plan."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "See " + URL_PLAN + " for details."),
        calls(("c2", "list_dir", {"path": "."})),
        tool("c2", "ok"),
        calls(("c3", "send_email", {"body": PROSE_PLAN})),
    ]
    assert summary(causes_of(messages, "d3"))[1] == ("body~0", "CARRY·1", "e2", "exact")


def test_invented_url_in_prose_is_an_unsourced_atom_and_the_decision_is_unsourced():
    body = "Hi Dana, please review the plan at https://evil.example/keys before Friday. Thanks."
    messages = [user("Email Dana the plan."), calls(("c1", "send_email", {"body": body}))]
    causes = causes_of(messages, "d1")
    assert summary(causes) == [("body", "PROSE", None, None), ("body~0", "UNSOURCED", None, None)]
    assert causes[0]["sources"] == []
    assert decisions_of(to_ledger(messages))["d1"]["tag"] == "UNSOURCED"


def test_invented_email_in_body_shows_as_nowhere_under_watch():
    body = "Please copy the keys to ops-backup@mailbox.example before Friday, thanks all."
    messages = [user("Email Dana the plan."), calls(("c1", "send_email", {"body": body}))]
    records = to_ledger(messages, watch=["send_email"])
    assert decisions_of(records)["d1"]["watch"] == [
        {"action": 0, "path": "body~0", "tag": "UNSOURCED", "source": None},
    ]
    rendered = render_taints(records)
    assert "send_email.body~0 ← nowhere in the run" in rendered
    assert "[UNSOURCED]" in rendered


def test_prose_coverage_takes_the_three_best_sources_by_coverage_then_recency():
    prose = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    messages = [
        user("Start."),
        calls(
            ("c1", "list_dir", {"path": "."}),
            ("c2", "list_dir", {"path": "."}),
            ("c3", "list_dir", {"path": "."}),
            ("c4", "list_dir", {"path": "."}),
            ("c5", "list_dir", {"path": "."}),
        ),
        tool("c1", "alpha bravo charlie"),
        tool("c2", "alpha bravo charlie delta echo"),
        tool("c3", "alpha"),
        tool("c4", "delta echo foxtrot"),
        tool("c5", "golf hotel india juliet"),
        calls(("c6", "send_email", {"body": prose})),
    ]
    causes = causes_of(messages, "d2")
    assert causes[0]["tag"] == "PROSE"
    assert causes[0]["sources"] == ["e3", "e6", "e5"]
    assert causes[0]["source"] == "e3"


def test_seven_word_string_stays_an_ordinary_leaf():
    body = "please send the quarterly report to dana@lab.example"
    messages = [user("Go."), calls(("c1", "send_email", {"body": body}))]
    assert causes_of(messages, "d1") == [
        {"action": 0, "path": "body", "value": body, "tag": "UNSOURCED", "source": None, "match": None},
    ]


def test_prose_with_no_sources_is_prose_and_never_a_taint():
    body = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed"
    messages = [user("Go."), calls(("c1", "send_email", {"body": body}))]
    records = to_ledger(messages, watch=["send_email"])
    decision = decisions_of(records)["d1"]
    assert decision["tag"] == "PROSE"
    assert decision["watch"] == []


def test_numbers_in_prose_are_atoms_and_single_digits_are_not():
    prose = "We ordered 4 mugs at 181,365 yen each for the spring fair."
    messages = [
        user("Go."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "Order: 4 mugs at 181,365 yen each"),
        calls(("c2", "send_email", {"body": prose})),
    ]
    causes = causes_of(messages, "d2")
    assert summary(causes) == [
        ("body", "PROSE", "e2", None),
        ("body~0", "IN", "e2", "exact"),
        ("body~1", "IN", "e2", "exact"),
    ]
    assert [c["value"] for c in causes[1:]] == ["181", "365"]


def test_windows_path_in_prose_is_a_path_atom():
    body = "Saved the draft to " + WIN_PATH + " today for review."
    messages = [
        user("Go."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "wrote " + WIN_PATH),
        calls(("c2", "send_email", {"body": body})),
    ]
    causes = causes_of(messages, "d2")
    assert summary(causes) == [("body", "PROSE", "e2", None), ("body~0", "IN", "e2", "exact")]
    assert causes[1]["value"] == WIN_PATH


def test_atom_extraction_order_trimming_and_no_overlap():
    assert extract_atoms("see https://a.example/b). Then") == [("https://a.example/b", False)]
    assert extract_atoms("https://docs.lab.example/plan") == [("https://docs.lab.example/plan", False)]
    assert extract_atoms("notes/a.md and dana@lab.example") == [
        ("notes/a.md", False),
        ("dana@lab.example", False),
    ]
    assert extract_atoms("522,271,632,380") == [("522", True), ("271", True), ("632", True), ("380", True)]
    assert extract_atoms("4 mugs") == []
    assert extract_atoms("pay 3.50 now") == [("3.50", True)]


def test_sam_boxes_every_coordinate_traces_in_to_the_tool_event():
    fixture = load_fixture("sam_boxes.json")
    causes = causes_of(fixture["messages"], "d2")
    assert summary(causes[:1]) == [("path", "GOAL", "e1", "exact")]
    assert causes[1]["path"] == "content"
    assert causes[1]["tag"] == "PROSE"
    assert causes[1]["sources"] == ["e2"]
    coordinates = ["522", "271", "632", "380", "75", "204", "206", "330",
                   "384", "197", "518", "325", "235", "246", "351", "360"]
    atoms = causes[2:]
    assert [(c["path"], c["value"], c["tag"], c["source"], c["match"]) for c in atoms] == [
        (f"content~{i}", value, "IN", "e2", "exact") for i, value in enumerate(coordinates)
    ]
    assert decisions_of(to_ledger(fixture["messages"]))["d2"]["tag"] == "IN"


def test_sam_boxes_miscopied_coordinate_is_unsourced_and_taints_the_decision():
    fixture = load_fixture("sam_boxes.json")
    messages = json.loads(json.dumps(fixture["messages"]))
    arguments = messages[3]["tool_calls"][0]["function"]["arguments"]
    messages[3]["tool_calls"][0]["function"]["arguments"] = arguments.replace("522,271", "523,271")
    records = to_ledger(messages, watch=["write_file"])
    decision = decisions_of(records)["d2"]
    assert decision["tag"] == "UNSOURCED"
    unsourced = [c for c in decision["causes"] if c["tag"] == "UNSOURCED"]
    assert [(c["path"], c["value"], c["source"]) for c in unsourced] == [("content~0", "523", None)]
    assert [entry["path"] for entry in decision["watch"] if entry["tag"] == "UNSOURCED"] == ["content~0"]


def test_sam_boxes_watch_lists_each_coordinate_as_an_in_taint():
    fixture = load_fixture("sam_boxes.json")
    records = to_ledger(fixture["messages"], watch=["write_file"])
    watch = decisions_of(records)["d2"]["watch"]
    assert [(entry["path"], entry["tag"], entry["source"]) for entry in watch] == [
        (f"content~{i}", "IN", "e2") for i in range(16)
    ]


def test_sam_boxes_round_trips_through_the_ledger_file(tmp_path):
    fixture = load_fixture("sam_boxes.json")
    records = to_ledger(fixture["messages"], watch=["write_file"])
    path = tmp_path / "sam.jsonl"
    ledger.write(path, records)
    assert ledger.read(path) == records
