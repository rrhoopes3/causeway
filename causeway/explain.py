"""Prompt, answer schema and the `why` call (SPEC 7.2, 7.3)."""

from __future__ import annotations

import json
from typing import Any

from causeway.client import message_of
from causeway.render import render
from causeway.verify import verify

_CITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "quote"],
    "properties": {"id": {"type": "string"}, "quote": {"type": "string"}},
}

_CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "cites"],
    "properties": {
        "text": {"type": "string"},
        "cites": {"type": "array", "items": _CITE_SCHEMA},
    },
}

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "claims"],
    "properties": {
        "answer": {"type": "string"},
        "claims": {"type": "array", "items": _CLAIM_SCHEMA},
    },
}

SYSTEM_PROMPT = """You explain why an AI agent took the actions it took.

The user gives you a ledger with one line per record. Lines starting [e...] are input messages (ids e1, e2, ...). Lines starting [d...] are the agent's decisions (ids d1, d2, ...). Each line gives its step number. A decision line shows the action or actions taken, then a cause tag. IN means an argument came from input that had just arrived. CARRY·k means it came from input k steps earlier. GOAL means it came from the task statement. SELF means it came only from the agent's own earlier output. UNSOURCED means it appears nowhere earlier. LIT means a trivial literal that was not traced. A "← e3" list gives the record ids the arguments came from. ECHO←d2 means the decision repeats an earlier one. Newlines inside a record are shown as the two characters \\n.

Answer only from the ledger. Every factual claim must cite at least one record id, with a short quote copied verbatim from that record. The quote must appear in the record text exactly, apart from letter case and spacing. If the ledger does not show something, say "the ledger doesn't show this" instead of guessing.

Reply with JSON only, matching the schema. "answer" is your answer as plain text. "claims" is a list of objects. Each has "text" (one claim) and "cites" (a list of objects, each with "id" and "quote")."""


def response_format() -> dict[str, Any]:
    """The strict json_schema response_format for the answer."""
    return {
        "type": "json_schema",
        "json_schema": {"name": "causeway_answer", "schema": ANSWER_SCHEMA, "strict": True},
    }


def build_messages(ledger: list[dict[str, Any]], question: str) -> list[dict[str, Any]]:
    """System prompt, then one user message holding the rendered ledger and the question."""
    user = f"Ledger:\n{render(ledger)}\n\nQuestion: {question}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def why(
    ledger: list[dict[str, Any]],
    question: str,
    client: Any,
    reasoning_effort: str | None = "high",
) -> dict[str, Any]:
    """Ask the model, parse its JSON and verify it. Content that is not valid JSON is
    treated as a malformed answer. The result is verify()'s dict plus "raw", the content string."""
    response = client.complete(
        build_messages(ledger, question),
        response_format=response_format(),
        reasoning_effort=reasoning_effort,
    )
    content = message_of(response).get("content")
    raw = content if isinstance(content, str) else ""
    try:
        answer = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        answer = None
    result = verify(ledger, answer)
    result["raw"] = raw
    return result
