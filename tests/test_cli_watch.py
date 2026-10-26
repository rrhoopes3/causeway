from pathlib import Path

from causeway import ledger
from causeway.cli import main, origins

FIXTURE = Path(__file__).parent / "fixtures" / "injection.json"


def _tag(tmp_path, *extra):
    out = tmp_path / "run.jsonl"
    assert main(["tag", str(FIXTURE), "-o", str(out), *extra]) == 0
    return out


def test_tag_without_watch_adds_no_watch_keys(tmp_path):
    records = ledger.read(_tag(tmp_path))
    assert not any("watch" in record for record in records)


def test_show_names_the_file_each_taint_came_from(tmp_path, capsys):
    out = _tag(tmp_path, "--watch", "send_email")
    capsys.readouterr()
    assert main(["show", str(out)]) == 0
    text = capsys.readouterr().out
    assert "⚠ d5 send_email.to ← e5 read_file(notes/b.md)  [CARRY·1]" in text
    assert "⚠ d4 send_email.to ← e4 read_file(team.md)  [CARRY·1]" in text
    assert "⚠ d5 send_email.body ← nowhere in the run  [UNSOURCED]" in text


def test_watch_accepts_a_comma_list_with_spaces(tmp_path):
    records = ledger.read(_tag(tmp_path, "--watch", " write_file , send_email "))
    assert [r["id"] for r in records if "watch" in r] == ["d4", "d5"]


def test_origins_label_tool_results_and_the_task(tmp_path):
    records = ledger.read(_tag(tmp_path))
    labels = origins(records)
    assert labels["e5"] == "read_file(notes/b.md)"
    goal_ids = [r["id"] for r in records if r["kind"] == "event" and r["step"] == 1 and r["role"] != "tool"]
    assert goal_ids and all(labels[i] == "the task" for i in goal_ids)


def test_atoms_from_one_source_collapse_to_one_line(tmp_path, capsys):
    out = tmp_path / "run.jsonl"
    fixture = Path(__file__).parent / "fixtures" / "sam_boxes.json"
    assert main(["tag", str(fixture), "-o", str(out), "--watch", "write_file"]) == 0
    capsys.readouterr()
    assert main(["show", str(out)]) == 0
    taints = [line for line in capsys.readouterr().out.splitlines() if line.startswith("⚠")]
    assert len(taints) == 1
    assert "write_file.content~0..15 (16 values) ← e" in taints[0]
    assert taints[0].endswith("[IN]")
