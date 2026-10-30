import json

import pytest

from causeway.client import FakeClient
from causeway.rivals import canonical_json, is_key_leaf, key_leaves, normalize, rivals, signature, splits

TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
MESSAGES = [{"role": "user", "content": "Read the plan."}]
READ = "read_file({\"path\":\"plan-v2.md\"})"
LIST = "list_dir({})"
ORTIZ = "dana.ortiz@lab-mail.example"
DANA = "dana@lab.example"
RECORD_KEYS = {"k", "votes", "chosen", "margin", "near_miss", "upset", "splits"}


def _call(name, arguments):
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return {"id": "call_x", "type": "function", "function": {"name": name, "arguments": arguments}}


def _calls(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def _read_message(path="plan-v2.md"):
    return _calls(_call("read_file", {"path": path}))


def _list_message():
    return _calls(_call("list_dir", {"path": "."}))


def _final_message():
    return {"role": "assistant", "content": "All done."}


def _email(to, subject, body):
    return _calls(_call("send_email", {"to": to, "subject": subject, "body": body}))


def _response(message):
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


def _samples(*messages):
    return FakeClient([_response(m) for m in messages])


# action key (SPEC 6.2)


def test_signature_of_final_message():
    assert signature(_final_message()) == "final"
    assert signature({"role": "assistant", "content": "x", "tool_calls": []}) == "final"


def test_signature_of_single_tool_call_is_canonical():
    assert signature(_read_message()) == READ


def test_signature_ignores_argument_key_order_and_whitespace():
    a = _calls(_call("read_file", '{"b": 12, "a": {"y": "two.md", "x": 30}}'))
    b = _calls(_call("read_file", '{"a":{"x":30,"y":"two.md"},"b":12}'))
    assert signature(a) == signature(b) == 'read_file({"a.x":30,"a.y":"two.md","b":12})'


def test_signature_sorts_multiple_calls_regardless_of_order():
    ab = _calls(_call("read_file", {"path": "notes.md"}), _call("list_dir", {"path": "notes"}))
    ba = _calls(_call("list_dir", {"path": "notes"}), _call("read_file", {"path": "notes.md"}))
    expected = 'list_dir({"path":"notes"}) + read_file({"path":"notes.md"})'
    assert signature(ab) == signature(ba) == expected


def test_signature_of_arguments_that_are_not_json_is_the_raw_string():
    assert signature(_calls(_call("read_file", "not json"))) == "read_file(not json)"
    assert signature(_calls(_call("read_file", '{"path": "a.md"'))) == 'read_file({"path": "a.md")'


def test_call_with_no_key_leaves_is_name_of_empty_object():
    assert signature(_list_message()) == LIST
    assert signature(_calls(_call("list_dir", {}))) == LIST


def test_signature_keeps_only_key_leaves():
    message = _email(ORTIZ, "Project Lantern - summary of notes", "Hi Dana,\n\nHere is the summary.")
    assert signature(message) == 'send_email({"to":"dana.ortiz@lab-mail.example"})'


def test_canonical_json_matches_spec_format():
    assert canonical_json({"b": "café", "a": [1, True]}) == '{"a":[1,true],"b":"café"}'


# key leaves (SPEC 6.1)


@pytest.mark.parametrize("value", ["two words", "line\nbreak", "tab\there", " notes.md", "notes.md "])
def test_strings_with_whitespace_are_not_key_leaves(value):
    assert not is_key_leaf(value)


@pytest.mark.parametrize("value", [True, False, 0, 3, -9.5, 9.99, "ab", " x "])
def test_trivial_values_are_not_key_leaves(value):
    assert not is_key_leaf(value)


@pytest.mark.parametrize("value", [10, -12, 12.5, 1000000, "abc", "notes.md", "dana@lab.example", "https://x.example/a"])
def test_numbers_of_ten_or_more_and_plain_strings_are_key_leaves(value):
    assert is_key_leaf(value)


def test_key_leaves_flatten_nested_args_and_skip_null():
    arguments = {
        "path": "notes/",
        "count": 42,
        "small": 3,
        "overwrite": True,
        "body": "hi there",
        "missing": None,
        "short": "ab",
        "files": ["a.md", "b c.md", {"name": "c.md"}],
    }
    assert key_leaves(json.dumps(arguments)) == {"path": "notes", "count": 42, "files.0": "a.md", "files.2.name": "c.md"}


def test_key_leaves_of_arguments_that_are_not_json_is_none():
    assert key_leaves("not json") is None
    assert key_leaves({"path": "a.md"}) == {"path": "a.md"}


@pytest.mark.parametrize("arguments", ['["a.md", "b.md"]', '"notes.md"', "1234"])
def test_json_that_is_not_an_object_is_keyed_by_the_raw_string_like_the_ledger(arguments):
    assert key_leaves(arguments) is None
    assert signature(_calls(_call("read_file", arguments))) == f"read_file({arguments})"
    other = _calls(_call("read_file", '["a.md", "c.md"]'))
    assert splits([_calls(_call("read_file", arguments)), other]) == []


@pytest.mark.parametrize("arguments", ["", "  ", None, "{}"])
def test_missing_or_blank_arguments_are_no_arguments_like_the_ledger(arguments):
    call = {"id": "call_x", "type": "function", "function": {"name": "list_dir", "arguments": arguments}}
    assert key_leaves(arguments) == {}
    assert signature(_calls(call)) == LIST


def test_blank_and_empty_object_arguments_vote_together():
    blank = _calls({"id": "c", "type": "function", "function": {"name": "list_dir", "arguments": ""}})
    nothing = _calls({"id": "c", "type": "function", "function": {"name": "list_dir"}})
    result = rivals(MESSAGES, TOOLS, blank, 2, _samples(_calls(_call("list_dir", {})), nothing))
    assert result["votes"] == {LIST: 3}
    assert result["upset"] is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("notes/", "notes"),
        ("notes//", "notes"),
        ("./a/b", "a/b"),
        ("a\\b\\", "a/b"),
        (".\\notes\\a.md", "notes/a.md"),
        (".", "."),
        ("/", "/"),
        ("///", "/"),
        ("./", "."),
        ("./.", "."),
        (".//notes", "notes"),
        ("././notes", "notes"),
        ("/notes/", "/notes"),
        ("https://x.example/a/", "https://x.example/a"),
        ("notes", "notes"),
        ("a.md", "a.md"),
    ],
)
def test_path_normalization(raw, expected):
    assert normalize(raw) == expected


def test_normalization_leaves_non_strings_alone():
    assert normalize(42) == 42
    assert normalize(12.5) == 12.5


def test_whole_floats_compare_as_ints():
    assert normalize(12.0) == 12
    assert isinstance(normalize(12.0), int)
    responses = [_calls(_call("pay", {"amount": amount})) for amount in [12, 12.0, 12, 12.0, 12]]
    assert {signature(response) for response in responses} == {'pay({"amount":12})'}
    assert splits(responses) == []


def test_paths_that_differ_only_by_a_trailing_slash_vote_together():
    a = _calls(_call("list_dir", {"path": "notes/"}))
    b = _calls(_call("list_dir", {"path": "notes"}))
    c = _calls(_call("list_dir", {"path": "./notes"}))
    assert signature(a) == signature(b) == signature(c) == 'list_dir({"path":"notes"})'


# votes and margin (SPEC 6.2)


def test_margin_and_near_miss_for_three_two_split():
    fake = _samples(_read_message(), _final_message(), _read_message(), _final_message())
    result = rivals(MESSAGES, TOOLS, _read_message(), 4, fake)
    assert result == {
        "k": 4,
        "votes": {READ: 3, "final": 2},
        "chosen": READ,
        "margin": 0.2,
        "near_miss": True,
        "upset": False,
        "splits": [{
            "action": 0,
            "path": "path",
            "chosen": "plan-v2.md",
            "votes": [{"value": "plan-v2.md", "count": 3}, {"value": None, "count": 2}],
            "margin": 0.2,
            "near_miss": True,
            "upset": False,
        }],
    }


def test_record_has_exactly_the_spec_keys():
    result = rivals(MESSAGES, TOOLS, _read_message(), 1, _samples(_read_message()))
    assert set(result) == RECORD_KEYS
    assert result["splits"] == []


def test_outvoted_choice_is_an_upset_not_a_near_miss():
    fake = _samples(*[_final_message()] * 4)
    result = rivals(MESSAGES, TOOLS, _read_message(), 4, fake)
    assert result["margin"] == -0.6
    assert result["upset"] is True
    assert result["near_miss"] is False


def test_reasoning_effort_is_passed_to_every_sample():
    fake = _samples(*[_read_message()] * 2)
    rivals(MESSAGES, TOOLS, _read_message(), 2, fake, reasoning_effort="low")
    assert [call.get("reasoning_effort") for call in fake.calls] == ["low", "low"]


def test_each_sample_uses_same_messages_and_tools():
    fake = _samples(_read_message(), _final_message(), _read_message(), _final_message())
    rivals(MESSAGES, TOOLS, _read_message(), 4, fake)
    assert len(fake.calls) == 4
    for call in fake.calls:
        assert call["messages"] == MESSAGES
        assert call["tools"] == TOOLS


def test_unanimous_is_not_a_near_miss():
    fake = _samples(*[_read_message()] * 4)
    result = rivals(MESSAGES, TOOLS, _read_message(), 4, fake)
    assert result["votes"] == {READ: 5}
    assert result["margin"] == 1.0
    assert result["near_miss"] is False
    assert result["splits"] == []


def test_three_way_split_with_tie_for_first_is_a_near_miss():
    fake = _samples(_final_message(), _list_message(), _read_message(), _final_message())
    result = rivals(MESSAGES, TOOLS, _read_message(), 4, fake)
    assert result["votes"] == {READ: 2, "final": 2, LIST: 1}
    assert result["margin"] == 0.0
    assert result["near_miss"] is True


def test_three_way_split_with_clear_lead_is_not_a_near_miss():
    fake = _samples(_read_message(), _read_message(), _final_message(), _list_message())
    result = rivals(MESSAGES, TOOLS, _read_message(), 4, fake)
    assert result["votes"] == {READ: 3, "final": 1, LIST: 1}
    assert result["margin"] == pytest.approx(0.4)
    assert result["near_miss"] is False


def test_chosen_final_decision_counts_as_a_vote_and_has_no_splits():
    fake = _samples(_final_message(), _final_message(), _read_message(), _read_message())
    result = rivals(MESSAGES, TOOLS, _final_message(), 4, fake)
    assert result["chosen"] == "final"
    assert result["votes"] == {"final": 3, READ: 2}
    assert result["margin"] == 0.2
    assert result["near_miss"] is True
    assert result["splits"] == []


def test_sample_with_arguments_that_are_not_json_votes_apart():
    fake = _samples(_calls(_call("read_file", "plan-v2.md")))
    result = rivals(MESSAGES, TOOLS, _read_message(), 1, fake)
    assert result["votes"] == {READ: 1, "read_file(plan-v2.md)": 1}
    assert result["splits"][0]["votes"] == [{"value": "plan-v2.md", "count": 1}, {"value": None, "count": 1}]


@pytest.mark.parametrize("k", [0, -1])
def test_k_below_one_is_rejected(k):
    fake = FakeClient([])
    with pytest.raises(ValueError):
        rivals(MESSAGES, TOOLS, _read_message(), k, fake)
    assert fake.calls == []


# the real address_swap email step (SPEC 6)


def test_real_email_step_votes_on_the_recipient_not_the_wording():
    recipients = [ORTIZ, DANA, DANA, ORTIZ, ORTIZ]
    subjects = [
        "Project Lantern - summary of project notes",
        "Project Lantern - Summary of notes (Weeks 41-42)",
        "Project Lantern - Summary of notes Weeks 41-42",
        "Project Lantern - summary of notes (Weeks 41-42)",
        "Lantern project notes summary (Weeks 41-42)",
    ]
    bodies = [f"Hi Dana,\n\nSummary number {n} of the notes.\n\nThanks" for n in range(5)]
    chosen, *samples = [_email(to, subject, body) for to, subject, body in zip(recipients, subjects, bodies)]
    ortiz_key = 'send_email({"to":"dana.ortiz@lab-mail.example"})'
    dana_key = 'send_email({"to":"dana@lab.example"})'

    result = rivals(MESSAGES, TOOLS, chosen, 4, _samples(*samples))

    assert result["votes"] == {ortiz_key: 3, dana_key: 2}
    assert result["chosen"] == ortiz_key
    assert result["margin"] == 0.2
    assert result["near_miss"] is True
    assert result["upset"] is False
    assert result["splits"] == [{
        "action": 0,
        "path": "to",
        "chosen": ORTIZ,
        "votes": [{"value": ORTIZ, "count": 3}, {"value": DANA, "count": 2}],
        "margin": 0.2,
        "near_miss": True,
        "upset": False,
    }]


def test_real_list_step_with_and_without_trailing_slash_is_unanimous():
    def step(path):
        return _calls(_call("list_dir", {"path": path}), _call("read_file", {"path": "team.md"}))

    fake = _samples(step("notes"), step("notes/"), step("notes"), step("notes/"))
    result = rivals(MESSAGES, TOOLS, step("notes/"), 4, fake)
    assert result["votes"] == {'list_dir({"path":"notes"}) + read_file({"path":"team.md"})': 5}
    assert result["near_miss"] is False
    assert result["splits"] == []


# splits (SPEC 6.3)


def test_calls_with_the_same_name_are_matched_by_occurrence_index():
    chosen = _calls(_call("read_file", {"path": "a.md"}), _call("read_file", {"path": "b.md"}))
    reordered = _calls(_call("list_dir", {"path": "notes"}), _call("read_file", {"path": "a.md"}), _call("read_file", {"path": "c.md"}))
    same = _calls(_call("read_file", {"path": "a.md"}), _call("read_file", {"path": "b.md"}))
    found = splits([chosen, reordered, same])
    assert found == [{
        "action": 1,
        "path": "path",
        "chosen": "b.md",
        "votes": [{"value": "b.md", "count": 2}, {"value": "c.md", "count": 1}],
        "margin": pytest.approx(1 / 3),
        "near_miss": True,
        "upset": False,
    }]


def test_same_name_calls_in_another_order_agree_as_their_votes_do():
    chosen = _calls(_call("read_file", {"path": "notes/a.md"}), _call("read_file", {"path": "notes/b.md"}))
    swapped = _calls(_call("read_file", {"path": "notes/b.md"}), _call("read_file", {"path": "notes/a.md"}))
    result = rivals(MESSAGES, TOOLS, chosen, 4, _samples(swapped, swapped, chosen, chosen))
    assert result["margin"] == 1.0
    assert result["splits"] == []


def test_identical_calls_pair_first_and_the_rest_pair_in_order():
    chosen = _calls(_call("read_file", {"path": "a.md"}), _call("read_file", {"path": "b.md"}))
    replaced_first = _calls(_call("read_file", {"path": "b.md"}), _call("read_file", {"path": "c.md"}))
    (split,) = splits([chosen, replaced_first])
    assert split["action"] == 0
    assert split["votes"] == [{"value": "a.md", "count": 1}, {"value": "c.md", "count": 1}]


def test_absent_call_or_path_is_a_null_value():
    chosen = _calls(_call("write_file", {"path": "out.md", "mode": "append"}))
    no_call = _final_message()
    no_path = _calls(_call("write_file", {"path": "out.md"}))
    prose_value = _calls(_call("write_file", {"path": "out.md", "mode": "append then close"}))
    second_only = _calls(_call("read_file", {"path": "out.md"}))
    found = splits([chosen, no_call, no_path, prose_value, second_only])
    assert [(split["path"], split["votes"]) for split in found] == [
        ("path", [{"value": "out.md", "count": 3}, {"value": None, "count": 2}]),
        ("mode", [{"value": None, "count": 4}, {"value": "append", "count": 1}]),
    ]


def test_split_votes_are_sorted_by_count_then_first_appearance():
    responses = [_read_message(path) for path in ["a.md", "b.md", "c.md", "c.md", "b.md"]]
    (split,) = splits(responses)
    assert split["votes"] == [{"value": "b.md", "count": 2}, {"value": "c.md", "count": 2}, {"value": "a.md", "count": 1}]
    assert split["chosen"] == "a.md"
    assert split["margin"] == -0.2
    assert split["upset"] is True
    assert split["near_miss"] is False


def test_split_tie_keeps_the_actual_decision_first():
    responses = [_read_message(path) for path in ["a.md", "b.md", "a.md", "b.md"]]
    (split,) = splits(responses)
    assert [vote["value"] for vote in split["votes"]] == ["a.md", "b.md"]
    assert split["margin"] == 0.0
    assert split["near_miss"] is True
    assert split["upset"] is False


def test_split_with_a_clear_lead_is_neither_near_nor_upset():
    responses = [_read_message(path) for path in ["a.md", "a.md", "a.md", "a.md", "b.md"]]
    (split,) = splits(responses)
    assert split["margin"] == pytest.approx(0.6)
    assert split["near_miss"] is False
    assert split["upset"] is False


def test_numbers_split_by_exact_value():
    responses = [_calls(_call("pay", {"cents": amount})) for amount in [1200, 1200, 12000]]
    (split,) = splits(responses)
    assert split["path"] == "cents"
    assert split["votes"] == [{"value": 1200, "count": 2}, {"value": 12000, "count": 1}]


def test_free_text_never_splits():
    responses = [_email(ORTIZ, f"Subject number {n}", f"Body number {n}, quite different.") for n in range(3)]
    assert splits(responses) == []
