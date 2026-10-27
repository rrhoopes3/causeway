"""Offline verifier for an explanation (SPEC 7.3): every cite must name a real record and quote it.

A quote that really appears still matches as a substring. A claim that then joins two
facts with a motive (`because`, `since`, `so that`, `so as to`, `in order to`,
`in order that`) is struck unless one cited quote joins those same facts with some
connective in that family. A sentence that opens with one of those words is still a motive.
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
# `since` is the same family as `because`: a quote may use either word for one motive.
_CAUSAL = re.compile(r"\b(?:in order that|in order to|so as to|so that|because|since)\b")
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


def _opening_link(after: str) -> tuple[str, str] | None:
    """'cause, effect' after a connective that opens the sentence, as (effect, cause).

    The comma splits the opening clause from the main clause. The pair is stored in the
    same order as a medial 'effect connective cause', so either wording can support the other.
    """
    sentence = _CLAUSE_BREAK.split(after, maxsplit=1)[0]
    cause, separator, effect = sentence.partition(",")
    if not separator:
        return None
    cause = cause.strip(" ,;:")
    effect = effect.strip(" ,;:")
    if not cause or not effect:
        return None
    return effect, cause


def _causal_links(text: str) -> list[tuple[str, str, str]]:
    """Each motive as (marker, effect, cause).

    A medial connective has the effect before it and the cause after it. A connective with
    no clause before it ('Because the address was retired, the agent skipped team.md') uses
    the opening clause as the cause and the main clause as the effect.
    """
    links: list[tuple[str, str, str]] = []
    for match in _CAUSAL.finditer(text):
        before = _clause_before(text[: match.start()])
        after = text[match.end() :]
        if before:
            cause = _clause_after(after)
            if cause:
                links.append((match.group(0), before, cause))
            continue
        opening = _opening_link(after)
        if opening:
            effect, cause = opening
            links.append((match.group(0), effect, cause))
    return links


def _tokens_in_order(needle: list[str], hay: list[str]) -> bool:
    """True when `needle` is a contiguous token run inside `hay`."""
    span = len(needle)
    if span == 0:
        return False
    return any(hay[index : index + span] == needle for index in range(len(hay) - span + 1))


def _clauses_align(claim_side: str, quote_side: str) -> bool:
    """One wording of a fact may add words, but the shorter wording must sit inside the longer one.

    The words have to appear in order, so "did not use" is not grounded by "did use".
    """
    claim_tokens = _tokens(claim_side)
    quote_tokens = _tokens(quote_side)
    if not claim_tokens or not quote_tokens:
        return False
    return _tokens_in_order(claim_tokens, quote_tokens) or _tokens_in_order(
        quote_tokens, claim_tokens
    )


def _quote_links(left: str, right: str, quote: str) -> bool:
    """The quote joins these facts when any family connective has each fact on its matching side."""
    for _marker, quoted_left, quoted_right in _causal_links(quote):
        if _clauses_align(left, quoted_left) and _clauses_align(right, quoted_right):
            return True
    return False


def _motive_reason(claim: dict[str, Any]) -> str | None:
    """A claim may not add a motive the quotes never connect.

    Substring checks have already passed. `because` / `since` / `so that` (also `so as to`,
    `in order to`, `in order that`) between two facts stays supported when one cited quote
    uses any connective in that family with both facts on the matching sides. The claim may
    say `because` where the record says `since`, and it may add words around the quoted fact.
    A sentence that opens with the connective is the same kind of link. A claim that only
    restates a quote keeps that quote's motive and stays supported.
    """
    links = _causal_links(_norm(claim["text"]))
    if not links:
        return None
    quotes = [_norm(cite["quote"]) for cite in claim["cites"]]
    for _marker, left, right in links:
        if not any(_quote_links(left, right, quote) for quote in quotes):
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
    joins two facts with because / since / so that (or so as to, in order to, in order that),
    including when that word opens the sentence, and no cited quote connects those facts
    with any connective in that family. The reason is `motive not in quotes`.
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
