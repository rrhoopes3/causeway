import json
from pathlib import Path

from causeway.explain import ANSWER_SCHEMA, SYSTEM_PROMPT, build_messages, response_format, why
from causeway.client import FakeClient
from causeway.render import render

FIXTURE = Path(__file__).parent / "fixtures" / "ledger_slice2.jsonl"
QUESTION = "Why did it read plan-v2.md at step 4?"


def _ledger():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def _response(content):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


def _check_strict(node, path="root"):
    """Every object: additionalProperties false, and required lists every property."""
    if not isinstance(node, dict):
        return
    if node.get("type") == "object":
        assert node.get("additionalProperties") is False, f"{path} allows extra keys"
        assert sorted(node.get("required", [])) == sorted(node.get("properties", {})), f"{path} required mismatch"
    for key, child in node.get("properties", {}).items():
        _check_strict(child, f"{path}.{key}")
    if "items" in node:
        _check_strict(node["items"], f"{path}[]")


def test_schema_is_strict_at_every_object():
    _check_strict(ANSWER_SCHEMA)


def test_schema_top_level_shape():
    assert ANSWER_SCHEMA["required"] == ["answer", "claims"]
    assert ANSWER_SCHEMA["properties"]["claims"]["items"]["properties"]["cites"]["items"]["required"] == [
        "id",
        "quote",
    ]


def test_response_format_wraps_schema_strictly():
    fmt = response_format()
    assert fmt == {
        "type": "json_schema",
        "json_schema": {"name": "causeway_answer", "schema": ANSWER_SCHEMA, "strict": True},
    }


def test_system_prompt_states_the_rules():
    assert "verbatim" in SYSTEM_PROMPT
    assert "the ledger doesn't show this" in SYSTEM_PROMPT


def test_build_messages_contains_system_prompt_ledger_and_question():
    ledger = _ledger()
    messages = build_messages(ledger, QUESTION)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == SYSTEM_PROMPT
    assert render(ledger) in messages[1]["content"]
    assert messages[1]["content"].endswith(f"Question: {QUESTION}")


def test_why_verifies_supported_and_struck_claims_end_to_end():
    content = json.dumps(
        {
            "answer": "It read plan-v2.md because list_dir listed it, then read it again.",
            "claims": [
                {"text": "list_dir listed plan-v2.md.", "cites": [{"id": "e3", "quote": "plan-v2.md"}]},
                {
                    "text": "The plan has a Friday deadline.",
                    "cites": [{"id": "e4", "quote": "ships Friday"}],
                },
            ],
        }
    )
    fake = FakeClient([_response(content)])
    result = why(_ledger(), QUESTION, fake)

    assert result["answer"].startswith("It read plan-v2.md")
    assert result["ok"] == 1
    assert result["total"] == 2
    assert [c["text"] for c in result["supported"]] == ["list_dir listed plan-v2.md."]
    assert result["struck"][0]["claim"]["text"] == "The plan has a Friday deadline."
    assert result["struck"][0]["reasons"] == ["quote not found in e4"]
    assert result["raw"] == content

    call = fake.calls[0]
    assert call["response_format"] == response_format()
    assert call["reasoning_effort"] == "high"
    assert call["tools"] is None
    assert call["messages"] == build_messages(_ledger(), QUESTION)


def test_why_treats_invalid_json_as_malformed():
    fake = FakeClient([_response("Sorry, here is my answer: it read the file.")])
    result = why(_ledger(), QUESTION, fake)
    assert result["ok"] == 0
    assert result["supported"] == []
    assert result["answer"] is None
    assert all(entry["reasons"] == ["malformed answer"] for entry in result["struck"])
    assert result["raw"] == "Sorry, here is my answer: it read the file."


def test_why_treats_null_content_as_malformed():
    fake = FakeClient([{"choices": [{"message": {"role": "assistant", "content": None}}]}])
    result = why(_ledger(), QUESTION, fake, reasoning_effort="low")
    assert result["ok"] == 0
    assert result["struck"][0]["reasons"] == ["malformed answer"]
    assert result["raw"] == ""
    assert fake.calls[0]["reasoning_effort"] == "low"


def test_why_treats_non_object_json_as_malformed():
    fake = FakeClient([_response("[1, 2, 3]")])
    result = why(_ledger(), QUESTION, fake)
    assert result["ok"] == 0
    assert result["struck"][0]["reasons"] == ["malformed answer"]
    assert result["raw"] == "[1, 2, 3]"
