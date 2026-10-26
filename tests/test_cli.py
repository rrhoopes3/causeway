"""Tests for causeway.cli and `python -m causeway` (SPEC §8)."""

import json
import subprocess
import sys
from pathlib import Path

from causeway import ledger
from causeway.cli import format_actions, main

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


def decision_tags(path):
    return {r["id"]: r["tag"] for r in ledger.read(path) if r["kind"] == "decision"}


def test_tag_writes_header_and_tagged_decisions(tmp_path):
    out = tmp_path / "run.jsonl"
    code = main(["tag", str(FIXTURES / "explore_basic.json"), "-o", str(out), "--run-id", "r-test", "--model", "muse-spark-1.3"])
    assert code == 0
    records = ledger.read(out)
    assert records[0] == {"kind": "header", "run_id": "r-test", "model": "muse-spark-1.3", "source": "transcript", "causeway": "0.1.0"}
    assert decision_tags(out) == {"d1": "GOAL", "d2": "IN", "d3": "CARRY·1", "d4": "CARRY·2", "d5": "FINAL"}


def test_tag_accepts_a_bare_message_list(tmp_path):
    fixture = json.loads((FIXTURES / "unsourced_path.json").read_text(encoding="utf-8"))
    source = tmp_path / "list.json"
    source.write_text(json.dumps(fixture["messages"]), encoding="utf-8")
    out = tmp_path / "run.jsonl"
    assert main(["tag", str(source), "-o", str(out)]) == 0
    assert decision_tags(out) == {"d1": "LIT", "d2": "UNSOURCED", "d3": "FINAL"}


def test_tag_twice_gives_byte_identical_files(tmp_path):
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    main(["tag", str(FIXTURES / "parallel_calls.json"), "-o", str(first)])
    main(["tag", str(FIXTURES / "parallel_calls.json"), "-o", str(second)])
    assert first.read_bytes() == second.read_bytes()


def test_tag_reports_malformed_transcript_without_writing(tmp_path, capsys):
    source = tmp_path / "bad.json"
    source.write_text(json.dumps({"notes": []}), encoding="utf-8")
    out = tmp_path / "out.jsonl"
    assert main(["tag", str(source), "-o", str(out)]) == 1
    assert "messages" in capsys.readouterr().err
    assert not out.exists()


def test_tag_reports_missing_file(tmp_path, capsys):
    assert main(["tag", str(tmp_path / "nope.json"), "-o", str(tmp_path / "out.jsonl")]) == 1
    assert "nope.json" in capsys.readouterr().err


def test_show_prints_one_row_per_decision_with_echo_badge(tmp_path, capsys):
    out = tmp_path / "echo.jsonl"
    main(["tag", str(FIXTURES / "echo_loop.json"), "-o", str(out)])
    capsys.readouterr()
    assert main(["show", str(out)]) == 0
    lines = capsys.readouterr().out.splitlines()
    rows = {line.split()[0]: line for line in lines[1:]}
    assert lines[0].split()[0] == "id"
    assert sorted(rows) == ["d1", "d2", "d3", "d4"]
    assert "ECHO←d1" in rows["d2"]
    assert "ECHO←d2" in rows["d3"]
    assert "ECHO" not in rows["d1"]


def test_show_lists_unique_sources_and_carry_tags(tmp_path, capsys):
    out = tmp_path / "run.jsonl"
    main(["tag", str(FIXTURES / "parallel_calls.json"), "-o", str(out)])
    capsys.readouterr()
    main(["show", str(out)])
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()[1:]}
    assert rows["d2"].split()[-1] == "e2"
    assert "IN" in rows["d2"]

    main(["tag", str(FIXTURES / "explore_basic.json"), "-o", str(out)])
    capsys.readouterr()
    main(["show", str(out)])
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()[1:]}
    assert "CARRY·2" in rows["d4"]
    assert rows["d4"].split()[-1] == "e3"


def test_show_on_missing_ledger_returns_1(tmp_path, capsys):
    assert main(["show", str(tmp_path / "missing.jsonl")]) == 1
    assert "missing.jsonl" in capsys.readouterr().err


def test_format_actions_renders_json_values_and_truncates():
    assert format_actions([{"name": "read_file", "args": {"path": "plan-v2.md"}}]) == 'read_file(path="plan-v2.md")'
    assert format_actions([]) == "(final)"
    long = format_actions([{"name": "write_file", "args": {"path": "x" * 100}}])
    assert len(long) == 60
    assert long.endswith("...")


def test_main_without_a_command_prints_help_and_returns_2(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().out


def test_python_dash_m_round_trip_writes_utf8_to_a_pipe(tmp_path):
    out = tmp_path / "run.jsonl"
    subprocess.run(
        [sys.executable, "-m", "causeway", "tag", str(FIXTURES / "echo_loop.json"), "-o", str(out)],
        cwd=ROOT, check=True, capture_output=True,
    )
    shown = subprocess.run(
        [sys.executable, "-m", "causeway", "show", str(out)],
        cwd=ROOT, check=True, capture_output=True,
    )
    assert "ECHO←d1" in shown.stdout.decode("utf-8")
