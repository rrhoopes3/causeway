"""Ledger rendering for the model (SPEC 7.1) and the searchable text quotes are checked against (SPEC 7.3)."""

from __future__ import annotations

import json
from typing import Any


def _one_line(text: Any) -> str:
    """Return text with every newline shown as the two characters backslash-n."""
    s = "" if text is None else str(text)
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def render_action(action: dict[str, Any]) -> str:
    """Render one action as name(k=<json value>, ...)."""
    args = action.get("args") or {}
    inner = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())
    return f"{action['name']}({inner})"


def _render_actions(decision: dict[str, Any]) -> str:
    return " + ".join(render_action(a) for a in decision.get("actions") or [])


def _cause_sources(decision: dict[str, Any]) -> list[str]:
    """Unique non-null cause source ids, in order of first appearance."""
    sources: list[str] = []
    for cause in decision.get("causes") or []:
        source = cause.get("source")
        if source and source not in sources:
            sources.append(source)
    return sources


def _render_event(record: dict[str, Any]) -> str:
    head = f"[{record['id']}] step {record['step']} {record['role']}"
    if record.get("name"):
        head += f" {record['name']}"
    return f"{head}: {_one_line(record.get('text'))}"


def _render_decision(record: dict[str, Any]) -> str:
    """Final decisions show their text. For a decision with actions and text, the text
    follows the actions as `text: ...`, because the verifier lets quotes match it and
    the model must be able to see it."""
    head = f"[{record['id']}] step {record['step']} "
    if record.get("final"):
        body = f"final: {_one_line(record.get('text'))}"
    else:
        body = _render_actions(record)
        if record.get("text"):
            body += f"  text: {_one_line(record['text'])}"
    parts = [head + body]
    if record.get("tag"):
        parts.append(f"tag {record['tag']}")
    sources = _cause_sources(record)
    if sources:
        parts.append("← " + ", ".join(sources))
    if record.get("echo_of"):
        parts.append(f"ECHO←{record['echo_of']}")
    return "  ".join(parts)


def render(ledger: list[dict[str, Any]]) -> str:
    """One line per event and decision, joined by newlines. The header is skipped and
    there is no trailing newline."""
    lines: list[str] = []
    for record in ledger:
        kind = record.get("kind")
        if kind == "event":
            lines.append(_render_event(record))
        elif kind == "decision":
            lines.append(_render_decision(record))
        elif kind != "header":
            raise ValueError(f"unknown record kind: {kind!r}")
    return "\n".join(lines)


def searchable(record: dict[str, Any]) -> str:
    """Event: its text. Decision: its text, rendered actions and tag, one per line.
    Empty parts are dropped."""
    kind = record.get("kind")
    if kind == "event":
        return record.get("text") or ""
    if kind == "decision":
        parts = [record.get("text") or "", _render_actions(record), record.get("tag") or ""]
        return "\n".join(p for p in parts if p)
    raise ValueError(f"no searchable text for kind: {kind!r}")
