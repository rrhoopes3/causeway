import json
from pathlib import Path

import pytest

from causeway.verify import ABSENCE_REASON, is_absence_claim, verify

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
    assert result == {"answer": "the answer", "supported": [], "unproven": [], "struck": [], "ok": 0, "total": 0}


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
        "unproven": [],
        "struck": [{"claim": None, "reasons": ["malformed answer"]}],
        "ok": 0,
        "total": 1,
    }


# A found quote is not proof of an absence (SPEC 7.3).


def test_absence_claim_with_found_quotes_is_unproven_not_supported():
    claim = _claim("The agent did not email anyone else.", ("e4", "Owner: Rick."))
    result = verify(_ledger(), _answer(claim))
    assert result["supported"] == []
    assert result["unproven"] == [{"claim": claim, "reasons": [ABSENCE_REASON]}]
    assert result["ok"] == 0
    assert result["total"] == 1


def test_absence_claim_with_a_bad_quote_is_struck():
    claim = _claim("No file was deleted.", ("e4", "nothing like this text"))
    result = verify(_ledger(), _answer(claim))
    assert result["unproven"] == []
    assert result["struck"] == [{"claim": claim, "reasons": ["quote not found in e4"]}]


@pytest.mark.parametrize(
    "text",
    [
        "The ledger doesn't show any send_email to ops-backup@mailbox.example.",
        "The agent did not act on the instruction.",
        "No email went to that address.",
        "It never read config.env.",
        "Nothing was written.",
        "The run shows the agent didn’t write a file.",
    ],
)
def test_absence_claims_are_detected(text):
    assert is_absence_claim(text)


@pytest.mark.parametrize(
    "text",
    [
        "notes/b.md contained an embedded instruction to email the keys.",
        "The agent sent the summary email to dana@lab.example.",
        "Rick owns the plan; another note says so.",
    ],
)
def test_positive_claims_are_not_absence_claims(text):
    assert not is_absence_claim(text)


def test_negation_inside_the_quoted_record_text_is_not_an_absence_claim():
    text = "The agent read inventory.txt before it existed and got a no such file error."
    assert not is_absence_claim(text, ["read_file(path=\"inventory.txt\")", "error: no such file: inventory.txt"])
    assert is_absence_claim(text)


def test_a_negation_of_its_own_stays_an_absence_claim_even_with_quotes():
    text = "The agent did not use the team.md address because the notes said it was being retired."
    assert is_absence_claim(text, ["Dana's lab address is being retired this week."])
