"""Tools the demo agent can call. Every path is confined to the sandbox root (returns errors, never raises)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from causeway import sam
from causeway.client import APIError, load_api_key

MAX_READ_BYTES = 200_000
OUTBOX = "outbox.jsonl"


class SandboxError(ValueError):
    """A path that is absolute, has a drive, or resolves outside the sandbox."""


def resolve_in_sandbox(sandbox: Path, rel: str) -> Path:
    """Return the absolute path for `rel` under `sandbox`, or raise SandboxError.

    Rejects empty paths, absolute paths, drive letters and UNC paths, and anything that resolves
    outside the sandbox (so `..` and symlinks cannot escape).
    """
    if not isinstance(rel, str) or not rel.strip():
        raise SandboxError("path must be a non-empty string")
    if "\x00" in rel:
        raise SandboxError("path contains a NUL byte")
    win, posix = PureWindowsPath(rel), PurePosixPath(rel)
    if win.drive or win.root or posix.is_absolute() or rel.startswith(("/", "\\")):
        raise SandboxError(f"absolute paths are not allowed: {rel}")
    root = Path(sandbox).resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise SandboxError(f"path escapes the sandbox: {rel}")
    return target


def _rel(sandbox: Path, target: Path) -> str:
    return target.relative_to(Path(sandbox).resolve()).as_posix()


def list_dir(sandbox: Path, path: str) -> str:
    """List a directory inside the sandbox. Subdirectories end with '/'."""
    target = resolve_in_sandbox(sandbox, path)
    if not target.is_dir():
        return f"error: not a directory: {path}"
    names = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    return "\n".join(names) if names else "(empty directory)"


def read_file(sandbox: Path, path: str) -> str:
    """Read a UTF-8 text file inside the sandbox (truncated past 200 KB)."""
    target = resolve_in_sandbox(sandbox, path)
    if not target.is_file():
        return f"error: no such file: {path}"
    data = target.read_bytes()
    truncated = len(data) > MAX_READ_BYTES
    try:
        text = data[:MAX_READ_BYTES].decode("utf-8")
    except UnicodeDecodeError:
        return f"error: not UTF-8 text: {path}"
    return text + ("\n[truncated]" if truncated else "")


def write_file(sandbox: Path, path: str, content: str) -> str:
    """Write a UTF-8 text file inside the sandbox, creating parent folders."""
    if not isinstance(content, str):
        return "error: content must be a string"
    target = resolve_in_sandbox(sandbox, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content.encode('utf-8'))} bytes to {_rel(sandbox, target)}"


def send_email(sandbox: Path, to: str, subject: str, body: str) -> str:
    """Queue an email. Nothing is sent: a JSON line is appended to outbox.jsonl in the sandbox."""
    for name, value in (("to", to), ("subject", subject), ("body", body)):
        if not isinstance(value, str):
            return f"error: {name} must be a string"
    record = {"to": to, "subject": subject, "body": body}
    with (Path(sandbox).resolve() / OUTBOX).open("a", encoding="utf-8") as out:
        out.write(json.dumps(record, ensure_ascii=False) + "\n")
    return "queued"


def segment_image(sandbox: Path, path: str, prompt: str) -> str:
    """Segment `prompt` in an image inside the sandbox with SAM 3.1 and summarise the result."""
    target = resolve_in_sandbox(sandbox, path)
    if not target.is_file():
        return f"error: no such file: {path}"
    key = load_api_key()
    if not key:
        return "error: MODEL_API_KEY is not set, so SAM 3.1 cannot be called"
    try:
        records = sam.segment(str(target), prompt, api_key=key)
    except APIError as err:
        return f"error: SAM 3.1 request failed: HTTP {err.status}: {err.body[:300]}"
    if not records:
        return f'0 x "{prompt}": no matches'
    boxes = [r["box"] for r in records if r["box"] is not None]
    summary = f'{len(records)} x "{prompt}": boxes {json.dumps(boxes)}'
    confidences = [r["confidence"] for r in records if r["confidence"] is not None]
    if confidences:
        summary += f"; confidence {json.dumps(confidences)}"
    return summary


@dataclass(frozen=True)
class Tool:
    """A tool the model can call: its OpenAI function schema and the Python function behind it."""

    name: str
    schema: dict[str, Any]
    fn: Callable[..., str]


def _schema(name: str, description: str, properties: dict[str, dict[str, str]]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {k: {"type": "string", "description": v} for k, v in properties.items()},
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


LIST_DIR = Tool(
    "list_dir",
    _schema("list_dir", "List the entries of a folder in the sandbox.", {"path": "Folder path relative to the sandbox root, e.g. '.' or 'notes'."}),
    list_dir,
)
READ_FILE = Tool(
    "read_file",
    _schema("read_file", "Read a text file in the sandbox.", {"path": "File path relative to the sandbox root."}),
    read_file,
)
WRITE_FILE = Tool(
    "write_file",
    _schema(
        "write_file",
        "Create or overwrite a text file in the sandbox.",
        {"path": "File path relative to the sandbox root.", "content": "Full text to write."},
    ),
    write_file,
)
SEND_EMAIL = Tool(
    "send_email",
    _schema(
        "send_email",
        "Send an email. In this demo it is only queued to the sandbox outbox and never delivered.",
        {"to": "Recipient address.", "subject": "Subject line.", "body": "Plain-text body."},
    ),
    send_email,
)
SEGMENT_IMAGE = Tool(
    "segment_image",
    _schema(
        "segment_image",
        "Find and outline every instance of a described object in an image in the sandbox, using SAM 3.1.",
        {"path": "Image path relative to the sandbox root, e.g. 'desk.jpg'.", "prompt": "Short noun phrase, e.g. 'mug'."},
    ),
    segment_image,
)

ALL: tuple[Tool, ...] = (LIST_DIR, READ_FILE, WRITE_FILE, SEND_EMAIL, SEGMENT_IMAGE)


def execute(sandbox: Path, name: str, arguments: str, tools: tuple[Tool, ...] = ALL) -> str:
    """Run one tool call given its name and JSON-string arguments. Failures come back as 'error: ...'."""
    by_name = {t.name: t for t in tools}
    tool = by_name.get(name)
    if tool is None:
        return f"error: unknown tool: {name}"
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return "error: arguments are not valid JSON"
    if not isinstance(args, dict):
        return "error: arguments must be a JSON object"
    try:
        return tool.fn(Path(sandbox), **args)
    except TypeError as err:
        return f"error: bad arguments for {name}: {err}"
    except SandboxError as err:
        return f"error: {err}"
    except Exception as err:  # the model gets the failure as text; the loop keeps going
        return f"error: {name} failed: {type(err).__name__}: {err}"
