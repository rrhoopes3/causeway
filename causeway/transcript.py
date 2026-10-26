"""Turn an OpenAI chat-completions message list into a tagged ledger (SPEC §4)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from causeway import __version__, tagger

EVENT_ROLES = frozenset({"system", "developer", "user", "tool"})


def to_ledger(
    messages: list[dict],
    run_id: str = "r-1",
    model: str = "unknown",
    watch: Iterable[str] = (),
) -> list[dict]:
    """Build a tagged ledger from chat messages.

    Step n is the n-th assistant message. Each assistant message is a decision, and each other message is an
    event of the step that follows the previous decision. A tool result takes its name from the call it answers.
    Message content is text: a list of parts keeps its text parts, joined with newlines. watch names the tools to
    taint-check (§5.5). Raises ValueError for an unknown role.
    """
    records: list[dict] = [
        {"kind": "header", "run_id": run_id, "model": model, "source": "transcript", "causeway": __version__}
    ]
    step = 1
    event_count = 0
    decision_count = 0
    call_names: dict[Any, str | None] = {}
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            decision_count += 1
            actions = [_action(call, call_names) for call in message.get("tool_calls") or []]
            records.append({
                "kind": "decision",
                "id": f"d{decision_count}",
                "step": step,
                "text": _text(message.get("content")) or None,
                "actions": actions,
                "final": not actions,
                "causes": [],
                "tag": None,
                "echo_of": None,
                "rivals": None,
            })
            step += 1
        elif role in EVENT_ROLES:
            event_count += 1
            record: dict[str, Any] = {
                "kind": "event",
                "id": f"e{event_count}",
                "step": step,
                "role": role,
                "text": _text(message.get("content")),
            }
            if role == "tool":
                call_id = message.get("tool_call_id")
                record["name"] = call_names.get(call_id, message.get("name"))
                record["tool_call_id"] = call_id
            records.append(record)
        else:
            raise ValueError(f"unsupported message role: {role!r}")
    return tagger.tag_ledger(records, watch=watch)


def parse_args(raw: Any) -> dict:
    """Decode a tool call's arguments.

    A JSON object is returned as is. Missing or blank arguments give {}. Anything else that is not a JSON object,
    including malformed JSON, is kept as {"_raw": text}.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if raw is None:
        return {}
    if not isinstance(raw, str):
        return {"_raw": str(raw)}
    if not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return decoded if isinstance(decoded, dict) else {"_raw": raw}


def _action(call: dict, call_names: dict[Any, str | None]) -> dict:
    """One action of a decision. Remembers the call's name so that its tool result can carry it."""
    function = call.get("function") or {}
    call_names[call.get("id")] = function.get("name")
    return {"call_id": call.get("id"), "name": function.get("name"), "args": parse_args(function.get("arguments"))}


def _text(content: Any) -> str:
    """Message content as text. Strings are kept, the text parts of a list are joined with newlines, None is empty."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    return str(content)
