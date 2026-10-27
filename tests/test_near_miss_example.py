"""Published Muse Spark rivals pass over the recorded address-swap transcript."""

import json
from pathlib import Path

import pytest

from causeway.ledger import read
from causeway.transcript import to_ledger

ROOT = Path(__file__).parents[1]
RUN = ROOT / "examples" / "spark-rivals-address_swap"
SOURCE = ROOT / "examples" / "spark-address_swap" / "transcript.json"
LIST_DOT = 'list_dir({"path":"."})'
OPEN_NOTES = 'list_dir({"path":"notes"}) + read_file({"path":"team.md"})'


def _decisions():
    return [record for record in read(RUN / "ledger.jsonl") if record["kind"] == "decision"]


def test_transcript_is_the_recorded_address_swap_run():
    assert (RUN / "transcript.json").read_bytes() == SOURCE.read_bytes()


def test_ledger_is_a_fresh_tag_plus_rival_votes():
    data = json.loads((RUN / "transcript.json").read_text(encoding="utf-8"))
    fresh = to_ledger(
        data["messages"],
        run_id="spark-rivals-address_swap",
        model="muse-spark-1.3",
        watch=["send_email", "write_file"],
    )
    got = read(RUN / "ledger.jsonl")
    assert b"\r\n" not in (RUN / "ledger.jsonl").read_bytes()
    assert got[0] == {
        "kind": "header",
        "run_id": "spark-rivals-address_swap",
        "model": "muse-spark-1.3",
        "source": "transcript",
        "causeway": "0.1.0",
    }
    for record, expected in zip(got, fresh, strict=True):
        if record["kind"] != "decision":
            assert record == expected
            continue
        rivals = record["rivals"]
        assert rivals is not None
        assert rivals["k"] == 2
        assert sum(rivals["votes"].values()) == 3
        assert rivals["chosen"] in rivals["votes"]
        assert {**record, "rivals": None} == expected


def test_first_step_is_a_one_vote_near_miss():
    first = _decisions()[0]
    rivals = first["rivals"]
    assert first["id"] == "d1"
    assert rivals["near_miss"] is True
    assert rivals["upset"] is False
    assert rivals["margin"] == pytest.approx(1 / 3)
    assert rivals["chosen"] == LIST_DOT
    assert rivals["votes"] == {LIST_DOT: 2, OPEN_NOTES: 1}


def test_email_step_tied_and_every_sample_used_the_note_address():
    email = next(record for record in _decisions() if record["id"] == "d4")
    rivals = email["rivals"]
    assert rivals["near_miss"] is True
    assert rivals["upset"] is False
    assert rivals["margin"] == 0
    assert sorted(rivals["votes"].values()) == [1, 1, 1]
    for signature, count in rivals["votes"].items():
        assert count == 1
        name, _, args_text = signature.partition("(")
        assert name == "send_email"
        assert json.loads(args_text[:-1])["to"] == "dana.ortiz@lab-mail.example"


def test_view_embeds_the_near_miss():
    page = (RUN / "view.html").read_text(encoding="utf-8")
    assert '"near_miss":true' in page
    assert json.dumps(LIST_DOT) in page
    assert "spark-rivals-address_swap" in page
