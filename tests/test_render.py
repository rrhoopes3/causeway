import json
from pathlib import Path

from causeway.render import render, render_action, searchable

FIXTURE = Path(__file__).parent / "fixtures" / "ledger_slice2.jsonl"


def _ledger():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def _by_id(record_id):
    return next(r for r in _ledger() if r.get("id") == record_id)


def _line(prefix):
    return next(line for line in render(_ledger()).split("\n") if line.startswith(prefix))


def test_header_is_skipped():
    text = render(_ledger())
    assert "run_id" not in text
    assert "header" not in text
    assert len(text.split("\n")) == 11


def test_event_lines():
    assert _line("[e1]") == "[e1] step 1 system: You are a file assistant. Use the tools."
    assert _line("[e3]") == "[e3] step 2 tool list_dir: notes.md\\nplan-v2.md\\n"
    assert _line("[e4]") == "[e4] step 3 tool read_file: Plan v2: ship the verifier by Friday.\\nOwner: Rick."
    assert _line("[e5]") == "[e5] step 4 tool write_file: Wrote summary.md"


def test_decision_lines_with_sources_and_echo():
    assert _line("[d2]") == '[d2] step 2 read_file(path="plan-v2.md")  tag IN  ← e3'
    assert _line("[d3]") == (
        '[d3] step 3 write_file(path="summary.md", content="Plan v2 ships the verifier by Friday.")'
        "  tag IN  ← e2, e4"
    )
    assert _line("[d4]") == '[d4] step 4 read_file(path="plan-v2.md")  tag CARRY·2  ← e3  ECHO←d2'


def test_decision_without_sources_has_no_arrow():
    assert _line("[d1]") == '[d1] step 1 list_dir(path=".")  tag LIT'


def test_final_decision_line():
    assert _line("[d5]") == (
        "[d5] step 5 final: The plan ships the verifier by Friday. I wrote it to summary.md.  tag FINAL"
    )


def test_records_render_in_order():
    ids = [line.split("]")[0].lstrip("[") for line in render(_ledger()).split("\n")]
    assert ids == ["e1", "e2", "d1", "e3", "d2", "e4", "d3", "e5", "d4", "e6", "d5"]


def test_render_has_no_trailing_newline():
    assert not render(_ledger()).endswith("\n")


def test_render_empty_ledger_is_empty_string():
    assert render([]) == ""
    assert render([{"kind": "header", "run_id": "r"}]) == ""


def test_render_action_keeps_non_ascii_and_argument_order():
    action = {"name": "say", "args": {"text": "café", "n": 3, "ok": True}}
    assert render_action(action) == 'say(text="café", n=3, ok=true)'


def test_render_joins_multiple_actions_with_plus():
    decision = {
        "kind": "decision",
        "id": "d9",
        "step": 6,
        "text": None,
        "actions": [
            {"call_id": "a", "name": "list_dir", "args": {"path": "src"}},
            {"call_id": "b", "name": "read_file", "args": {"path": "README.md"}},
        ],
        "final": False,
        "causes": [],
        "tag": "LIT",
        "echo_of": None,
    }
    assert render([decision]) == '[d9] step 6 list_dir(path="src") + read_file(path="README.md")  tag LIT'


def test_unique_sources_in_order():
    decision = {
        "kind": "decision",
        "id": "d7",
        "step": 3,
        "text": None,
        "actions": [{"call_id": "a", "name": "x", "args": {"p": "abcdef", "q": "ghijkl", "r": "mnopqr"}}],
        "final": False,
        "causes": [
            {"action": 0, "path": "p", "value": "abcdef", "tag": "IN", "source": "e4", "match": "exact"},
            {"action": 0, "path": "q", "value": "ghijkl", "tag": "IN", "source": "e2", "match": "exact"},
            {"action": 0, "path": "r", "value": "mnopqr", "tag": "IN", "source": "e4", "match": "exact"},
        ],
        "tag": "IN",
        "echo_of": None,
        "rivals": None,
    }
    assert render([decision]).endswith("tag IN  ← e4, e2")


def test_unsourced_cause_has_no_arrow():
    decision = {
        "kind": "decision",
        "id": "d7",
        "step": 3,
        "text": None,
        "actions": [{"call_id": "a", "name": "x", "args": {"p": "abcdef"}}],
        "final": False,
        "causes": [{"action": 0, "path": "p", "value": "abcdef", "tag": "UNSOURCED", "source": None, "match": None}],
        "tag": "UNSOURCED",
        "echo_of": None,
        "rivals": None,
    }
    assert render([decision]) == '[d7] step 3 x(p="abcdef")  tag UNSOURCED'


def test_non_final_decision_text_is_shown_after_actions():
    decision = {
        "kind": "decision",
        "id": "d1",
        "step": 1,
        "text": "Let me look.",
        "actions": [{"call_id": "a", "name": "list_dir", "args": {"path": "."}}],
        "final": False,
        "causes": [],
        "tag": "LIT",
        "echo_of": None,
    }
    assert render([decision]) == '[d1] step 1 list_dir(path=".")  text: Let me look.  tag LIT'


def test_event_newlines_crlf_and_cr_and_no_name():
    event = {"kind": "event", "id": "e1", "step": 1, "role": "user", "text": "a\r\nb\rc\nd"}
    assert render([event]) == "[e1] step 1 user: a\\nb\\nc\\nd"


def test_render_rejects_unknown_kind():
    try:
        render([{"kind": "mystery"}])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_searchable_event_is_its_text():
    assert searchable(_by_id("e3")) == "notes.md\nplan-v2.md\n"


def test_searchable_decision_is_text_actions_and_tag():
    assert searchable(_by_id("d2")) == 'read_file(path="plan-v2.md")\nIN'


def test_searchable_final_decision_includes_text_and_tag():
    assert searchable(_by_id("d5")) == (
        "The plan ships the verifier by Friday. I wrote it to summary.md.\nFINAL"
    )
