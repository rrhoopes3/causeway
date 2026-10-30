"""Live tests against the real Meta Model API (SPEC 9, 10). They cost money, so they run only with a key and only
the minimum: one `why` run and one rivals decision. Run with: py -3.12 -m pytest -q -m live tests/test_live.py
"""

import json
from pathlib import Path

import pytest

from causeway import cli, rivals, steps
from causeway.client import ChatClient, load_api_key

FIXTURE = Path(__file__).parent / "fixtures" / "explore_basic.json"
QUESTION = "Why did the agent read that file?"
TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List a directory.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                                                     "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a file.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                                                     "required": ["path"]}}},
]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(load_api_key() is None, reason="no MODEL_API_KEY in the environment or ~/.config/causeway/.env"),
]


class _LowEffort:
    """Adds reasoning_effort="low" to every call. rivals.rivals() does not take an effort argument."""

    def __init__(self, inner):
        self._inner = inner

    def complete(self, messages, tools=None, **extra):
        return self._inner.complete(messages, tools=tools, reasoning_effort="low", **extra)


def test_why_end_to_end_on_the_explore_fixture(tmp_path, capsys):
    run = tmp_path / "run.jsonl"
    assert cli.main(["tag", str(FIXTURE), "-o", str(run)]) == 0
    out = tmp_path / "why.json"

    code = cli.main(["why", str(run), QUESTION, "--effort", "low", "--json", str(out)])

    result = json.loads(out.read_text(encoding="utf-8"))
    assert set(result) == {"answer", "supported", "unproven", "struck", "ok", "total", "raw"}
    assert isinstance(result["answer"], str)
    assert len(result["supported"]) == result["ok"]
    assert result["ok"] + len(result["unproven"]) + len(result["struck"]) == result["total"]
    assert result["total"] >= 1
    assert code == (0 if result["ok"] >= 1 else 3)
    assert "quote found for" in capsys.readouterr().out


def test_rivals_on_the_first_decision_only():
    # The fixture has no tools key, so the tool definitions its calls imply are supplied here.
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    before, message = steps.prefixes(data["messages"])[0]
    chat = ChatClient()
    try:
        result = rivals.rivals(before, TOOLS, message, 2, _LowEffort(chat))
    finally:
        chat.close()
    assert sum(result["votes"].values()) == 3
