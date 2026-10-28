"""Self-contained HTML viewer for a ledger (`python -m causeway view`).

The page is one file. viewer_template.html holds its CSS and JavaScript, and the ledger goes in as a JSON block.
Ledger text is untrusted (a tool result can carry HTML or script), so the JSON is escaped so that no string in it
can close the block, and the page writes ledger text only with textContent. The page makes no network requests:
its Content-Security-Policy allows only inline script and style.
"""

from __future__ import annotations

import html
import json
import re
from importlib import resources
from pathlib import Path
from typing import Any

from causeway import cli, ledger, render, tagger, verify

TEMPLATE_NAME = "viewer_template.html"
_PLACEHOLDER = re.compile(r"__(TITLE|LEDGER_JSON)__")
_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", " ": "\\u2028", " ": "\\u2029"}


def template_text() -> str:
    """The page template, read from the installed package."""
    return resources.files("causeway").joinpath(TEMPLATE_NAME).read_text(encoding="utf-8")


def embed_json(payload: Any) -> str:
    """Compact, key-sorted JSON that is safe inside a script element. The characters that could end the block or
    start a markup construct (<, >, &) and the two line separators are written as \\u escapes. The result still
    parses to the same value with json.loads."""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "".join(_ESCAPES.get(char, char) for char in text)


def taint_groups(records: list[dict]) -> list[dict]:
    """The watch taints grouped the way cli.render_taints groups them (SPEC 5.5). One entry per decision, watched
    action, source and tag. Each entry's `line` is the same text render_taints prints for it."""
    labels = cli.origins(records)
    groups: list[dict] = []
    for record in records:
        if record.get("kind") != "decision":
            continue
        grouped: dict[tuple, list[str]] = {}
        for entry in record.get("watch") or []:
            base, _, atom = entry["path"].partition("~")
            grouped.setdefault((entry["action"], base, entry["source"], entry["tag"]), []).append(atom)
        for (action_index, base, source, tag), atoms in grouped.items():
            name = record["actions"][action_index]["name"]
            span = cli._atom_span(base, atoms)
            where = f"{source} {labels.get(source, '')}".rstrip() if source else "nowhere in the run"
            groups.append({
                "decision": record["id"],
                "action": action_index,
                "base": base,
                "span": span,
                "source": source,
                "tag": tag,
                "line": f"{cli.WATCH_MARK} {record['id']} {name}.{span} ← {where}  [{tag}]",
            })
    return groups


def load_why(path: str | Path) -> dict[str, Any]:
    """The result written by `causeway why --json`, reduced to what the Explain panel shows. Raises ValueError when
    the file is not such a result."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("supported"), list) or not isinstance(data.get("struck"), list):
        raise ValueError(f"{path}: not a why result (expected the JSON from `causeway why --json`)")
    answer = data.get("answer")
    supported, unproven = [], list(data.get("unproven") or [])
    for claim in data["supported"]:
        # A result written before absence claims were split out still lists them as supported (SPEC 7.3).
        if isinstance(claim, dict) and isinstance(claim.get("text"), str) and verify.is_absence_claim(
            claim["text"], verify.quotes_of(claim)
        ):
            unproven.append({"claim": claim, "reasons": [verify.ABSENCE_REASON]})
        else:
            supported.append(claim)
    return {
        "answer": answer if isinstance(answer, str) else None,
        "supported": supported,
        "unproven": unproven,
        "struck": data["struck"],
        "ok": len(supported),
        "total": data.get("total", len(supported) + len(unproven) + len(data["struck"])),
    }


def build_html(
    records: list[dict],
    *,
    title: str | None = None,
    question: str | None = None,
    why: dict[str, Any] | None = None,
) -> str:
    """The whole page for a ledger. Every record is embedded as-is, plus the derived data the page cannot compute
    without the tagger: origin labels, goal events, the action text it shows, and the taint groups."""
    header = next((record for record in records if record.get("kind") == "header"), {})
    if title is None:
        title = f"Causeway · {header['run_id']}" if header.get("run_id") else "Causeway ledger"
    decisions = [record for record in records if record.get("kind") == "decision"]
    payload = {
        "title": title,
        "question": question,
        "records": records,
        "origins": cli.origins(records),
        "goal": [record["id"] for record in records if record.get("kind") == "event" and tagger.is_goal(record)],
        "shown": {
            record["id"]: " + ".join(render.render_action(action) for action in record["actions"])
            for record in decisions
        },
        "watched": any("watch" in record for record in decisions),
        "taints": taint_groups(records),
        "why": why,
    }
    values = {"TITLE": html.escape(title, quote=True), "LEDGER_JSON": embed_json(payload)}
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template_text())


def write_view(
    ledger_path: str | Path,
    output_path: str | Path,
    *,
    why_path: str | Path | None = None,
    title: str | None = None,
    question: str | None = None,
) -> None:
    """Read a ledger (and an optional why result) and write the page as UTF-8 with LF line endings."""
    records = ledger.read(ledger_path)
    why = load_why(why_path) if why_path is not None else None
    page = build_html(records, title=title, question=question, why=why)
    Path(output_path).write_bytes(page.encode("utf-8"))
