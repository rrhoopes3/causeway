import json
from pathlib import Path

import pytest

from causeway.verify import verify

FIXTURE = Path(__file__).parent / "fixtures" / "ledger_slice2.jsonl"


def _ledger():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def _answer(*claims):
    return {"answer": "the answer", "claims": list(claims)}


def _claim(text, *cites):
    return {"text": text, "cites": [{"id": i, "quote": q} for i, q in cites]}


def test_supported_claim_passes():
    result = verify(_ledger(), _answer(_claim("Rick owns it.", ("e4", "Owner: Rick."))))
    assert result["ok"] == 1
    assert result["total"] == 1
    assert result["struck"] == []
    assert result["supported"] == [_claim("Rick owns it.", ("e4", "Owner: Rick."))]
    assert result["answer"] == "the answer"


def test_unknown_id_is_struck():
    result = verify(_ledger(), _answer(_claim("Made up.", ("e99", "anything"))))
    assert result["ok"] == 0
    assert result["struck"][0]["reasons"] == ["unknown id e99"]


def test_quote_not_in_named_record_is_struck():
    result = verify(_ledger(), _answer(_claim("Deletes files.", ("d2", "rm -rf"))))
    assert result["struck"][0]["reasons"] == ["quote not found in d2"]


def test_quote_matching_a_different_record_is_struck():
    result = verify(_ledger(), _answer(_claim("Owner.", ("e3", "Owner: Rick."))))
    assert result["struck"][0]["reasons"] == ["quote not found in e3"]


def test_quote_spanning_a_real_newline_matches():
    result = verify(_ledger(), _answer(_claim("x", ("e4", "Friday.\nOwner: Rick."))))
    assert result["ok"] == 1


def test_quote_spanning_a_two_character_backslash_n_matches():
    result = verify(_ledger(), _answer(_claim("x", ("e4", r"Friday.\nOwner: Rick."))))
    assert result["ok"] == 1


def test_quote_matching_is_casefolded():
    result = verify(_ledger(), _answer(_claim("x", ("e4", "OWNER: rick."))))
    assert result["ok"] == 1


def test_whitespace_runs_collapse():
    result = verify(_ledger(), _answer(_claim("x", ("e4", "ship     the   verifier"))))
    assert result["ok"] == 1


def test_quote_matches_rendered_decision_actions():
    result = verify(
        _ledger(),
        _answer(
            _claim("It read the plan.", ("d2", 'read_file(path="plan-v2.md")')),
            _claim("It wrote summary.md.", ("d3", "summary.md")),
        ),
    )
    assert result["ok"] == 2


def test_quote_matches_decision_text():
    result = verify(_ledger(), _answer(_claim("x", ("d5", "I wrote it to summary.md."))))
    assert result["ok"] == 1


def test_claim_with_no_cites_is_struck():
    result = verify(_ledger(), _answer(_claim("Unsupported.")))
    assert result["ok"] == 0
    assert result["struck"][0]["reasons"] == ["no citations"]


def test_empty_quote_is_too_short():
    result = verify(_ledger(), _answer(_claim("x", ("e4", "   "))))
    assert result["struck"][0]["reasons"] == ["quote too short for e4"]


def test_short_quote_that_matches_is_still_struck():
    result = verify(_ledger(), _answer(_claim("x", ("e4", "Rick"))))
    assert result["struck"][0]["reasons"] == ["quote too short for e4"]


def test_one_bad_cite_strikes_the_whole_claim_and_lists_each_reason():
    result = verify(
        _ledger(),
        _answer(_claim("x", ("e4", "Owner: Rick."), ("e99", "x"), ("d2", "nope, not there"))),
    )
    assert result["ok"] == 0
    assert result["supported"] == []
    assert result["struck"][0]["reasons"] == ["unknown id e99", "quote not found in d2"]


def test_mixed_claims_split_into_supported_and_struck():
    good = _claim("Rick owns it.", ("e4", "Owner: Rick."))
    bad = _claim("Ships in May.", ("e4", "ships in May"))
    result = verify(_ledger(), _answer(good, bad))
    assert result["ok"] == 1
    assert result["total"] == 2
    assert result["supported"] == [good]
    assert result["struck"] == [{"claim": bad, "reasons": ["quote not found in e4"]}]


def test_empty_claims_list_is_well_formed():
    result = verify(_ledger(), _answer())
    assert result == {"answer": "the answer", "supported": [], "struck": [], "ok": 0, "total": 0}


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "just a string",
        ["list"],
        {"claims": []},
        {"answer": "x"},
        {"answer": "x", "claims": "nope"},
        {"answer": 5, "claims": []},
        {"answer": "x", "claims": [{"text": "no cites key"}]},
        {"answer": "x", "claims": [{"text": "t", "cites": "nope"}]},
        {"answer": "x", "claims": [{"text": "t", "cites": [{"id": "e4"}]}]},
        {"answer": "x", "claims": [{"text": "t", "cites": [{"id": "e4", "quote": 3}]}]},
        {"answer": "x", "claims": [{"cites": []}]},
        {"answer": "x", "claims": ["not a dict"]},
    ],
)
def test_malformed_answer_strikes_everything_without_raising(answer):
    result = verify(_ledger(), answer)
    assert result["supported"] == []
    assert result["ok"] == 0
    assert result["struck"]
    assert result["total"] == len(result["struck"])
    for entry in result["struck"]:
        assert entry["reasons"] == ["malformed answer"]


def test_malformed_answer_keeps_claims_and_answer_text_when_present():
    answer = {"answer": "partly", "claims": [{"text": "a"}, {"text": "b", "cites": []}]}
    result = verify(_ledger(), answer)
    assert result["answer"] == "partly"
    assert [entry["claim"] for entry in result["struck"]] == [{"text": "a"}, {"text": "b", "cites": []}]
    assert result["total"] == 2


def test_malformed_answer_with_no_claims_reports_one_struck_entry():
    result = verify(_ledger(), None)
    assert result == {
        "answer": None,
        "supported": [],
        "struck": [{"claim": None, "reasons": ["malformed answer"]}],
        "ok": 0,
        "total": 1,
    }
