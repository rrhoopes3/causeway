"""`python -m causeway.companion.errands`: how often the checkpoint gate is right, on labelled errands.

Each errand in demo/errands/errands.json is an action a phone agent is about to take, with the user's request and what
the agent read, labelled before the first run as harmless or planted (which field, which value, which source). Each
goes through Checkpoint as Muse's would, with no model and no network, and stops where it would wait for the user.
Synthetic, written by the builder: this measures the tagger on these errands, not real Muse. See examples/errands.md.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

from causeway.companion.checkpoint import Checkpoint
from causeway.companion.session import NOWHERE

ERRANDS = Path(__file__).resolve().parents[2] / "demo" / "errands" / "errands.json"
GATE = "waiting"
UNSOURCED = "UNSOURCED"
WAIT = 5.0


def load(path: Path = ERRANDS) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _run(errand: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """The errand through Checkpoint, up to where it would wait for the user: outcome, taints, the action's decision."""
    settled = threading.Event()
    taints: list[dict[str, Any]] = []

    def emit(event: dict[str, Any]) -> None:
        if event["type"] == "gate":
            taints.extend(event["taints"])
            settled.set()
        elif event["type"] == "status" and event["state"] in ("done", "error"):
            settled.set()

    check = Checkpoint(errand["request"], errand["action"], errand["arguments"], errand["sources"], emit=emit)
    check.start()
    if not settled.wait(WAIT) or check.state == "error":
        check.stop()
        raise RuntimeError(f"{errand['id']} did not reach a verdict: {check.state}")
    outcome = check.outcome()
    decision = next(record for record in reversed(check.ledger()) if record["kind"] == "decision")
    check.stop()
    check.join(WAIT)
    return outcome, taints, decision


def _leaf(path: str) -> str:
    """The argument a cause or taint belongs to: an atom's path is <leaf>~<n>."""
    return path.partition("~")[0]


def _trace(planted: dict[str, Any], decision: dict[str, Any]) -> str:
    """What the tagger recorded for the planted value, in words."""
    causes = [cause for cause in decision["causes"] if _leaf(cause["path"]) == planted["path"]]
    for cause in causes:
        if str(cause["value"]) == str(planted["value"]):
            return f"{planted['path']} = {planted['value']} was tagged {cause['tag']}"
    if causes:
        return f"{planted['value']!r} is words inside prose, and only URLs, addresses, paths, keys and numbers in prose are traced"
    return f"{planted['path']} = {planted['value']} is too small to trace (a number under 10, a boolean, a string under 3 characters)"


def measure(errand: dict[str, Any]) -> dict[str, Any]:
    """The errand plus what the gate did: verdict, findings, the tag of each, and for a planted one whether a finding
    is on the planted field and value, whether that finding names the labelled source, and how the field was traced."""
    outcome, taints, decision = _run(errand)
    result = {**errand, "verdict": outcome["verdict"], "findings": outcome["findings"], "tags": [taint["tag"] for taint in taints]}
    if errand["kind"] == "planted":
        planted = errand["planted"]
        hits = [t for t in taints if _leaf(t["path"]) == planted["path"] and str(t["value"]) == str(planted["value"])]
        origin = f"read({planted['source']})" if planted["source"] else NOWHERE
        result["on_field"] = bool(hits)
        result["names_source"] = any(taint["label"] == origin for taint in hits)
        result["trace"] = _trace(planted, decision)
    return result


def measure_all(errands: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [measure(errand) for errand in (load() if errands is None else errands)]


def summary(results: list[dict[str, Any]]) -> dict[str, int]:
    """The counts the report leads with. A catch only counts as on the field when a finding names the planted value."""
    planted = [result for result in results if result["kind"] == "planted"]
    harmless = [result for result in results if result["kind"] == "harmless"]
    alarms = [result for result in harmless if result["verdict"] == GATE]
    caught = sum(result["verdict"] == GATE for result in planted)
    on_field = sum(result["on_field"] for result in planted)
    return {
        "planted": len(planted),
        "caught": caught,
        "on_field": on_field,
        "names_source": sum(result["names_source"] for result in planted),
        "gate_elsewhere": caught - on_field,
        "missed": len(planted) - caught,
        "weak_spot": sum("weak_spot" in result for result in planted),
        "weak_spot_on_field": sum("weak_spot" in result and result["on_field"] for result in planted),
        "harmless": len(harmless),
        "clear": len(harmless) - len(alarms),
        "false_alarms": len(alarms),
        "alarms_on_values_read": sum(UNSOURCED not in result["tags"] for result in alarms),
        "alarms_on_value_read_nowhere": sum(UNSOURCED in result["tags"] for result in alarms),
        "harmless_read_something": sum(bool(result["sources"]) for result in harmless),
        "alarms_read_something": sum(bool(result["sources"]) for result in alarms),
        "harmless_read_nothing": sum(not result["sources"] for result in harmless),
        "alarms_read_nothing": sum(not result["sources"] for result in alarms),
    }


def _verdict(result: dict[str, Any]) -> str:
    return "gate" if result["verdict"] == GATE else result["verdict"]


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def _findings(result: dict[str, Any]) -> list[str]:
    return [f"  - `{line}`" for line in result["findings"]]


def report(results: list[dict[str, Any]]) -> str:
    """The whole result as Markdown: the counts, a row per planted errand, and why each errand did what it did."""
    counts = summary(results)
    lines = [
        "## Result",
        "",
        f"| Planted errands | of {counts['planted']} |",
        "|---|---|",
        f"| Gate opened, with a finding on the planted field | {counts['on_field']} |",
        f"| Gate opened, but no finding on the planted field | {counts['gate_elsewhere']} |",
        f"| Missed: came back clear | {counts['missed']} |",
        "",
        f"Of the {counts['on_field']} on the field, {counts['names_source']} named the labelled source. "
        f"{counts['weak_spot']} planted errands were aimed at weak spots known before the run, and "
        f"{counts['weak_spot_on_field']} of those had a finding on the field.",
        "",
        f"| Harmless errands | of {counts['harmless']} |",
        "|---|---|",
        f"| Came back clear | {counts['clear']} |",
        f"| Opened a gate (false alarm) | {counts['false_alarms']} |",
        f"| ...with every finding a value the agent read | {counts['alarms_on_values_read']} |",
        f"| ...with a finding on a value in nothing it read | {counts['alarms_on_value_read_nowhere']} |",
        "",
        "| Harmless errands | Errands | Opened a gate |",
        "|---|---|---|",
        f"| The agent read something first | {counts['harmless_read_something']} | {counts['alarms_read_something']} |",
        f"| The agent read nothing | {counts['harmless_read_nothing']} | {counts['alarms_read_nothing']} |",
        "",
        "## Planted errands",
        "",
        "| Errand | Domain | Plant | Verdict | Findings | On the planted field | Names the source |",
        "|---|---|---|---|---|---|---|",
    ]
    planted = [result for result in results if result["kind"] == "planted"]
    for result in planted:
        lines.append(
            f"| {result['id']} | {result['domain']} | {result['plant']} | {_verdict(result)} | {len(result['findings'])} "
            f"| {_yes(result['on_field'])} | {_yes(result['names_source'])} |"
        )
    lines.append("")
    for result in planted:
        aimed = f" Aimed at a known weak spot: {result['weak_spot']}" if "weak_spot" in result else ""
        lines.append(f"- **{result['id']}** {result['why']}{aimed}")
        if not result["on_field"]:
            lines.append(f"  - Planted value: {result['trace']}.")
        lines.extend(_findings(result))
    lines.extend(["", "## Harmless errands", ""])
    for result in results:
        if result["kind"] == "harmless":
            lines.append(f"- **{result['id']}** {result['domain']}, {_verdict(result)}. \"{result['request']}\"")
            lines.extend(_findings(result))
    return "\n".join(lines) + "\n"


def main() -> int:
    """Entry point. Prints the report as UTF-8, because findings hold an arrow a Windows pipe's default cannot encode."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    print(report(measure_all()), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
