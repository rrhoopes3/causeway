import pytest

from causeway.client import FakeClient
from causeway.rivals import canonical_json, rivals, signature

TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
MESSAGES = [{"role": "user", "content": "Read the plan."}]
READ = "read_file({\"path\":\"plan-v2.md\"})"
LIST = "list_dir({\"path\":\".\"})"


def _call(name, arguments):
    return {"id": "call_x", "type": "function", "function": {"name": name, "arguments": arguments}}


def _read_message():
    return {"role": "assistant", "content": None, "tool_calls": [_call("read_file", '{"path": "plan-v2.md"}')]}


def _final_message():
    return {"role": "assistant", "content": "All done."}


def _response(message):
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


def _samples(*messages):
    return FakeClient([_response(m) for m in messages])


def test_signature_of_final_message():
    assert signature(_final_message()) == "final"
    assert signature({"role": "assistant", "content": "x", "tool_calls": []}) == "final"


def test_signature_of_single_tool_call_is_canonical():
    assert signature(_read_message()) == READ


def test_signature_ignores_argument_key_order_and_whitespace():
    a = {"tool_calls": [_call("read_file", '{"b": 1, "a": {"y": 2, "x": 3}}')]}
    b = {"tool_calls": [_call("read_file", '{"a":{"x":3,"y":2},"b":1}')]}
    assert signature(a) == signature(b) == 'read_file({"a":{"x":3,"y":2},"b":1})'


def test_signature_sorts_multiple_calls_regardless_of_order():
    ab = {"tool_calls": [_call("read_file", '{"path":"x"}'), _call("list_dir", '{"path":"."}')]}
    ba = {"tool_calls": [_call("list_dir", '{"path":"."}'), _call("read_file", '{"path":"x"}')]}
    expected = 'list_dir({"path":"."}) + read_file({"path":"x"})'
    assert signature(ab) == signature(ba) == expected


def test_signature_falls_back_to_raw_string_for_bad_json():
    message = {"tool_calls": [_call("read_file", "not json")]}
    assert signature(message) == 'read_file("not json")'


def test_canonical_json_matches_spec_format():
    assert canonical_json({"b": "café", "a": [1, True]}) == '{"a":[1,true],"b":"café"}'


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
    }


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


def test_three_way_split_with_tie_for_first_is_a_near_miss():
    fake = _samples(_final_message(), _call_message(LIST), _read_message(), _final_message())
    result = rivals(MESSAGES, TOOLS, _read_message(), 4, fake)
    assert result["votes"] == {READ: 2, "final": 2, LIST: 1}
    assert result["margin"] == 0.0
    assert result["near_miss"] is True


def test_three_way_split_with_clear_lead_is_not_a_near_miss():
    fake = _samples(_read_message(), _read_message(), _final_message(), _call_message(LIST))
    result = rivals(MESSAGES, TOOLS, _read_message(), 4, fake)
    assert result["votes"] == {READ: 3, "final": 1, LIST: 1}
    assert result["margin"] == pytest.approx(0.4)
    assert result["near_miss"] is False


def test_chosen_final_decision_counts_as_a_vote():
    fake = _samples(_final_message(), _final_message(), _read_message(), _read_message())
    result = rivals(MESSAGES, TOOLS, _final_message(), 4, fake)
    assert result["chosen"] == "final"
    assert result["votes"] == {"final": 3, READ: 2}
    assert result["margin"] == 0.2
    assert result["near_miss"] is True


@pytest.mark.parametrize("k", [0, -1])
def test_k_below_one_is_rejected(k):
    fake = FakeClient([])
    with pytest.raises(ValueError):
        rivals(MESSAGES, TOOLS, _read_message(), k, fake)
    assert fake.calls == []


def _call_message(signature_text):
    name, _, args = signature_text.partition("(")
    return {"role": "assistant", "content": None, "tool_calls": [_call(name, args[:-1])]}
