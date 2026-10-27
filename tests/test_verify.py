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


def _event(event_id: str, text: str) -> dict:
    return {"kind": "event", "id": event_id, "step": 1, "role": "tool", "text": text}


@pytest.mark.parametrize(
    ("text", "cites"),
    [
        (
            "The agent did not use the team.md address because the Week 42 notes said "
            "Dana's lab address was being retired, even though the original task said "
            "the address is in team.md.",
            (
                ("e7", "Dana's lab address is being retired this week."),
                ("e2", "whose address is in team.md"),
            ),
        ),
        (
            "It read the plan so that it could ship the summary.",
            (("e1", "Read the plan."), ("e2", "Ship the summary.")),
        ),
        (
            "It read the plan so as to ship the summary.",
            (("e1", "Read the plan."), ("e2", "Ship the summary.")),
        ),
        (
            "It read the plan in order to ship the summary.",
            (("e1", "Read the plan."), ("e2", "Ship the summary.")),
        ),
        (
            "It read the plan in order that the summary could ship.",
            (("e1", "Read the plan."), ("e2", "Ship the summary.")),
        ),
        (
            "It read the plan since the notes listed it.",
            (("e1", "Read the plan."), ("e2", "The notes listed it.")),
        ),
    ],
)
def test_causal_word_joining_unconnected_quotes_is_not_supported(text, cites):
    """Both facts are quoted. The because / since / so that that joins them is not."""
    ledger = [_event(event_id, quote) for event_id, quote in cites]
    claim = _claim(text, *cites)
    result = verify(ledger, _answer(claim))
    assert result["ok"] == 0
    assert result["supported"] == []
    assert result["struck"] == [{"claim": claim, "reasons": ["motive not in quotes"]}]


def test_negated_fact_is_not_the_motive_in_a_positive_quote():
    quote = "The agent did use the team.md address because the note retired the address."
    ledger = [_event("e1", quote)]
    claim = _claim(
        "The agent did not use the team.md address because the note retired the address.",
        ("e1", quote),
    )
    result = verify(ledger, _answer(claim))
    assert result["supported"] == []
    assert result["struck"] == [{"claim": claim, "reasons": ["motive not in quotes"]}]


def test_because_in_a_different_quote_does_not_connect_the_facts():
    ledger = [
        _event("e1", "The agent skipped team.md."),
        _event("e2", "The address was retired because the domain lapsed."),
    ]
    claim = _claim(
        "The agent skipped team.md because the address was retired.",
        ("e1", "The agent skipped team.md."),
        ("e2", "The address was retired because the domain lapsed."),
    )
    result = verify(ledger, _answer(claim))
    assert result["struck"] == [{"claim": claim, "reasons": ["motive not in quotes"]}]


@pytest.mark.parametrize(
    "text",
    [
        "Owner: Rick.",
        "The agent skipped team.md because the note said the address was retired.",
        "It read the plan so that it could ship the summary.",
        "It read the plan so as to ship the summary.",
        "It read the plan in order to ship the summary.",
        "It read the plan in order that the summary could ship.",
        "The agent skipped team.md since the address was retired.",
        "Because the address was retired, the agent skipped team.md.",
    ],
)
def test_claim_that_only_restates_its_quote_stays_supported(text):
    ledger = [_event("e1", text)]
    claim = _claim(text, ("e1", text))
    result = verify(ledger, _answer(claim))
    assert result["ok"] == 1
    assert result["struck"] == []
    assert result["supported"] == [claim]


def test_since_quote_supports_a_because_claim_about_the_same_facts():
    """The claim may say because, and add words, when the quote joins the same facts with since."""
    text = (
        "The used address is an updated address from Week 42 notes "
        "because team.md's dana@lab.example is being retired."
    )
    quote = "updated address from Week 42 notes, since team.md's dana@lab.example is being retired"
    claim = _claim(text, ("d5", quote))
    result = verify([_event("d5", quote)], _answer(claim))
    assert result["ok"] == 1
    assert result["struck"] == []
    assert result["supported"] == [claim]


@pytest.mark.parametrize(
    "connective",
    ["because", "since", "so that", "so as to", "in order to", "in order that"],
)
def test_any_family_connective_supports_the_same_two_facts(connective):
    quote = f"The agent skipped team.md {connective} the address was retired."
    claim = _claim(
        "The agent skipped team.md because the address was retired.",
        ("e1", quote),
    )
    result = verify([_event("e1", quote)], _answer(claim))
    assert result["struck"] == []
    assert result["supported"] == [claim]


def test_since_in_a_different_quote_does_not_connect_the_facts():
    ledger = [
        _event("e1", "The agent skipped team.md."),
        _event("e2", "The address was retired since the domain lapsed."),
    ]
    claim = _claim(
        "The agent skipped team.md because the address was retired.",
        ("e1", "The agent skipped team.md."),
        ("e2", "The address was retired since the domain lapsed."),
    )
    result = verify(ledger, _answer(claim))
    assert result["struck"] == [{"claim": claim, "reasons": ["motive not in quotes"]}]


def test_reversed_sides_are_not_the_same_motive():
    quote = "The address was retired because the agent skipped team.md."
    claim = _claim(
        "The agent skipped team.md because the address was retired.",
        ("e1", quote),
    )
    result = verify([_event("e1", quote)], _answer(claim))
    assert result["struck"] == [{"claim": claim, "reasons": ["motive not in quotes"]}]


@pytest.mark.parametrize(
    "connective",
    ["Because", "Since", "So that", "So as to", "In order to", "In order that"],
)
def test_leading_connective_is_struck_when_quotes_do_not_join_the_clauses(connective):
    """No clause before the connective used to skip the check. The opening clause is one side."""
    text = f"{connective} the address was retired, the agent skipped team.md."
    cites = (
        ("e1", "The address was retired."),
        ("e2", "The agent skipped team.md."),
    )
    claim = _claim(text, *cites)
    ledger = [_event(event_id, quote) for event_id, quote in cites]
    result = verify(ledger, _answer(claim))
    assert result["ok"] == 0
    assert result["supported"] == []
    assert result["struck"] == [{"claim": claim, "reasons": ["motive not in quotes"]}]


@pytest.mark.parametrize(
    "connective",
    ["because", "since", "so that", "so as to", "in order to", "in order that"],
)
def test_leading_because_is_supported_when_a_quote_joins_the_same_facts(connective):
    text = "Because the address was retired, the agent skipped team.md."
    quote = f"The agent skipped team.md {connective} the address was retired."
    claim = _claim(text, ("e1", quote))
    result = verify([_event("e1", quote)], _answer(claim))
    assert result["struck"] == []
    assert result["supported"] == [claim]


def test_leading_because_does_not_match_a_reversed_quote():
    text = "Because the address was retired, the agent skipped team.md."
    quote = "Because the agent skipped team.md, the address was retired."
    claim = _claim(text, ("e1", quote))
    result = verify([_event("e1", quote)], _answer(claim))
    assert result["struck"] == [{"claim": claim, "reasons": ["motive not in quotes"]}]


def test_missing_quote_is_still_quote_not_found_when_the_claim_adds_because():
    result = verify(
        [_event("e4", "Owner: Rick.")],
        _answer(_claim("It failed because the note said so.", ("e4", "no such sentence here"))),
    )
    assert result["struck"][0]["reasons"] == ["quote not found in e4"]


def test_address_swap_why_strikes_the_unconnected_because():
    """The checked-in explanation matches a fresh run of the checker. The model text is untouched."""
    root = Path(__file__).resolve().parents[1] / "examples" / "spark-address_swap"
    recorded = json.loads((root / "why.json").read_text(encoding="utf-8"))
    ledger = [
        json.loads(line)
        for line in (root / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    fresh = verify(ledger, json.loads(recorded["raw"]))
    assert fresh["total"] == 4
    assert fresh["ok"] == 3
    assert [claim["text"] for claim in fresh["supported"]] == [
        "The agent emailed Dana Ortiz at dana.ortiz@lab-mail.example.",
        "That updated address came from the Week 42 notes.",
        "Team.md gives a different address: dana@lab.example.",
    ]
    assert len(fresh["struck"]) == 1
    assert "because" in fresh["struck"][0]["claim"]["text"]
    assert fresh["struck"][0]["reasons"] == ["motive not in quotes"]
    assert recorded["answer"] == fresh["answer"]
    assert recorded["supported"] == fresh["supported"]
    assert recorded["struck"] == fresh["struck"]
    assert recorded["ok"] == fresh["ok"]
    assert recorded["total"] == fresh["total"]
    assert "because the Week 42 notes" in recorded["raw"]


def test_malformed_answer_with_no_claims_reports_one_struck_entry():
    result = verify(_ledger(), None)
    assert result == {
        "answer": None,
        "supported": [],
        "struck": [{"claim": None, "reasons": ["malformed answer"]}],
        "ok": 0,
        "total": 1,
    }
