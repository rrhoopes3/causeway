"""Command-line interface (SPEC 8).

`tag` turns a transcript into a ledger and `show` prints it as a table. `why` asks the model to explain one question
against a ledger and prints the verified answer. `rivals` re-asks the model at every decision and stores the votes.

Usage errors make argparse exit with SystemExit, as usual. Other failures print to stderr and return 1. For the
model commands, an API failure or a missing key returns 2. `why` returns 0 when at least one claim is supported and
3 when none is.

main(argv, client) takes an optional client, which tests use in place of a network ChatClient. Without one, the model
commands build a ChatClient from --base-url and --model; the key comes from the environment or ~/.config/causeway/.env.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from causeway import explain, ledger, rivals, steps, tagger, transcript
from causeway.client import DEFAULT_BASE_URL, DEFAULT_MODEL, REASONING_EFFORTS, APIError, ChatClient

ACTION_WIDTH = 60
ECHO_BADGE = "ECHO←"
WATCH_MARK = "⚠"
COLUMNS = ("id", "step", "actions", "tag", "echo", "sources")
RIVALS_COLUMN = "margin"
WHY_SUPPORTED = 0
WHY_NONE_SUPPORTED = 3


def format_actions(actions: list[dict]) -> str:
    """Actions as name(key=json_value,...) joined by ' + ', cut to ACTION_WIDTH characters. Final decisions show as (final)."""
    if not actions:
        return "(final)"
    calls = []
    for action in actions:
        arguments = ",".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in action["args"].items())
        calls.append(f"{action['name']}({arguments})")
    text = " + ".join(calls)
    if len(text) > ACTION_WIDTH:
        text = text[: ACTION_WIDTH - 3] + "..."
    return text


def _margin_cell(record: dict) -> str:
    """m=0.20, plus ' near' for a near miss or ' upset' when a rival outvoted the chosen action. Empty without rivals."""
    rivals_record = record.get("rivals")
    if not rivals_record:
        return ""
    cell = f"m={rivals_record['margin']:.2f}"
    if rivals_record.get("upset"):
        cell += " upset"
    elif rivals_record.get("near_miss"):
        cell += " near"
    return cell


def render_table(records: list[dict]) -> str:
    """One row per decision: id, step, actions, tag, echo badge, unique source ids in cause order, and
    the rival margin. The margin column appears only when some decision has rivals."""
    decisions = [record for record in records if record.get("kind") == "decision"]
    with_margin = any(record.get("rivals") for record in decisions)
    header = list(COLUMNS) + ([RIVALS_COLUMN] if with_margin else [])
    rows = [header]
    for record in decisions:
        echo = f"{ECHO_BADGE}{record['echo_of']}" if record.get("echo_of") else ""
        sources = list(dict.fromkeys(cause["source"] for cause in record.get("causes", []) if cause.get("source")))
        tag = record.get("tag") or ""
        row = [
            record["id"],
            str(record["step"]),
            format_actions(record["actions"]),
            f"{WATCH_MARK} {tag}" if record.get("watch") else tag,
            echo,
            ",".join(sources),
        ]
        if with_margin:
            row.append(_margin_cell(record))
        rows.append(row)
    widths = [max(len(row[column]) for row in rows) for column in range(len(header))]
    lines = []
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row[:-1], widths[:-1])] + [row[-1]]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines) + "\n" + render_taints(records)


def origins(records: list[dict]) -> dict[str, str]:
    """Label each tool-result event with the call that produced it, e.g. e5 -> 'read_file(notes/b.md)'.

    A ledger id alone doesn't tell a reader which file or page an argument came from."""
    calls = {}
    for record in records:
        if record.get("kind") == "decision":
            for action in record["actions"]:
                values = [str(value) for _, value in tagger.flatten(action["args"])]
                calls[action.get("call_id")] = f"{action['name']}({', '.join(values)[:48]})"
    labels = {}
    for record in records:
        if record.get("kind") == "event":
            if record["role"] == "tool" and record.get("tool_call_id") in calls:
                labels[record["id"]] = calls[record["tool_call_id"]]
            elif tagger.is_goal(record):
                labels[record["id"]] = "the task"
    return labels


def render_taints(records: list[dict]) -> str:
    """One line per tainted argument of a watched tool (§5.5), naming where the value came from."""
    labels = origins(records)
    lines = []
    for record in records:
        groups: dict[tuple, list[str]] = {}
        for entry in record.get("watch") or []:
            base, _, atom = entry["path"].partition("~")
            key = (entry["action"], base, entry["source"], entry["tag"])
            groups.setdefault(key, []).append(atom)
        for (action_index, base, source, tag), atoms in groups.items():
            name = record["actions"][action_index]["name"]
            where = f"{source} {labels.get(source, '')}".rstrip() if source else "nowhere in the run"
            lines.append(f"{WATCH_MARK} {record['id']} {name}.{_atom_span(base, atoms)} ← {where}  [{tag}]\n")
    return ("\n" + "".join(lines)) if lines else ""


def _atom_span(base: str, atoms: list[str]) -> str:
    """body~0..15 (16 values) for a contiguous run of atoms from one source; body~0,2 otherwise; body for a plain leaf."""
    if atoms == [""]:
        return base
    if len(atoms) == 1:
        return f"{base}~{atoms[0]}"
    numbers = [int(atom) for atom in atoms if atom.isdigit()]
    if len(numbers) == len(atoms) and numbers == list(range(numbers[0], numbers[0] + len(numbers))):
        return f"{base}~{numbers[0]}..{numbers[-1]} ({len(numbers)} values)"
    return f"{base}~{','.join(atoms)}"


def load_transcript(path: str) -> tuple[list[dict], list[dict] | None]:
    """Return (messages, tools). A transcript is a JSON list of messages, or an object whose messages key holds
    that list and whose optional tools key holds the tool definitions (None when absent)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        messages, tools = data.get("messages"), data.get("tools")
    else:
        messages, tools = data, None
    if not isinstance(messages, list) or not all(isinstance(message, dict) for message in messages):
        raise ValueError(f"{path}: expected a list of messages or an object with a 'messages' list")
    if tools is not None and not (isinstance(tools, list) and all(isinstance(tool, dict) for tool in tools)):
        raise ValueError(f"{path}: 'tools' must be a list of tool definitions")
    return messages, tools


def load_messages(path: str) -> list[dict]:
    """The messages of a transcript. See load_transcript."""
    return load_transcript(path)[0]


def format_why(result: dict[str, Any]) -> str:
    """The answer, then one line per claim: ✓ supported with its cited ids, ✗ struck with its reasons. Ends with the
    verified count."""
    answer = result.get("answer")
    lines = [answer if answer else "(no answer: the reply did not match the answer schema)", ""]
    for claim in result["supported"]:
        ids = list(dict.fromkeys(cite["id"] for cite in claim["cites"]))
        lines.append(f"✓ {claim['text']}  [{', '.join(ids)}]")
    for struck in result["struck"]:
        lines.append(f"✗ {_claim_text(struck['claim'])}  — {'; '.join(struck['reasons'])}")
    lines.extend(["", f"verified {result['ok']}/{result['total']} claims"])
    return "\n".join(lines) + "\n"


def _claim_text(claim: Any) -> str:
    """A claim's text. A malformed claim, which the verifier struck, is shown as it arrived."""
    if isinstance(claim, dict) and isinstance(claim.get("text"), str):
        return claim["text"]
    if claim is None:
        return "(no claim)"
    return json.dumps(claim, ensure_ascii=False)


def format_rivals(decisions: list[dict]) -> str:
    """One line per decision: id, margin, and 'near miss' or 'upset' when it is one."""
    lines = []
    for record in decisions:
        rivals_record = record["rivals"]
        line = f"{record['id']}  margin {rivals_record['margin']:.2f}"
        if rivals_record.get("upset"):
            line += "  upset: a rival outvoted what it did"
        elif rivals_record["near_miss"]:
            line += "  near miss"
        lines.append(line + "\n")
    return "".join(lines)


def _fail(exc: BaseException, code: int) -> int:
    print(f"causeway: {exc}", file=sys.stderr)
    return code


def _open_chat(args: argparse.Namespace, client: Any) -> Any:
    """The injected client, else a ChatClient from --base-url and --model. Raises ValueError without a key for the
    Meta API."""
    if client is not None:
        return client
    return ChatClient(base_url=args.base_url, model=args.model)


def _cmd_tag(args: argparse.Namespace, client: Any = None) -> int:
    try:
        records = transcript.to_ledger(
            load_messages(args.transcript), run_id=args.run_id, model=args.model, watch=_watch_set(args)
        )
        ledger.write(args.output, records)
    except (OSError, ValueError) as exc:
        return _fail(exc, 1)
    return 0


def _cmd_show(args: argparse.Namespace, client: Any = None) -> int:
    try:
        records = ledger.read(args.ledger)
    except (OSError, ValueError) as exc:
        return _fail(exc, 1)
    sys.stdout.write(render_table(records))
    return 0


def _cmd_why(args: argparse.Namespace, client: Any = None) -> int:
    try:
        records = ledger.read(args.ledger)
    except (OSError, ValueError) as exc:
        return _fail(exc, 1)
    try:
        chat = _open_chat(args, client)
    except ValueError as exc:
        return _fail(exc, 2)
    try:
        result = explain.why(records, args.question, chat, reasoning_effort=args.effort)
    except (APIError, httpx.HTTPError) as exc:
        return _fail(exc, 2)
    except ValueError as exc:
        return _fail(exc, 1)
    finally:
        if client is None:
            chat.close()
    if args.json_out:
        try:
            Path(args.json_out).write_bytes(
                (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            )
        except OSError as exc:
            return _fail(exc, 1)
    sys.stdout.write(format_why(result))
    return WHY_SUPPORTED if result["ok"] >= 1 else WHY_NONE_SUPPORTED


def _cmd_rivals(args: argparse.Namespace, client: Any = None) -> int:
    try:
        messages, tools = load_transcript(args.transcript)
        records = transcript.to_ledger(messages, run_id="r-1", model=args.model, watch=_watch_set(args))
        decisions = [record for record in records if record["kind"] == "decision"]
        jobs = list(zip(decisions, steps.prefixes(messages), strict=True))
    except (OSError, ValueError) as exc:
        return _fail(exc, 1)
    try:
        chat = _open_chat(args, client)
    except ValueError as exc:
        return _fail(exc, 2)
    try:
        for record, (before, message) in jobs:
            record["rivals"] = rivals.rivals(before, tools, message, args.k, chat, reasoning_effort=args.effort)
    except (APIError, httpx.HTTPError) as exc:
        return _fail(exc, 2)
    finally:
        if client is None:
            chat.close()
    try:
        ledger.write(args.output, records)
    except OSError as exc:
        return _fail(exc, 1)
    sys.stdout.write(format_rivals(decisions))
    return 0


def _cmd_view(args: argparse.Namespace, client: Any = None) -> int:
    from causeway import view  # local: view.py imports this module's helpers

    try:
        view.write_view(args.ledger, args.output, why_path=args.why, title=args.title, question=args.question)
    except (OSError, ValueError) as exc:
        return _fail(exc, 1)
    return 0


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _watch_set(args: argparse.Namespace) -> list[str]:
    """Tool names from --watch a,b,c (§5.5)."""
    return [name.strip() for name in (args.watch or "").split(",") if name.strip()]


def _add_watch_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--watch", metavar="TOOL,TOOL", default="", help="flag arguments of these tools that the task didn't supply")


def _add_model_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="chat API base URL (default: the Meta Model API)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="model name sent with each request")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="causeway", description="See why your agent acted.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    tag = commands.add_parser("tag", help="turn a transcript into a tagged ledger")
    tag.add_argument("transcript", help="JSON list of messages, or an object with a messages list")
    tag.add_argument("-o", "--output", required=True, help="JSONL ledger to write")
    tag.add_argument("--run-id", default="r-1")
    tag.add_argument("--model", default="unknown")
    _add_watch_flag(tag)
    tag.set_defaults(func=_cmd_tag)

    show = commands.add_parser("show", help="print a ledger as a table, one row per decision")
    show.add_argument("ledger", help="JSONL ledger written by tag")
    show.set_defaults(func=_cmd_show)

    why = commands.add_parser("why", help="ask the model why the agent acted; the answer is checked against the ledger")
    why.add_argument("ledger", help="JSONL ledger written by tag")
    why.add_argument("question", help="what to ask about the run")
    _add_model_flags(why)
    why.add_argument("--effort", choices=REASONING_EFFORTS, default="high", help="reasoning_effort for the request")
    why.add_argument("--json", dest="json_out", metavar="OUT.json", help="also write the full result as JSON")
    why.set_defaults(func=_cmd_why)

    rivals_cmd = commands.add_parser("rivals", help="re-ask the model at every decision and record the votes")
    rivals_cmd.add_argument("transcript", help="transcript JSON: a messages list, optionally with tools")
    rivals_cmd.add_argument("-k", dest="k", type=_positive_int, default=4, help="extra samples per decision (default 4)")
    rivals_cmd.add_argument("-o", "--output", required=True, help="JSONL ledger to write, with rivals filled in")
    _add_model_flags(rivals_cmd)
    rivals_cmd.add_argument("--effort", choices=REASONING_EFFORTS, default="low", help="reasoning_effort for each sample (default low: rivals cost k calls per step)")
    _add_watch_flag(rivals_cmd)
    rivals_cmd.set_defaults(func=_cmd_rivals)

    view_cmd = commands.add_parser("view", help="write one self-contained HTML page for a ledger")
    view_cmd.add_argument("ledger", help="JSONL ledger written by tag or rivals")
    view_cmd.add_argument("-o", "--output", required=True, help="HTML file to write")
    view_cmd.add_argument("--why", metavar="WHY.json", help="result of `why --json`, shown in an Explain panel")
    view_cmd.add_argument("--question", help="the question asked of `why`, shown in the Explain panel")
    view_cmd.add_argument("--title", help="page title (default: the run id)")
    view_cmd.set_defaults(func=_cmd_view)
    return parser


def main(argv: Sequence[str] | None = None, client: Any = None) -> int:
    """Run the CLI with argv (default: sys.argv[1:]) and return the exit code.

    client, when given, replaces the network ChatClient for `why` and `rivals`. Tests use this seam.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    return args.func(args, client)
