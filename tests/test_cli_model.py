"""Offline tests for the model-backed CLI commands `why` and `rivals`, and for `show` with rivals.

Every test injects a FakeClient (or a stub that raises), so nothing touches the network (SPEC 10).
"""

import json
from pathlib import Path

import pytest

from causeway import ledger, steps
from causeway.client import APIError, FakeClient
from causeway.cli import format_why, main

FIXTURES = Path(__file__).parent / "fixtures"
EXPLORE = FIXTURES / "explore_basic.json"
QUESTION = "Why did the agent read that file?"
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]


def _response(content):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


def _answer_response(answer, claims):
    return _response(json.dumps({"answer": answer, "claims": claims}, ensure_ascii=False))


def _chat_response(message):
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


class FailingClient:
    """Raises the error an unreachable or rejecting API would produce."""

    def __init__(self, error):
        self.error = error

    def complete(self, messages, **kwargs):
        raise self.error


def _tag_explore(tmp_path):
    run = tmp_path / "run.jsonl"
    assert main(["tag", str(EXPLORE), "-o", str(run)]) == 0
    return run


def _mixed_answer():
    """Two supported claims (one cites a decision, one an event) and one struck for an unknown id."""
    return _answer_response(
        "The agent read plan-v2.md because you asked which plan is newest.",
        [
            {"text": "You asked which plan is newest.", "cites": [{"id": "e2", "quote": "which plan is newest"}]},
            {"text": "It first listed /work/notes.", "cites": [{"id": "d1", "quote": 'list_dir(path="/work/notes")'}]},
            {"text": "The agent read notes.md out of habit.", "cites": [{"id": "e99", "quote": "out of habit"}]},
        ],
    )


# why: output formatting and exit codes


def test_why_prints_answer_checks_strikes_and_total_then_exits_0(tmp_path, capsys):
    run = _tag_explore(tmp_path)
    fake = FakeClient([_mixed_answer()])
    assert main(["why", str(run), QUESTION], client=fake) == 0
    assert capsys.readouterr().out == (
        "The agent read plan-v2.md because you asked which plan is newest.\n"
        "\n"
        "✓ quote found: You asked which plan is newest.  [e2]\n"
        "✓ quote found: It first listed /work/notes.  [d1]\n"
        "✗ The agent read notes.md out of habit.  — unknown id e99\n"
        "\n"
        "quote found for 2/3 claims\n"
    )


def test_why_exits_3_when_no_claim_is_supported(tmp_path, capsys):
    run = _tag_explore(tmp_path)
    answer = _answer_response(
        "Probably habit.",
        [
            {"text": "Out of habit.", "cites": []},
            {"text": "Made up.", "cites": [{"id": "e99", "quote": "nothing like this"}]},
        ],
    )
    assert main(["why", str(run), QUESTION], client=FakeClient([answer])) == 3
    out = capsys.readouterr().out
    assert "✗ Out of habit.  — no citations" in out
    assert "✗ Made up.  — unknown id e99" in out
    assert out.endswith("quote found for 0/2 claims\n")


def test_why_with_no_claims_at_all_exits_3(tmp_path, capsys):
    run = _tag_explore(tmp_path)
    assert main(["why", str(run), QUESTION], client=FakeClient([_answer_response("No idea.", [])])) == 3
    assert capsys.readouterr().out.endswith("quote found for 0/0 claims\n")


def test_why_reply_that_is_not_json_is_a_malformed_answer_exit_3(tmp_path, capsys):
    run = _tag_explore(tmp_path)
    assert main(["why", str(run), QUESTION], client=FakeClient([_response("not json at all")])) == 3
    out = capsys.readouterr().out
    assert "(no answer: the reply did not match the answer schema)" in out
    assert "✗ (no claim)  — malformed answer" in out
    assert out.endswith("quote found for 0/1 claims\n")


def test_why_sends_the_question_and_requested_effort(tmp_path):
    run = _tag_explore(tmp_path)
    fake = FakeClient([_mixed_answer()])
    main(["why", str(run), QUESTION, "--effort", "low"], client=fake)
    call = fake.calls[0]
    assert call["reasoning_effort"] == "low"
    assert call["response_format"]["json_schema"]["strict"] is True
    assert QUESTION in call["messages"][-1]["content"]


def test_why_default_effort_is_high(tmp_path):
    run = _tag_explore(tmp_path)
    fake = FakeClient([_mixed_answer()])
    main(["why", str(run), QUESTION], client=fake)
    assert fake.calls[0]["reasoning_effort"] == "high"


def test_why_json_writes_the_full_result_as_utf8_indent_2(tmp_path, capsys):
    run = _tag_explore(tmp_path)
    answer = _answer_response("Le café.", [{"text": "Café order.", "cites": [{"id": "e2", "quote": "tell me which plan"}]}])
    out_path = tmp_path / "why.json"
    assert main(["why", str(run), QUESTION, "--json", str(out_path)], client=FakeClient([answer])) == 0
    raw = out_path.read_bytes().decode("utf-8")
    assert '\n  "answer": "Le café."' in raw
    result = json.loads(raw)
    assert result["ok"] == 1 and result["total"] == 1
    assert result["supported"][0]["text"] == "Café order."
    assert set(result) == {"answer", "supported", "unproven", "struck", "ok", "total", "raw"}


def test_why_json_is_written_even_when_no_claim_is_supported(tmp_path):
    run = _tag_explore(tmp_path)
    out_path = tmp_path / "why.json"
    code = main(["why", str(run), QUESTION, "--json", str(out_path)], client=FakeClient([_answer_response("x", [])]))
    assert code == 3
    assert json.loads(out_path.read_text(encoding="utf-8"))["total"] == 0


def test_why_on_missing_ledger_returns_1(tmp_path, capsys):
    assert main(["why", str(tmp_path / "missing.jsonl"), QUESTION], client=FakeClient([])) == 1
    assert "missing.jsonl" in capsys.readouterr().err


# why: API errors


def test_why_api_error_exits_2_and_prints_the_message(tmp_path, capsys):
    run = _tag_explore(tmp_path)
    client = FailingClient(APIError(503, "service unavailable"))
    assert main(["why", str(run), QUESTION], client=client) == 2
    captured = capsys.readouterr()
    assert captured.err == "causeway: chat API returned HTTP 503: service unavailable\n"
    assert captured.out == ""


def test_why_writes_no_json_on_api_error(tmp_path):
    run = _tag_explore(tmp_path)
    out_path = tmp_path / "why.json"
    main(["why", str(run), QUESTION, "--json", str(out_path)], client=FailingClient(APIError(500, "boom")))
    assert not out_path.exists()


def test_format_why_on_a_hand_built_result():
    result = {
        "answer": "Yes.",
        "supported": [{"text": "A.", "cites": [{"id": "e1", "quote": "x"}, {"id": "e1", "quote": "y"}, {"id": "d2", "quote": "z"}]}],
        "struck": [{"claim": {"text": "B."}, "reasons": ["quote too short for e1", "quote not found in d2"]}],
        "ok": 1,
        "total": 2,
    }
    assert format_why(result) == (
        "Yes.\n\n✓ quote found: A.  [e1, d2]\n✗ B.  — quote too short for e1; quote not found in d2\n\nquote found for 1/2 claims\n"
    )


def test_format_why_shows_an_absence_claim_as_not_proof():
    result = {
        "answer": "No.",
        "supported": [],
        "unproven": [{"claim": {"text": "No mail went to x.", "cites": []}, "reasons": ["claims an absence"]}],
        "struck": [],
        "ok": 0,
        "total": 1,
    }
    out = format_why(result)
    assert "? not proof: No mail went to x.  (claims an absence)" in out
    assert "verified" not in out
    assert out.endswith("quote found for 0/1 claims\n")


# rivals


def _explore_assistants():
    return [m for m in json.loads(EXPLORE.read_text(encoding="utf-8"))["messages"] if m["role"] == "assistant"]


def _scripted_rivals_client():
    """Five decisions, two rival calls each. Decision 2 splits 2 to 1 and decision 5 splits 2 to 1; the rest are
    unanimous."""
    d1, d2, d3, d4, d5 = _explore_assistants()
    final = {"role": "assistant", "content": "A plain answer."}
    script = [
        d1, d1,
        d2, final,
        d3, d3,
        d4, d4,
        d5, d2,
    ]
    return FakeClient([_chat_response(message) for message in script])


def test_rivals_fills_every_decision_and_writes_a_valid_ledger(tmp_path, capsys):
    out = tmp_path / "rivals.jsonl"
    fake = _scripted_rivals_client()
    assert main(["rivals", str(EXPLORE), "-k", "2", "-o", str(out)], client=fake) == 0

    records = ledger.read(out)
    assert records[0]["kind"] == "header"
    decisions = [r for r in records if r["kind"] == "decision"]
    assert [r["id"] for r in decisions] == ["d1", "d2", "d3", "d4", "d5"]
    for record in decisions:
        assert record["rivals"] is not None
        assert record["rivals"]["k"] == 2
        assert sum(record["rivals"]["votes"].values()) == 3
    assert [r["rivals"]["near_miss"] for r in decisions] == [False, True, False, False, True]
    assert decisions[1]["rivals"]["margin"] == pytest.approx(1 / 3)

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "d1  margin 1.00",
        "d2  margin 0.33  near miss",
        "d3  margin 1.00",
        "d4  margin 1.00",
        "d5  margin 0.33  near miss",
    ]


def test_rivals_changes_only_the_rivals_field_of_the_tagged_ledger(tmp_path):
    out = tmp_path / "rivals.jsonl"
    main(["rivals", str(EXPLORE), "-k", "2", "-o", str(out)], client=_scripted_rivals_client())
    tagged = tmp_path / "tagged.jsonl"
    main(["tag", str(EXPLORE), "-o", str(tagged)])
    plain = ledger.read(tagged)[1:]
    filled = ledger.read(out)[1:]
    assert len(plain) == len(filled) == 11  # six events and five decisions
    for before, after in zip(plain, filled, strict=True):
        if before["kind"] == "decision":
            after = {**after, "rivals": None}
        assert before == after


def test_rivals_sends_the_prefix_before_each_decision(tmp_path):
    out = tmp_path / "rivals.jsonl"
    fake = _scripted_rivals_client()
    main(["rivals", str(EXPLORE), "-k", "2", "-o", str(out)], client=fake)
    messages = json.loads(EXPLORE.read_text(encoding="utf-8"))["messages"]
    assert len(fake.calls) == 10
    for index, (before, _) in enumerate(steps.prefixes(messages)):
        for sample in fake.calls[2 * index : 2 * index + 2]:
            assert sample["messages"] == before


def test_rivals_passes_the_transcript_tools_to_every_call(tmp_path):
    transcript_path = tmp_path / "with_tools.json"
    data = json.loads(EXPLORE.read_text(encoding="utf-8"))
    transcript_path.write_text(json.dumps({"messages": data["messages"], "tools": TOOLS}), encoding="utf-8")
    fake = _scripted_rivals_client()
    assert main(["rivals", str(transcript_path), "-k", "2", "-o", str(tmp_path / "r.jsonl")], client=fake) == 0
    assert all(call["tools"] == TOOLS for call in fake.calls)


def test_rivals_api_error_exits_2_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "rivals.jsonl"
    code = main(["rivals", str(EXPLORE), "-k", "2", "-o", str(out)], client=FailingClient(APIError(429, "slow down")))
    assert code == 2
    assert "chat API returned HTTP 429: slow down" in capsys.readouterr().err
    assert not out.exists()


def test_rivals_rejects_k_below_one_as_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as info:
        main(["rivals", str(EXPLORE), "-k", "0", "-o", str(tmp_path / "r.jsonl")], client=FakeClient([]))
    assert info.value.code == 2


def test_rivals_with_a_malformed_tools_field_returns_1(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"messages": [{"role": "user", "content": "hi"}], "tools": "read_file"}), encoding="utf-8")
    assert main(["rivals", str(bad), "-k", "1", "-o", str(tmp_path / "r.jsonl")], client=FakeClient([])) == 1
    assert "tools" in capsys.readouterr().err


# show with rivals


def test_show_adds_a_margin_column_with_near_marks(tmp_path, capsys):
    out = tmp_path / "rivals.jsonl"
    main(["rivals", str(EXPLORE), "-k", "2", "-o", str(out)], client=_scripted_rivals_client())
    capsys.readouterr()
    assert main(["show", str(out)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[-1] == "margin"
    rows = {line.split()[0]: line for line in lines[1:]}
    assert rows["d1"].split()[-1] == "m=1.00"
    assert rows["d2"].split()[-2:] == ["m=0.33", "near"]
    assert rows["d3"].split()[-1] == "m=1.00"
    assert rows["d5"].split()[-2:] == ["m=0.33", "near"]


def test_show_has_no_margin_column_without_rivals(tmp_path, capsys):
    out = tmp_path / "plain.jsonl"
    main(["tag", str(EXPLORE), "-o", str(out)])
    capsys.readouterr()
    main(["show", str(out)])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[-1] == "sources"
    assert "margin" not in lines[0]
