"""Offline verifier for an explanation (SPEC 7.3): every cite must name a real record and quote it.

A quote that really appears still matches as a substring. A claim that then joins two
quoted facts with a motive those quotes never connect (`because`, `so that`, and the
same family) is struck.
"""

from __future__ import annotations

import re
from typing import Any

from causeway.render import searchable

_WS = re.compile(r"\s+")
MIN_QUOTE = 6
"""Normalized characters a quote needs; shorter ones ("the", "IN") match nearly any record."""


def _norm(text: str) -> str:
    """Casefold, treat the two characters backslash-n as whitespace, collapse whitespace runs, strip.

    Applied to both quotes and record text so that rendered newlines match real ones."""
    return _WS.sub(" ", text.replace("\\n", " ")).casefold().strip()


def _quote_in(quote: str, text: str) -> bool:
    needle = _norm(quote)
    return bool(needle) and needle in _norm(text)


# Longer phrases first so "in order to" is not read as a shorter marker.
_CAUSAL = re.compile(r"\b(?:in order that|in order to|so as to|so that|because)\b")
_CLAUSE_BREAK = re.compile(r"[;]|(?<=[.!?])\s+")
_TOKEN = re.compile(r"[a-z0-9]+(?:[.'’@_-][a-z0-9]+)*")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _clause_before(text: str) -> str:
    parts = _CLAUSE_BREAK.split(text)
    return parts[-1].strip(" ,;:") if parts else ""


def _clause_after(text: str) -> str:
    parts = _CLAUSE_BREAK.split(text, maxsplit=1)
    return parts[0].strip(" ,;:") if parts else ""


def _causal_links(text: str) -> list[tuple[str, str, str]]:
    """Each connective that sits between two facts, as (marker, left clause, right clause)."""
    links: list[tuple[str, str, str]] = []
    for match in _CAUSAL.finditer(text):
        left = _clause_before(text[: match.start()])
        right = _clause_after(text[match.end() :])
        if left and right:
            links.append((match.group(0), left, right))
    return links


def _tokens_in_order(needle: list[str], hay: list[str]) -> bool:
    """True when `needle` is a contiguous token run inside `hay`."""
    span = len(needle)
    if span == 0:
        return False
    return any(hay[index : index + span] == needle for index in range(len(hay) - span + 1))


def _quote_links(marker: str, left: str, right: str, quote: str) -> bool:
    """The quote joins these facts when the same connective has each fact on its matching side.

    The fact has to appear in order, so "did not use" is not grounded by "did use".
    """
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return False
    for quoted_marker, quoted_left, quoted_right in _causal_links(quote):
        if quoted_marker != marker:
            continue
        if _tokens_in_order(left_tokens, _tokens(quoted_left)) and _tokens_in_order(
            right_tokens, _tokens(quoted_right)
        ):
            return True
    return False


def _motive_reason(claim: dict[str, Any]) -> str | None:
    """A claim may not add a motive the quotes never connect.

    Substring checks have already passed. `because` / `so that` (also `so as to`,
    `in order to`, `in order that`) between two facts stays supported only when one
    cited quote uses that same connective with both facts on the matching sides.
    A claim that only restates a quote keeps that quote's connective and stays supported.
    """
    links = _causal_links(_norm(claim["text"]))
    if not links:
        return None
    quotes = [_norm(cite["quote"]) for cite in claim["cites"]]
    for marker, left, right in links:
        if not any(_quote_links(marker, left, right, quote) for quote in quotes):
            return "motive not in quotes"
    return None


def _is_claim(claim: Any) -> bool:
    return (
        isinstance(claim, dict)
        and isinstance(claim.get("text"), str)
        and isinstance(claim.get("cites"), list)
        and all(
            isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("quote"), str)
            for c in claim["cites"]
        )
    )


def _well_formed(answer: Any) -> bool:
    return (
        isinstance(answer, dict)
        and isinstance(answer.get("answer"), str)
        and isinstance(answer.get("claims"), list)
        and all(_is_claim(c) for c in answer["claims"])
    )


def _malformed(answer: Any) -> dict[str, Any]:
    """Strike every claim with `malformed answer`. With no claims list, one entry with claim None."""
    claims = answer.get("claims") if isinstance(answer, dict) else None
    items = claims if isinstance(claims, list) and claims else [None]
    struck = [{"claim": c, "reasons": ["malformed answer"]} for c in items]
    text = answer.get("answer") if isinstance(answer, dict) else None
    return {
        "answer": text if isinstance(text, str) else None,
        "supported": [],
        "struck": struck,
        "ok": 0,
        "total": len(struck),
    }


def _reasons(claim: dict[str, Any], index: dict[str, dict[str, Any]]) -> list[str]:
    cites = claim["cites"]
    if not cites:
        return ["no citations"]
    reasons: list[str] = []
    for cite in cites:
        record = index.get(cite["id"])
        if record is None:
            reasons.append(f"unknown id {cite['id']}")
        elif len(_norm(cite["quote"])) < MIN_QUOTE:
            reasons.append(f"quote too short for {cite['id']}")
        elif not _quote_in(cite["quote"], searchable(record)):
            reasons.append(f"quote not found in {cite['id']}")
    if reasons:
        return reasons
    motive = _motive_reason(claim)
    if motive:
        return [motive]
    return []


def verify(ledger: list[dict[str, Any]], answer: Any) -> dict[str, Any]:
    """Check each claim's cites against the ledger.

    Returns {"answer", "supported", "struck", "ok", "total"}. `supported` holds the claim
    objects that passed. `struck` holds {"claim": <claim object>, "reasons": [...]} for the
    rest. A malformed answer strikes every claim with `malformed answer` and does not raise.
    Quotes that appear in the named record still match. A claim is still struck when it
    joins two facts with because / so that (or so as to, in order to, in order that) and
    no cited quote connects those facts, with reason `motive not in quotes`.
    """
    if not _well_formed(answer):
        return _malformed(answer)
    index = {r["id"]: r for r in ledger if r.get("kind") in ("event", "decision")}
    supported: list[dict[str, Any]] = []
    struck: list[dict[str, Any]] = []
    for claim in answer["claims"]:
        reasons = _reasons(claim, index)
        if reasons:
            struck.append({"claim": claim, "reasons": reasons})
        else:
            supported.append(claim)
    return {
        "answer": answer["answer"],
        "supported": supported,
        "struck": struck,
        "ok": len(supported),
        "total": len(answer["claims"]),
    }
