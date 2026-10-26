"""Cause tagging for ledgers (SPEC §5). Pure functions, no model calls.

Each non-trivial leaf of a decision's arguments is searched for in the records that came
before the decision. The chosen source gives the leaf its tag. The decision takes the
highest-priority tag among its leaves. A prose leaf (§5.4c) is not searched as a whole.
Its atoms (URLs, emails, paths, numbers) are searched instead.
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
PROSE_MIN_WORDS = 8
PROSE_SOURCES = 3
GOAL_ROLES = frozenset({"system", "developer", "user"})
_BASE_RANK = {"UNSOURCED": 0, "IN": 1, "GOAL": 3, "SELF": 4, "PROSE": 5, "LIT": 6}
_URL = re.compile(r"https?://\S+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_EXTENSION = re.compile(r"\.[A-Za-z]{1,5}$")
_TOKEN = re.compile(r"[^\W\d_]{4,}|\d{2,}")
URL_TRAILING = ".,;:)]}'\""
PATH_LEADING = "\"'([{<"
PATH_TRAILING = "\"')]}>,;:!?."


def canon(args: Any) -> str:
    """Compact, key-sorted JSON. Used for action signatures only."""
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


def is_prose(value: Any) -> bool:
    """A string of 8 or more whitespace-separated words, or a string containing a newline (§5.4c)."""
    return isinstance(value, str) and (len(value.split()) >= PROSE_MIN_WORDS or "\n" in value)


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
    """Sort key, highest priority first: UNSOURCED, IN, CARRY·k (smallest k), GOAL, SELF, PROSE, LIT."""
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


def _best(level: str, needle: str, numeric: bool, texts: list[tuple[int, dict, list[str]]], n: int) -> dict | None:
    """The best record at level: highest-priority tag, then most recent. None when no record hits."""
    tokens = _fuzzy_tokens(needle)
    hits = [
        (pos, record)
        for pos, record, record_texts in texts
        if any(_hit(level, needle, tokens, numeric, text) for text in record_texts)
    ]
    if not hits:
        return None
    _, record = min(hits, key=lambda hit: (rank(source_tag(hit[1], n)), -hit[0]))
    return record


def _locate(needle: str, n: int, texts: list[tuple[int, dict, list[str]]]) -> dict | None:
    """Where a path segment or basename is found, exact first, then casefold (§5.4b2)."""
    for level in ("exact", "casefold"):
        record = _best(level, needle, False, texts, n)
        if record is not None:
            return record
    return None


def _composed(needle: str, n: int, texts: list[tuple[int, dict, list[str]]]) -> dict | None:
    """Source of a path built from parts (§5.4b2), or None.

    The path is split on / and \\, dropping empty, "." and ".." segments. The basename must be found and gives
    the source. Each other segment of more than 2 characters must be found somewhere. Shorter segments count as
    found without a source.
    """
    segments = [part for part in re.split(r"[/\\]", needle) if part not in ("", ".", "..")]
    if not segments:
        return None
    basename_source = _locate(segments[-1], n, texts)
    if basename_source is None:
        return None
    for segment in segments[:-1]:
        if len(segment) > 2 and _locate(segment, n, texts) is None:
            return None
    return basename_source


def find_source(needle: str, n: int, sources: list[tuple[int, dict]], numeric: bool = False) -> tuple[dict, str] | None:
    """Find the source of needle for decision n.

    Levels are tried in order: exact, casefold, then parts for a path-like needle (contains / or \\, §5.4b2), then
    fuzzy. Fuzzy applies only to needles of 12 or more characters that contain whitespace (§5.4b), and never to
    numbers. The first level with any hit wins. Among the hits of a level, the highest-priority tag wins, then the
    most recent record. Returns (source record, level), or None.
    """
    texts = [(pos, record, search_texts(record)) for pos, record in sources]
    for level in ("exact", "casefold"):
        record = _best(level, needle, numeric, texts, n)
        if record is not None:
            return record, level
    if not numeric and ("/" in needle or "\\" in needle):
        record = _composed(needle, n, texts)
        if record is not None:
            return record, "parts"
    if not numeric and len(needle) >= FUZZY_MIN_NEEDLE and any(ch.isspace() for ch in needle):
        record = _best("fuzzy", needle, numeric, texts, n)
        if record is not None:
            return record, "fuzzy"
    return None


def _trace(needle: str, numeric: bool, record: dict, sources: list[tuple[int, dict]]) -> tuple[str, str | None, str | None]:
    """Tag, source id and match level for one needle of decision record."""
    found = find_source(needle, record["step"], sources, numeric=numeric)
    if found is None:
        return "UNSOURCED", None, None
    source, match = found
    return source_tag(source, record["step"]), source["id"], match


def _is_path(value: str) -> bool:
    """A token with a slash or backslash, or a .ext suffix of 1 to 5 letters, at least 3 characters long (§5.4c)."""
    return len(value) >= 3 and ("/" in value or "\\" in value or _EXTENSION.search(value) is not None)


def extract_atoms(text: str) -> list[tuple[str, bool]]:
    """Atoms of a prose string as (text, is_number), in position order, without overlaps (§5.4c).

    Kinds are tried in order: URL, email, file path, number. A later kind may not overlap an earlier one.
    A number is \\d+(\\.\\d+)? with at least two digits, so commas always separate numbers.
    """
    claimed: list[tuple[int, int]] = []
    found: list[tuple[int, str, bool]] = []

    def claim(start: int, value: str, numeric: bool) -> None:
        end = start + len(value)
        if any(start < claimed_end and claimed_start < end for claimed_start, claimed_end in claimed):
            return
        claimed.append((start, end))
        found.append((start, value, numeric))

    for match in _URL.finditer(text):
        claim(match.start(), match.group().rstrip(URL_TRAILING), False)
    for match in _EMAIL.finditer(text):
        claim(match.start(), match.group(), False)
    for match in re.finditer(r"\S+", text):
        leading = match.group().lstrip(PATH_LEADING)
        start = match.start() + len(match.group()) - len(leading)
        value = leading.rstrip(PATH_TRAILING)
        if _is_path(value):
            claim(start, value, False)
    for match in _NUMBER.finditer(text):
        if sum(ch.isdigit() for ch in match.group()) >= 2:
            claim(match.start(), match.group(), True)
    return [(value, numeric) for _, value, numeric in sorted(found)]


def prose_tokens(text: str) -> set[str]:
    """Distinct casefolded words of 4 or more letters and numbers of 2 or more digits (§5.4c)."""
    return set(_TOKEN.findall(text.casefold()))


def prose_sources(tokens: set[str], sources: list[tuple[int, dict]]) -> list[str]:
    """Up to PROSE_SOURCES record ids whose coverage of the tokens is at least 0.2.

    Coverage is the fraction of tokens found in the record's search texts. The order is highest coverage first,
    then most recent first. The coverage test is done in integers: count * 5 >= number of tokens.
    """
    if not tokens:
        return []
    scored = []
    for pos, record in sources:
        texts = [text.casefold() for text in search_texts(record)]
        count = sum(any(token in text for text in texts) for token in tokens)
        if count * 5 >= len(tokens):
            scored.append((count, pos, record["id"]))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [record_id for _, _, record_id in scored[:PROSE_SOURCES]]


def _watch_entries(record: dict, causes: list[dict], watch: frozenset[str]) -> list[dict] | None:
    """Taint entries for the watched actions of a decision (§5.5).

    Returns None when no action of the decision is watched, so the decision gets no watch key. A taint is any
    traced leaf or atom whose tag is not GOAL or PROSE. Trivial leaves never appear in causes.
    """
    watched = {index for index, action in enumerate(record["actions"]) if action["name"] in watch}
    if not watched:
        return None
    return [
        {"action": cause["action"], "path": cause["path"], "tag": cause["tag"], "source": cause["source"]}
        for cause in causes
        if cause["action"] in watched and cause["tag"] not in ("GOAL", "PROSE")
    ]


def _tag_decision(record: dict, sources: list[tuple[int, dict]], latest: dict, watch: frozenset[str]) -> dict:
    """Return a copy of a decision with causes, tag, echo_of and, if an action is watched, watch filled in.

    Trivial leaves count toward the tag but get no cause entry. A prose leaf gets a PROSE entry with its
    sources, then one entry per atom, path <leaf>~<n>. A decision with no actions is FINAL. Echoes only apply
    to decisions with at least one action. Any watch key already on the record is dropped first.
    """
    causes: list[dict] = []
    leaf_tags: list[str] = []
    for index, action in enumerate(record["actions"]):
        for path, value in flatten(action["args"]):
            if is_trivial(value):
                leaf_tags.append("LIT")
                continue
            if is_prose(value):
                found_sources = prose_sources(prose_tokens(value), sources)
                causes.append({
                    "action": index, "path": path, "value": value, "tag": "PROSE",
                    "source": found_sources[0] if found_sources else None,
                    "sources": found_sources, "match": None,
                })
                leaf_tags.append("PROSE")
                for atom_index, (atom, numeric) in enumerate(extract_atoms(value)):
                    if numeric and is_trivial(float(atom)):
                        leaf_tags.append("LIT")
                        continue
                    tag, source_id, match = _trace(atom, numeric, record, sources)
                    causes.append({
                        "action": index, "path": f"{path}~{atom_index}", "value": atom,
                        "tag": tag, "source": source_id, "match": match,
                    })
                    leaf_tags.append(tag)
                continue
            numeric = not isinstance(value, str)
            tag, source_id, match = _trace(needle_of(value), numeric, record, sources)
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
