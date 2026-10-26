"""Rival sampling (SPEC 6): re-ask the model at one step and count the action signatures it returns."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from causeway.client import message_of


def canonical_json(obj: Any) -> str:
    """Sorted keys, no spaces, non-ASCII kept."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def signature(message: dict[str, Any]) -> str:
    """Return "final" when there are no tool calls. Otherwise the sorted name(canonical args)
    list joined with " + ". Arguments that are not valid JSON fall back to the raw string."""
    calls = message.get("tool_calls") or []
    if not calls:
        return "final"
    pairs: list[tuple[str, str]] = []
    for call in calls:
        fn = call["function"]
        raw = fn.get("arguments")
        args: Any = raw
        if isinstance(raw, str):
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = raw
        pairs.append((fn["name"], canonical_json(args)))
    return " + ".join(f"{name}({args})" for name, args in sorted(pairs))


def rivals(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    chosen_message: dict[str, Any],
    k: int,
    client: Any,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Make k extra calls with the same messages and tools and tally the signatures.

    The chosen decision counts as one vote, so votes sum to k+1. margin is the chosen
    count minus the largest other count, over k+1. near_miss is 0 <= margin <= 1/(k+1).
    upset is margin < 0: a rival outvoted what the agent actually did.
    """
    if k < 1:
        raise ValueError("k must be >= 1; with no rivals every step is trivially a near miss")
    chosen = signature(chosen_message)
    votes: Counter[str] = Counter({chosen: 1})
    extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    for _ in range(k):
        votes[signature(message_of(client.complete(messages, tools=tools, **extra)))] += 1
    others = [count for sig, count in votes.items() if sig != chosen]
    margin = (votes[chosen] - max(others, default=0)) / (k + 1)
    return {
        "k": k,
        "votes": dict(votes),
        "chosen": chosen,
        "margin": margin,
        "near_miss": 0 <= margin <= 1 / (k + 1),
        "upset": margin < 0,
    }
