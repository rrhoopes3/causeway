"""Cause tagging for ledgers (SPEC §5). Pure functions, no model calls.

Each non-trivial leaf of a decision's arguments is searched for in the records that came
before the decision. The chosen source gives the leaf its tag. The decision takes the
highest-priority tag among its leaves.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

CARRY = "CARRY·"
DIGITS = "0123456789"
TRIVIAL_NUMBER = 10
TRIVIAL_STRING = 3
FUZZY_MIN_NEEDLE = 12
FUZZY_MIN_TOKEN = 4
GOAL_ROLES = frozenset({"system", "developer", "user"})
_BASE_RANK = {"UNSOURCED": 0, "IN": 1, "GOAL": 3, "SELF": 4, "LIT": 5}


def canon(args: Any) -> str:
    """Compact, key-sorted JSON. Used for action signatures and for decision search text."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"))


def flatten(value: Any, path: str = "") -> list[tuple[str, Any]]:
    """Return (dotted path, value) for each scalar leaf. List indexes become path segments. Nulls are skipped."""
    if value is None:
        return []
    if isinstance(value, dict):
        children = [(str(key), child) for key, child in value.items()]
    elif isinstance(value, list):
        children = [(str(index), child) for index, child in enumerate(value)]
    else:
        return [(path, value)]
    leaves: list[tuple[str, Any]] = []
    for key, child in children:
        leaves.extend(flatten(child, f"{path}.{key}" if path else key))
    return leaves


def is_trivial(value: Any) -> bool:
    """Booleans, numbers with absolute value below 10, and strings shorter than 3 characters once stripped (§5.1)."""
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return abs(value) < TRIVIAL_NUMBER
    if isinstance(value, str):
        return len(value.strip()) < TRIVIAL_STRING
    return False


def needle_of(value: Any) -> str:
    """The text searched for. A string is itself. A number is its shortest plain form: 12.0 gives "12", 3.5 gives "3.5"."""
    if isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def is_goal(record: dict) -> bool:
    """A system, developer or user event from step 1 is part of the goal."""
    return record["kind"] == "event" and record["step"] == 1 and record["role"] in GOAL_ROLES


def search_texts(record: dict) -> list[str]:
    """Separate texts a record offers as sources, empty ones dropped.

    An event offers its text. A decision offers its text, then the decoded value of each leaf of each action,
    trivial ones included, in needle form. Booleans are skipped because no needle can be one. Serialized JSON is
    never searched, so escapes and key names cannot match.
    """
    texts = [record.get("text") or ""]
    if record["kind"] == "decision":
        for action in record["actions"]:
            for _, value in flatten(action["args"]):
                if not isinstance(value, bool):
                    texts.append(needle_of(value))
    return [text for text in texts if text]


def source_tag(source: dict, n: int) -> str:
    """Tag for a leaf found in source, when the leaf belongs to decision n (§5.3)."""
    if source["kind"] == "decision":
        return "SELF"
    if is_goal(source):
        return "GOAL"
    if source["step"] == n:
        return "IN"
    return f"{CARRY}{n - source['step']}"


def rank(tag: str) -> tuple[int, int]:
    """Sort key, highest priority first: UNSOURCED, IN, CARRY·k (smallest k), GOAL, SELF, LIT."""
    if tag.startswith(CARRY):
        return (2, int(tag[len(CARRY):]))
    return (_BASE_RANK[tag], 0)


def _fuzzy_tokens(needle: str) -> list[str]:
    """Casefolded alphanumeric tokens of at least FUZZY_MIN_TOKEN characters."""
    return [token.casefold() for token in re.split(r"[\W_]+", needle) if len(token) >= FUZZY_MIN_TOKEN]


def _is_digit(char: str) -> bool:
    return len(char) == 1 and char in DIGITS


def _numeric_at(text: str, needle: str, start: int) -> bool:
    """Whether a number needle found at start sits on numeric boundaries.

    The characters before and after must not be digits, nor a "." followed by a digit. So 42 is not
    found in 420, 3.42 or 42.5, but is found in "port 42.".
    """
    end = start + len(needle)
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if _is_digit(before) or _is_digit(after):
        return False
    if before == "." and _is_digit(text[start]):
        return False
    if after == "." and end + 1 < len(text) and _is_digit(text[end + 1]):
        return False
    return True


def _numeric_hit(needle: str, text: str) -> bool:
    start = text.find(needle)
    while start != -1:
        if _numeric_at(text, needle, start):
            return True
        start = text.find(needle, start + 1)
    return False


def _hit(level: str, needle: str, tokens: list[str], numeric: bool, text: str) -> bool:
    """Whether text matches needle at level. Numbers match only at numeric boundaries and never fuzzily.

    Fuzzy needs at least 60% of the tokens, checked in integers.
    """
    if level == "exact":
        return _numeric_hit(needle, text) if numeric else needle in text
    if level == "casefold":
        folded_needle, folded = needle.casefold(), text.casefold()
        return _numeric_hit(folded_needle, folded) if numeric else folded_needle in folded
    folded = text.casefold()
    matched = sum(token in folded for token in tokens)
    return bool(tokens) and matched * 5 >= len(tokens) * 3


def find_source(needle: str, n: int, sources: list[tuple[int, dict]], numeric: bool = False) -> tuple[dict, str] | None:
    """Find the source of needle for decision n.

    Levels are tried in order: exact, casefold, and fuzzy for needles of 12 or more characters that are not
    numbers. The first level with any hit wins. A record hits when any of its search texts hits. Among the hits,
    the highest-priority tag wins, then the most recent record. Returns (source record, level), or None.
    """
    tokens = _fuzzy_tokens(needle)
    levels = ["exact", "casefold"]
    if not numeric and len(needle) >= FUZZY_MIN_NEEDLE:
        levels.append("fuzzy")
    texts = [(pos, record, search_texts(record)) for pos, record in sources]
    for level in levels:
        hits = [
            (pos, record)
            for pos, record, record_texts in texts
            if any(_hit(level, needle, tokens, numeric, text) for text in record_texts)
        ]
        if hits:
            _, record = min(hits, key=lambda hit: (rank(source_tag(hit[1], n)), -hit[0]))
            return record, level
    return None


def _watch_entries(record: dict, causes: list[dict], watch: frozenset[str]) -> list[dict] | None:
    """Taint entries for the watched actions of a decision (§5.5).

    Returns None when no action of the decision is watched, so the decision gets no watch key. A taint is any
    traced leaf whose tag is not GOAL. Trivial leaves never appear in causes, so they are never taints.
    """
    watched = {index for index, action in enumerate(record["actions"]) if action["name"] in watch}
    if not watched:
        return None
    return [
        {"action": cause["action"], "path": cause["path"], "tag": cause["tag"], "source": cause["source"]}
        for cause in causes
        if cause["action"] in watched and cause["tag"] != "GOAL"
    ]


def _tag_decision(record: dict, sources: list[tuple[int, dict]], latest: dict, watch: frozenset[str]) -> dict:
    """Return a copy of a decision with causes, tag, echo_of and, if an action is watched, watch filled in.

    Trivial leaves count toward the tag but get no cause entry. A decision with no actions is FINAL.
    Echoes only apply to decisions with at least one action. Any watch key already on the record is dropped first.
    """
    causes: list[dict] = []
    leaf_tags: list[str] = []
    for index, action in enumerate(record["actions"]):
        for path, value in flatten(action["args"]):
            if is_trivial(value):
                leaf_tags.append("LIT")
                continue
            found = find_source(needle_of(value), record["step"], sources, numeric=not isinstance(value, str))
            if found is None:
                tag, source_id, match = "UNSOURCED", None, None
            else:
                source, match = found
                tag, source_id = source_tag(source, record["step"]), source["id"]
            causes.append({"action": index, "path": path, "value": value, "tag": tag, "source": source_id, "match": match})
            leaf_tags.append(tag)
    if not record["actions"]:
        tag = "FINAL"
    elif not leaf_tags:
        tag = "NOARGS"
    else:
        tag = min(leaf_tags, key=rank)
    echo_of = None
    if record["actions"]:
        signature = tuple(sorted((str(action["name"]), canon(action["args"])) for action in record["actions"]))
        echo_of = latest.get(signature)
        latest[signature] = record["id"]
    result = {key: value for key, value in record.items() if key != "watch"}
    result.update(causes=causes, tag=tag, echo_of=echo_of)
    entries = _watch_entries(record, causes, watch)
    if entries is not None:
        result["watch"] = entries
    return result


def tag_ledger(records: list[dict], watch: Iterable[str] = ()) -> list[dict]:
    """Return a new list of records with every decision's causes, tag and echo_of filled in.

    watch names the tools to taint-check (§5.5). A decision with a watched action gets a watch list. Other
    decisions get no watch key. Events and the header are copied unchanged. The input is not modified.
    Tagging is recomputed from scratch, so tagging a tagged ledger gives the same result.
    """
    names = frozenset(watch)
    tagged: list[dict] = []
    sources: list[tuple[int, dict]] = []
    latest: dict[tuple[tuple[str, str], ...], str] = {}
    for pos, record in enumerate(records):
        if record["kind"] == "decision":
            tagged.append(_tag_decision(record, sources, latest, names))
        else:
            tagged.append(dict(record))
        if record["kind"] in ("event", "decision"):
            sources.append((pos, record))
    return tagged
