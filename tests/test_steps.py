import json
from pathlib import Path

from causeway.steps import prefixes

FIXTURES = Path(__file__).parent / "fixtures"


def _user(text):
    return {"role": "user", "content": text}


def _assistant(text=None, call_id=None):
    if call_id is None:
        return {"role": "assistant", "content": text}
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
    }


def _tool(call_id, text):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def test_no_messages_gives_no_pairs():
    assert prefixes([]) == []


def test_no_assistant_message_gives_no_pairs():
    assert prefixes([_user("hi"), _tool("c1", "x")]) == []


def test_each_pair_holds_everything_before_its_assistant_message():
    system = {"role": "system", "content": "sys"}
    first, second = _assistant(call_id="c1"), _assistant("done")
    messages = [system, _user("go"), first, _tool("c1", "result"), second]
    pairs = prefixes(messages)
    assert [assistant for _, assistant in pairs] == [first, second]
    assert pairs[0][0] == [system, _user("go")]
    assert pairs[1][0] == [system, _user("go"), first, _tool("c1", "result")]


def test_pairs_come_in_order_and_hold_the_same_message_objects():
    first, second = _assistant(call_id="c1"), _assistant(call_id="c2")
    messages = [_user("go"), first, _tool("c1", "a"), second]
    pairs = prefixes(messages)
    assert pairs[0][1] is messages[1]
    assert pairs[1][1] is messages[3]
    assert pairs[0][0][0] is messages[0]


def test_prefix_is_a_copy_and_does_not_change_the_transcript():
    messages = [_user("go"), _assistant("one"), _user("again"), _assistant("two")]
    before_first, _ = prefixes(messages)[0]
    before_first.append(_user("mutated"))
    assert len(messages) == 4
    assert prefixes(messages)[0][0] == [_user("go")]


def test_trailing_messages_after_the_last_assistant_are_in_no_prefix():
    messages = [_user("go"), _assistant("answer"), _tool("c9", "late")]
    pairs = prefixes(messages)
    assert len(pairs) == 1
    assert pairs[0][0] == [_user("go")]


def test_explore_fixture_gives_five_decisions_with_growing_prefixes():
    data = json.loads((FIXTURES / "explore_basic.json").read_text(encoding="utf-8"))
    messages = data["messages"]
    pairs = prefixes(messages)
    assert len(pairs) == 5
    assert [len(before) for before, _ in pairs] == [2, 4, 6, 8, 10]
    assert pairs[-1][1]["content"].startswith("plan-v2.md is the newest")
