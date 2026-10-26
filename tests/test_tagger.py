"""Unit tests for causeway.tagger: leaves, match levels, priorities and echoes (SPEC §5)."""

import json

import pytest

from causeway import tagger
from causeway.transcript import to_ledger


def user(text):
    return {"role": "user", "content": text}


def tool(call_id, text):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def calls(*triples):
    """Assistant message with one tool call per (call_id, name, args) triple."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for call_id, name, args in triples
        ],
    }


def decisions_of(messages):
    return {record["id"]: record for record in to_ledger(messages) if record["kind"] == "decision"}


def cause_summary(decision):
    return [(c["path"], c["tag"], c["source"], c["match"]) for c in decision["causes"]]


def test_flatten_uses_dotted_paths_with_list_indexes_and_skips_nulls():
    args = {"files": [{"name": "a.md"}, {"name": None}], "meta": {"deep": {"x": "yes"}}, "n": None, "flag": True}
    assert tagger.flatten(args) == [("files.0.name", "a.md"), ("meta.deep.x", "yes"), ("flag", True)]


@pytest.mark.parametrize("value", [True, False, 9, -9.5, "ab", "  a ", ""])
def test_trivial_leaves(value):
    assert tagger.is_trivial(value)


@pytest.mark.parametrize("value", [10, -12, 25.0, "abc", "notes.md"])
def test_non_trivial_leaves(value):
    assert not tagger.is_trivial(value)


def test_number_needles_are_their_json_text():
    assert tagger.needle_of(4242) == "4242"
    assert tagger.needle_of(12.5) == "12.5"
    assert tagger.needle_of("plan.md") == "plan.md"


def test_number_leaves_match_their_written_text_and_trivial_numbers_are_not_traced():
    messages = [
        user("Read chunk 4242 at ratio 12.5."),
        calls(("c1", "read_chunk", {"chunk": 4242, "ratio": 12.5, "depth": 3})),
    ]
    decision = decisions_of(messages)["d1"]
    assert cause_summary(decision) == [("chunk", "GOAL", "e1", "exact"), ("ratio", "GOAL", "e1", "exact")]
    assert decision["tag"] == "GOAL"


def test_in_beats_goal_for_the_same_value():
    messages = [
        user("Check the plan-v2.md file."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "plan-v2.md\nreadme.md"),
        calls(("c2", "read_file", {"path": "plan-v2.md"})),
    ]
    decision = decisions_of(messages)["d2"]
    assert cause_summary(decision) == [("path", "IN", "e2", "exact")]
    assert decision["tag"] == "IN"


def test_goal_beats_self_for_the_same_value():
    messages = [
        user("Check the plan-v2.md file."),
        calls(("c1", "read_file", {"path": "plan-v2.md"})),
        tool("c1", "ok"),
        calls(("c2", "read_file", {"path": "plan-v2.md"})),
    ]
    decision = decisions_of(messages)["d2"]
    assert cause_summary(decision) == [("path", "GOAL", "e1", "exact")]


def test_self_when_only_an_earlier_decision_text_has_the_value():
    messages = [
        user("Start."),
        {
            "role": "assistant",
            "content": "I will use summary-final.md.",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{\"path\": \".\"}"}}],
        },
        tool("c1", "listing"),
        calls(("c2", "read_file", {"path": "summary-final.md"})),
    ]
    decision = decisions_of(messages)["d2"]
    assert cause_summary(decision) == [("path", "SELF", "d1", "exact")]
    assert decision["tag"] == "SELF"


def test_smallest_carry_wins_among_carries():
    messages = [
        user("Look around."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "gamma-report.md"),
        calls(("c2", "list_dir", {"path": "."})),
        tool("c2", "gamma-report.md"),
        calls(("c3", "list_dir", {"path": "."})),
        tool("c3", "nothing new"),
        calls(("c4", "read_file", {"path": "gamma-report.md"})),
    ]
    decision = decisions_of(messages)["d4"]
    assert cause_summary(decision) == [("path", "CARRY·1", "e3", "exact")]
    assert decision["tag"] == "CARRY·1"


def test_exact_level_stops_the_search_before_casefold():
    messages = [
        user("Open the Config.yaml file."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "config.yaml"),
        calls(("c2", "read_file", {"path": "Config.yaml"})),
    ]
    decision = decisions_of(messages)["d2"]
    assert cause_summary(decision) == [("path", "GOAL", "e1", "exact")]


def test_casefold_match_is_recorded():
    messages = [
        user("Find the notes."),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "README.md\nParser Notes.md"),
        calls(("c2", "read_file", {"path": "parser notes.md"})),
    ]
    decision = decisions_of(messages)["d2"]
    assert cause_summary(decision) == [("path", "IN", "e2", "casefold")]


def test_fuzzy_match_needs_a_needle_of_twelve_characters():
    long_needle = [user("Check alpha and bravo."), calls(("c1", "read_file", {"path": "alpha bravo!"}))]
    assert cause_summary(decisions_of(long_needle)["d1"]) == [("path", "GOAL", "e1", "fuzzy")]

    short_needle = [user("Check alpha and bravo."), calls(("c1", "read_file", {"path": "alpha bravo"}))]
    assert cause_summary(decisions_of(short_needle)["d1"]) == [("path", "UNSOURCED", None, None)]


def test_fuzzy_match_needs_whitespace_so_identifiers_never_match_fuzzily():
    identifier = [user("Check alpha and bravo."), calls(("c1", "read_file", {"path": "alpha-bravo!"}))]
    assert cause_summary(decisions_of(identifier)["d1"]) == [("path", "UNSOURCED", None, None)]

    changelog = [user("Update the changelog."), calls(("c1", "read_file", {"path": "CHANGELOG.md"}))]
    assert cause_summary(decisions_of(changelog)["d1"]) == [("path", "UNSOURCED", None, None)]


def test_fuzzy_match_needs_sixty_percent_of_tokens():
    two_of_three = [
        user("parser and tokenizer notes"),
        calls(("c1", "search_notes", {"query": "parser tokenizer errors"})),
    ]
    assert cause_summary(decisions_of(two_of_three)["d1"]) == [("query", "GOAL", "e1", "fuzzy")]

    one_of_three = [
        user("parser only here"),
        calls(("c1", "search_notes", {"query": "parser tokenizer errors"})),
    ]
    assert cause_summary(decisions_of(one_of_three)["d1"]) == [("query", "UNSOURCED", None, None)]


def test_unsourced_leaf_has_no_source_or_match():
    messages = [user("Hi there."), calls(("c1", "read_file", {"path": "docs/architecture.md"}))]
    assert cause_summary(decisions_of(messages)["d1"]) == [("path", "UNSOURCED", None, None)]


def test_unsourced_beats_in_at_decision_level():
    messages = [
        user("Open plan-v2.md"),
        calls(("c1", "list_dir", {"path": "."})),
        tool("c1", "plan-v2.md"),
        calls(("c2", "read_file", {"path": "plan-v2.md", "other": "ghost-file.txt"})),
    ]
    decision = decisions_of(messages)["d2"]
    assert [c["tag"] for c in decision["causes"]] == ["IN", "UNSOURCED"]
    assert decision["tag"] == "UNSOURCED"


def test_noargs_lit_only_and_final_decision_tags():
    messages = [
        user("Go."),
        calls(("c1", "ping", {})),
        tool("c1", "pong"),
        calls(("c2", "list_dir", {"path": ".", "depth": 2})),
        tool("c2", "x"),
        {"role": "assistant", "content": "done"},
    ]
    decisions = decisions_of(messages)
    assert decisions["d1"]["tag"] == "NOARGS"
    assert decisions["d2"]["tag"] == "LIT"
    assert decisions["d2"]["causes"] == []
    assert decisions["d3"]["tag"] == "FINAL"
    assert decisions["d3"]["causes"] == []


def test_echo_ignores_action_order_and_points_to_the_most_recent_match():
    messages = [
        user("Check queue and notes.md."),
        calls(("c1", "list_dir", {"path": "queue"}), ("c2", "read_file", {"path": "notes.md"})),
        tool("c1", "x"),
        tool("c2", "y"),
        calls(("c3", "read_file", {"path": "notes.md"}), ("c4", "list_dir", {"path": "queue"})),
        tool("c3", "y"),
        tool("c4", "x"),
        calls(("c5", "list_dir", {"path": "queue"}), ("c6", "read_file", {"path": "notes.md"})),
    ]
    decisions = decisions_of(messages)
    assert decisions["d1"]["echo_of"] is None
    assert decisions["d2"]["echo_of"] == "d1"
    assert decisions["d3"]["echo_of"] == "d2"


def test_final_decisions_never_echo():
    messages = [
        user("Go."),
        {"role": "assistant", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]
    decisions = decisions_of(messages)
    assert decisions["d1"]["echo_of"] is None
    assert decisions["d2"]["echo_of"] is None
