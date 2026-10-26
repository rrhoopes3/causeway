"""JSONL ledgers: one header, event or decision record per line (SPEC §4.2)."""

from __future__ import annotations

import json
from pathlib import Path


def dumps(records: list[dict]) -> str:
    """Serialize records as deterministic JSONL: sorted keys, raw UTF-8 characters, LF endings, trailing newline."""
    return "".join(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n" for record in records)


def loads(text: str) -> list[dict]:
    """Parse JSONL text. Lines are split on LF only, since ensure_ascii=False leaves characters such as U+2028 unescaped. Blank lines are skipped."""
    return [json.loads(line) for line in text.split("\n") if line.strip()]


def write(path: str | Path, records: list[dict]) -> None:
    """Write records as UTF-8 bytes so that line endings stay LF on every platform."""
    Path(path).write_bytes(dumps(records).encode("utf-8"))


def read(path: str | Path) -> list[dict]:
    """Read a JSONL ledger written by write()."""
    return loads(Path(path).read_bytes().decode("utf-8"))
