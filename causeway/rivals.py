"""Rival sampling (SPEC 6): re-ask the model at one step and count what each response targets.

Votes compare action keys built from key leaves only (§6.1, §6.2), so free text such as an email body never splits
them. Splits (§6.3) show which key leaf the responses disagreed on, value by value.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from causeway import tagger
from causeway.client import message_of


def canonical_json(obj: Any) -> str:
    """Sorted keys, no spaces, non-ASCII kept."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def is_key_leaf(value: Any) -> bool:
    """A leaf that is not trivial (§5.1) and is not a string containing whitespace (§6.1)."""
    if tagger.is_trivial(value):
        return False
    return not (isinstance(value, str) and any(char.isspace() for char in value))


def normalize(value: Any) -> Any:
    """The value a key leaf compares by (§6.1).

    A path (a string with / or \\) has \\ turned into /, then trailing / stripped and a leading ./ stripped, each
    unless that would leave it empty. The slashes after a leading ./ go with it, so .//notes is notes, not /notes.
    A whole float becomes an int, so 12.0 and 12 compare equal, as §5.2 reads 12.0 as 12. Other values are unchanged.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if not isinstance(value, str) or ("/" not in value and "\\" not in value):
        return value
    text = value.replace("\\", "/").rstrip("/") or "/"
    while text.startswith("./") and text[2:].lstrip("/"):
        text = text[2:].lstrip("/")
    return text


def key_leaves(arguments: Any) -> dict[str, Any] | None:
    """{path: normalized value} for each key leaf of a call's arguments, in flatten order (§6.1).

    Arguments are read as the ledger reads them (transcript.parse_args): missing or blank arguments are no arguments,
    and a JSON string is decoded. None when they are not a JSON object, so §6.2 keys the call by the raw string.
    """
    if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
        return {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    return {path: normalize(value) for path, value in tagger.flatten(arguments) if is_key_leaf(value)}


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    return message.get("tool_calls") or []


def _leaves(call: dict[str, Any]) -> dict[str, Any]:
    """The call's key leaves, or none when its arguments are not a JSON object."""
    return key_leaves(call["function"].get("arguments")) or {}


def _call_key(call: dict[str, Any]) -> str:
    """name(canonical key leaves), or name(raw arguments) when they are not a JSON object (§6.2)."""
    fn = call["function"]
    leaves = key_leaves(fn.get("arguments"))
    if leaves is None:
        return f"{fn['name']}({fn['arguments']})"
    return f"{fn['name']}({canonical_json(leaves)})"


def signature(message: dict[str, Any]) -> str:
    """The action key of a response (§6.2): "final" when it has no tool calls, otherwise the sorted
    name(canonical-json({path: value})) of each call over its key leaves, joined with " + "."""
    calls = _tool_calls(message)
    if not calls:
        return "final"
    return " + ".join(sorted(_call_key(call) for call in calls))


def _verdict(counts: dict[Any, int], chosen: Any, total: int) -> dict[str, Any]:
    """margin, near_miss and upset of the chosen entry against the largest other count (§6.2)."""
    rival = max((count for key, count in counts.items() if key != chosen), default=0)
    margin = (counts[chosen] - rival) / total
    return {"margin": margin, "near_miss": 0 <= margin <= 1 / total, "upset": margin < 0}


def _partners(chosen_calls: list[dict[str, Any]], calls: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """For each chosen call, the call of a response that §6.3 compares it with, or None when there is none.

    An identical call (same §6.2 call key) pairs first, so a response that makes the same calls of one tool in
    another order agrees, as its vote does. The calls left over pair by occurrence index among the leftover calls
    of the same tool name.
    """
    partners: list[dict[str, Any] | None] = [None] * len(chosen_calls)
    free = list(range(len(calls)))
    keys = [_call_key(call) for call in calls]
    for index, call in enumerate(chosen_calls):
        key = _call_key(call)
        match = next((position for position in free if keys[position] == key), None)
        if match is not None:
            partners[index] = calls[match]
            free.remove(match)
    names = [call["function"]["name"] for call in chosen_calls]
    for name in dict.fromkeys(names):
        left = [index for index, partner in enumerate(partners) if partner is None and names[index] == name]
        spare = [calls[position] for position in free if calls[position]["function"]["name"] == name]
        for index, call in zip(left, spare):
            partners[index] = call
    return partners


def splits(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One split per key leaf of the first response (the actual decision) on which the responses disagree (§6.3).

    Calls are matched by tool name (see _partners). Votes are sorted by count, highest first, then by first
    appearance. Values compare by their canonical JSON.
    """
    chosen_calls, total = _tool_calls(responses[0]), len(responses)
    pairings = [_partners(chosen_calls, _tool_calls(response)) for response in responses]
    found: list[dict[str, Any]] = []
    for index, call in enumerate(chosen_calls):
        for path, chosen in _leaves(call).items():
            tally: dict[str, list[Any]] = {}
            for partners in pairings:
                partner = partners[index]
                value = None if partner is None else _leaves(partner).get(path)
                tally.setdefault(canonical_json(value), [value, 0])[1] += 1
            if len(tally) < 2:
                continue
            counts = {key: entry[1] for key, entry in tally.items()}
            votes = sorted(tally.values(), key=lambda entry: -entry[1])
            found.append({
                "action": index,
                "path": path,
                "chosen": chosen,
                "votes": [{"value": value, "count": count} for value, count in votes],
                **_verdict(counts, canonical_json(chosen), total),
            })
    return found


def rivals(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    chosen_message: dict[str, Any],
    k: int,
    client: Any,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Make k extra calls with the same messages and tools, and tally action keys and splits (§6.4).

    The chosen decision counts as one vote, so votes sum to k+1. margin is the chosen count minus the largest
    other count, over k+1. near_miss is 0 <= margin <= 1/(k+1). upset is margin < 0: a rival outvoted what the
    agent actually did.
    """
    if k < 1:
        raise ValueError("k must be >= 1; with no rivals every step is trivially a near miss")
    extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    samples = [message_of(client.complete(messages, tools=tools, **extra)) for _ in range(k)]
    chosen = signature(chosen_message)
    votes: Counter[str] = Counter({chosen: 1})
    for sample in samples:
        votes[signature(sample)] += 1
    return {
        "k": k,
        "votes": dict(votes),
        "chosen": chosen,
        **_verdict(votes, chosen, k + 1),
        "splits": splits([chosen_message, *samples]),
    }
