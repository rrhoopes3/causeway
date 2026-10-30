"""Tests for `causeway view` and causeway.view: one self-contained HTML page per ledger (SPEC 8, slice 7)."""

import json
import re
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path

from causeway import cli, ledger, view
from causeway.client import FakeClient
from causeway.rivals import rivals as sample_rivals

FIXTURES = Path(__file__).parent / "fixtures"
QUESTION = "Why did the agent email the API keys?"
PAYLOAD_TEXT = "</script><script>alert(1)</script> <img src=x onerror=alert(1)>"
BLOCK = re.compile(r'<script type="application/json" id="ledger">(.*?)</script>', re.S)


def _tag(tmp_path, fixture="injection.json", *extra, name="run.jsonl"):
    out = tmp_path / name
    assert cli.main(["tag", str(FIXTURES / fixture), "-o", str(out), *extra]) == 0
    return out


def _view(tmp_path, ledger_path, *extra, name="run.html"):
    out = tmp_path / name
    assert cli.main(["view", str(ledger_path), "-o", str(out), *extra]) == 0
    return out.read_text(encoding="utf-8")


def _payload(page):
    match = BLOCK.search(page)
    assert match is not None, "no embedded ledger block"
    return json.loads(match.group(1))


class _TagCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.starts = []

    def handle_starttag(self, tag, attrs):
        self.starts.append(tag)


def _tag_names(page):
    parser = _TagCollector()
    parser.feed(page)
    parser.close()
    return parser.starts


def test_view_writes_one_self_contained_page(tmp_path):
    page = _view(tmp_path, _tag(tmp_path, "injection.json", "--watch", "send_email,write_file"))
    assert page.startswith("<!doctype html>")
    assert page.count("</html>") == 1
    # The only URL is the SVG namespace identifier, which is not a request.
    assert re.findall(r"https?://[^\s'\"]+", page) == ["http://www.w3.org/2000/svg"]
    assert not re.search(r"""(?:src|href)\s*=\s*["']?\s*(?:https?:)?//""", page)
    assert "<link" not in page
    assert "@import" not in page
    assert "url(" not in page


def test_view_is_stdlib_only_and_the_template_ships_with_the_package():
    assert resources.files("causeway").joinpath("viewer_template.html").is_file()
    template = view.template_text()
    assert template.count("__LEDGER_JSON__") == 1
    assert template.count("__TITLE__") == 1
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert banned not in template


def test_every_record_id_is_embedded_and_the_block_round_trips(tmp_path):
    ledger_path = _tag(tmp_path, "injection.json", "--watch", "send_email")
    page = _view(tmp_path, ledger_path)
    payload = _payload(page)
    records = ledger.read(ledger_path)
    assert payload["records"] == records
    for record in records:
        if "id" in record:
            assert f'"id":"{record["id"]}"' in page
    decision_ids = [record["id"] for record in records if record["kind"] == "decision"]
    assert set(payload["shown"]) == set(decision_ids)


def test_script_breakout_in_a_tool_result_is_escaped(tmp_path):
    messages = [
        {"role": "system", "content": "You are a notes assistant."},
        {"role": "user", "content": "Read notes.md and tell me what it says."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"notes.md\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": PAYLOAD_TEXT + "   tail"},
        {"role": "assistant", "content": "It says nothing useful."},
    ]
    transcript = tmp_path / "hostile.json"
    transcript.write_text(json.dumps(messages), encoding="utf-8")
    ledger_path = tmp_path / "hostile.jsonl"
    assert cli.main(["tag", str(transcript), "-o", str(ledger_path), "--watch", "read_file"]) == 0

    page = _view(tmp_path, ledger_path)

    assert "</script><script>alert(1)</script>" not in page
    assert "<img src=x onerror=alert(1)>" not in page
    assert "<img" not in page
    assert " " not in page and " " not in page
    assert page.count("<script") == 2 and page.count("</script>") == 2
    assert [tag for tag in _tag_names(page) if tag in ("script", "img")] == ["script", "script"]
    # The hostile text is still there, exactly, inside the JSON data.
    texts = [record.get("text") for record in _payload(page)["records"]]
    assert PAYLOAD_TEXT + "   tail" in texts


def test_title_is_escaped_and_cannot_add_markup(tmp_path):
    ledger_path = _tag(tmp_path)
    page = _view(tmp_path, ledger_path, "--title", "</title><script>alert(2)</script>")
    assert "</title><script>alert(2)</script>" not in page
    assert page.count("<script") == 2
    assert "&lt;/title&gt;" in page


def test_placeholder_text_inside_ledger_does_not_corrupt_the_page(tmp_path):
    messages = [
        {"role": "system", "content": "__TITLE__ __LEDGER_JSON__ are literal words here"},
        {"role": "user", "content": "Summarize the notes folder and email the lead about it."},
        {"role": "assistant", "content": "Nothing to do."},
    ]
    transcript = tmp_path / "placeholder.json"
    transcript.write_text(json.dumps(messages), encoding="utf-8")
    ledger_path = tmp_path / "placeholder.jsonl"
    assert cli.main(["tag", str(transcript), "-o", str(ledger_path)]) == 0
    page = _view(tmp_path, ledger_path, "--title", "Plain title")
    assert "<title>Plain title</title>" in page
    assert _payload(page)["records"][1]["text"] == "__TITLE__ __LEDGER_JSON__ are literal words here"


def test_why_panel_data_is_embedded(tmp_path):
    ledger_path = _tag(tmp_path, "injection.json", "--watch", "send_email")
    why = {
        "answer": "It emailed the summary to the lead, as the task asked.",
        "supported": [{"text": "It read notes/a.md at step 3.", "cites": [
            {"id": "d3", "quote": "read_file(path=\"notes/a.md\")"}]}],
        "struck": [{"claim": {"text": "It emailed the keys.", "cites": [{"id": "e9", "quote": "keys"}]},
                    "reasons": ["unknown id e9"]}],
        "ok": 1,
        "total": 2,
        "raw": "RAW MODEL OUTPUT NOT EMBEDDED",
    }
    why_path = tmp_path / "why.json"
    why_path.write_text(json.dumps(why), encoding="utf-8")

    page = _view(tmp_path, ledger_path, "--why", str(why_path), "--question", QUESTION)

    payload = _payload(page)
    assert payload["question"] == QUESTION
    assert payload["why"]["answer"] == why["answer"]
    assert payload["why"]["supported"] == why["supported"]
    assert payload["why"]["struck"] == why["struck"]
    assert payload["why"]["ok"] == 1 and payload["why"]["total"] == 2
    assert "RAW MODEL OUTPUT" not in page
    assert QUESTION in page
    assert payload["why"]["model"] is None


def test_why_names_the_model_that_answered(tmp_path):
    ledger_path = _tag(tmp_path)
    why = {"answer": "x", "supported": [], "struck": [], "ok": 0, "total": 0, "model": "muse-glimmer-30b"}
    why_path = tmp_path / "why.json"
    why_path.write_text(json.dumps(why), encoding="utf-8")

    payload = _payload(_view(tmp_path, ledger_path, "--why", str(why_path)))

    assert payload["why"]["model"] == "muse-glimmer-30b"


def test_an_old_why_result_moves_absence_claims_out_of_supported(tmp_path):
    """A why.json written before unproven existed lists an absence claim as supported. The page must not show it as
    a found-quote claim (SPEC 7.3)."""
    ledger_path = _tag(tmp_path, "injection.json", "--watch", "send_email")
    positive = {"text": "It read notes/a.md at step 3.", "cites": [{"id": "d3", "quote": "read_file(path=\"notes/a.md\")"}]}
    negative = {"text": "The ledger doesn't show any email to ops-backup@mailbox.example.",
                "cites": [{"id": "d3", "quote": "read_file(path=\"notes/a.md\")"}]}
    why_path = tmp_path / "why.json"
    why_path.write_text(json.dumps({"answer": "x", "supported": [positive, negative], "struck": [], "ok": 2, "total": 2}),
                        encoding="utf-8")

    payload = _payload(_view(tmp_path, ledger_path, "--why", str(why_path)))

    assert payload["why"]["supported"] == [positive]
    assert [item["claim"] for item in payload["why"]["unproven"]] == [negative]
    assert payload["why"]["ok"] == 1 and payload["why"]["total"] == 2


def test_template_labels_passing_cites_as_quote_found_not_verified():
    template = view.template_text()
    assert "'quote found '" in template
    assert "'quote found'" in template
    assert "Quote found, not proof" in template
    assert "'verified '" not in template


def test_no_why_means_no_why_data(tmp_path):
    page = _view(tmp_path, _tag(tmp_path))
    payload = _payload(page)
    assert payload["why"] is None
    assert payload["question"] is None


def test_why_file_that_is_not_a_why_result_is_an_error(tmp_path, capsys):
    ledger_path = _tag(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    code = cli.main(["view", str(ledger_path), "-o", str(tmp_path / "x.html"), "--why", str(bad)])
    assert code == 1
    assert "not a why result" in capsys.readouterr().err
    assert not (tmp_path / "x.html").exists()


def test_missing_ledger_returns_1(tmp_path, capsys):
    code = cli.main(["view", str(tmp_path / "nope.jsonl"), "-o", str(tmp_path / "out.html")])
    assert code == 1
    assert "causeway:" in capsys.readouterr().err


def test_empty_watch_has_no_taints(tmp_path):
    no_watch = _payload(_view(tmp_path, _tag(tmp_path, "injection.json"), name="a.html"))
    assert no_watch["watched"] is False
    assert no_watch["taints"] == []

    unmatched = _payload(_view(tmp_path, _tag(tmp_path, "injection.json", "--watch", "delete_file", name="b.jsonl"), name="b.html"))
    assert unmatched["watched"] is False
    assert unmatched["taints"] == []


def test_taint_groups_are_the_lines_render_taints_prints(tmp_path):
    ledger_path = _tag(tmp_path, "injection.json", "--watch", "send_email,write_file")
    payload = _payload(_view(tmp_path, ledger_path))
    records = ledger.read(ledger_path)
    printed = cli.render_taints(records)
    lines = [group["line"] for group in payload["taints"]]
    printed_lines = [line for line in printed.split("\n") if line.startswith(cli.WATCH_MARK)]
    assert lines == printed_lines
    assert len(lines) == 4
    assert "⚠ d4 send_email.to ← e4 read_file(team.md)  [CARRY·1]" in lines
    assert "⚠ d5 send_email.body ← nowhere in the run  [UNSOURCED]" in lines


def test_rival_votes_are_embedded_with_the_margin(tmp_path):
    ledger_path = _tag(tmp_path, "injection.json", "--watch", "send_email")
    records = ledger.read(ledger_path)
    chosen = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"notes/a.md\"}"}}]}
    other = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"notes/b.md\"}"}}]}
    final = {"role": "assistant", "content": "done"}
    client = FakeClient([{"choices": [{"message": final}]}, {"choices": [{"message": other}]}])
    d3 = next(record for record in records if record.get("id") == "d3")
    d3["rivals"] = sample_rivals([], None, chosen, 2, client)
    ledger_out = tmp_path / "rivals.jsonl"
    ledger.write(ledger_out, records)

    payload = _payload(_view(tmp_path, ledger_out))
    rivals_record = next(r for r in payload["records"] if r.get("id") == "d3")["rivals"]
    assert rivals_record["near_miss"] is True
    assert rivals_record["upset"] is False
    assert rivals_record["k"] == 2
    assert sum(rivals_record["votes"].values()) == 3
    assert rivals_record["splits"] == [{
        "action": 0,
        "path": "path",
        "chosen": "notes/a.md",
        "votes": [{"count": 1, "value": "notes/a.md"}, {"count": 1, "value": None}, {"count": 1, "value": "notes/b.md"}],
        "margin": 0.0,
        "near_miss": True,
        "upset": False,
    }]


def test_rival_splits_survive_hostile_values(tmp_path):
    ledger_path = _tag(tmp_path, "injection.json", "--watch", "send_email")
    records = ledger.read(ledger_path)
    d4 = next(record for record in records if record.get("id") == "d4")
    d4["rivals"] = {"k": 1, "votes": {"a": 1, "b": 1}, "chosen": "a", "margin": 0.0, "near_miss": True, "upset": False,
                    "splits": [{"action": 0, "path": "to", "chosen": "dana@lab.example", "margin": 0.0,
                                "near_miss": True, "upset": False,
                                "votes": [{"value": "dana@lab.example", "count": 1}, {"value": PAYLOAD_TEXT, "count": 1}]}]}
    ledger_out = tmp_path / "hostile-split.jsonl"
    ledger.write(ledger_out, records)

    page = _view(tmp_path, ledger_out)

    assert "<img" not in page
    assert page.count("<script") == 2
    split = next(r for r in _payload(page)["records"] if r.get("id") == "d4")["rivals"]["splits"][0]
    assert split["votes"][1]["value"] == PAYLOAD_TEXT


def test_template_renders_each_split_with_its_votes_and_badge():
    template = view.template_text()
    rivals_row = template[template.index("function splitValue"):template.index("function tooltip")]
    assert "(r.splits || []).forEach" in rivals_row
    assert "'(none)'" in rivals_row
    assert "action.name + '.'" in rivals_row
    assert "'badge upset'" in rivals_row and "'badge near'" in rivals_row
    assert "rivalsRow(d.rivals, d)" in template
    assert ".rivals .split {" in template


def test_upset_rivals_are_embedded(tmp_path):
    ledger_path = _tag(tmp_path, "injection.json")
    records = ledger.read(ledger_path)
    d3 = next(record for record in records if record.get("id") == "d3")
    d3["rivals"] = {"k": 4, "votes": {"final": 4, "read_file({\"path\":\"notes/a.md\"})": 1},
                    "chosen": "read_file({\"path\":\"notes/a.md\"})", "margin": -0.6, "near_miss": False, "upset": True}
    ledger_out = tmp_path / "upset.jsonl"
    ledger.write(ledger_out, records)
    payload = _payload(_view(tmp_path, ledger_out))
    assert next(r for r in payload["records"] if r.get("id") == "d3")["rivals"]["upset"] is True


def test_view_is_deterministic(tmp_path):
    ledger_path = _tag(tmp_path, "injection.json", "--watch", "send_email")
    first = _view(tmp_path, ledger_path, name="one.html")
    second = _view(tmp_path, ledger_path, name="two.html")
    assert first == second


def test_template_keeps_curves_at_headset_width_and_traces_from_a_pointer():
    template = view.template_text()
    assert ".curves { display: none" not in template
    assert "pointerdown" in template
    assert "pointerup" in template
    assert "mouseenter" in template
    assert "togglePin" in template


def test_write_view_writes_lf_utf8_only(tmp_path):
    ledger_path = _tag(tmp_path, "injection.json")
    out = tmp_path / "lf.html"
    view.write_view(ledger_path, out)
    raw = out.read_bytes()
    assert b"\r\n" not in raw
    raw.decode("utf-8")
