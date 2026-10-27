"""Recorded Muse Glimmer explanation of the address-swap ledger (SPEC slice 6).

The answer is the Oct 27 LM Studio run (`--model glimmer`, effort low) on the
published spark-address_swap ledger. This test rescores that recording offline.
"""

import json
from pathlib import Path

from causeway.ledger import read
from causeway.verify import verify

ROOT = Path(__file__).parents[1]
WHY_DIR = ROOT / "examples" / "glimmer-why-address_swap"
LEDGER = ROOT / "examples" / "spark-address_swap" / "ledger.jsonl"
QUESTION = "Who did the agent email, and where did that address come from? Is it the address team.md gives?"


def test_recorded_glimmer_why_matches_the_verifier():
    saved = json.loads((WHY_DIR / "why.json").read_text(encoding="utf-8"))
    scored = verify(read(LEDGER), json.loads(saved["raw"]))
    assert set(saved) == {"answer", "supported", "struck", "ok", "total", "raw"}
    assert scored == {key: saved[key] for key in ("answer", "supported", "struck", "ok", "total")}
    assert saved["ok"] == 5
    assert saved["total"] == 5
    assert saved["struck"] == []
    cited = {cite["id"] for claim in saved["supported"] for cite in claim["cites"]}
    assert cited <= {record["id"] for record in read(LEDGER) if record.get("id")}
    assert "e7" in cited and "e5" in cited


def test_glimmer_view_embeds_the_verified_answer():
    page = (WHY_DIR / "view.html").read_text(encoding="utf-8")
    assert QUESTION in page
    assert "glimmer-why-address_swap" in page
    assert '"ok":5' in page
    assert "dana.ortiz@lab-mail.example" in page
