"""Offline verifier for an explanation (SPEC 7.3): every cite must name a real record and quote it."""

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
    return reasons


def verify(ledger: list[dict[str, Any]], answer: Any) -> dict[str, Any]:
    """Check each claim's cites against the ledger.

    Returns {"answer", "supported", "struck", "ok", "total"}. `supported` holds the claim
    objects that passed. `struck` holds {"claim": <claim object>, "reasons": [...]} for the
    rest. A malformed answer strikes every claim with `malformed answer` and does not raise.
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
