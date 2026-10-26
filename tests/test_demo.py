import json
import socket
from pathlib import Path

import pytest

from causeway import sam
from causeway.client import FakeClient
from causeway.demo import tools
from causeway.demo.__main__ import SCENARIOS, main
from causeway.demo.agent import run
from causeway.demo.tools import ALL, SandboxError, execute, resolve_in_sandbox

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def world(tmp_path):
    """A sandbox with one file and a secret file just outside it."""
    sandbox = tmp_path / "sandbox"
    (sandbox / "sub").mkdir(parents=True)
    (sandbox / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    return sandbox


def call(sandbox, name, **args):
    return execute(sandbox, name, json.dumps(args))


def _msg(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


def _call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


# sandbox confinement --------------------------------------------------------

ESCAPES = [
    "../secret.txt",
    "sub/../../secret.txt",
    "/etc/passwd",
    "\\etc\\passwd",
    "C:\\Windows\\win.ini",
    "C:win.ini",
    "\\\\server\\share\\x.txt",
    "",
    "a\x00b.txt",
]


@pytest.mark.parametrize("rel", ESCAPES)
def test_read_file_rejects_escape(world, rel):
    result = call(world, "read_file", path=rel)
    assert result.startswith("error")
    assert "TOPSECRET" not in result


@pytest.mark.parametrize("rel", ["../secret.txt", "/abs/x.txt", "D:\\x.txt"])
def test_write_file_rejects_escape_and_writes_nothing(world, tmp_path, rel):
    result = call(world, "write_file", path=rel, content="pwned")
    assert result.startswith("error")
    assert (tmp_path / "secret.txt").read_text(encoding="utf-8") == "TOPSECRET"
    assert not (tmp_path / "x.txt").exists()


def test_list_dir_rejects_parent(world):
    assert call(world, "list_dir", path="..").startswith("error")


def test_resolve_raises_sandbox_error_for_escape(world):
    with pytest.raises(SandboxError):
        resolve_in_sandbox(world, "../secret.txt")


def test_dotdot_that_stays_inside_is_allowed(world):
    assert call(world, "read_file", path="sub/../a.txt") == "hello"


def test_symlink_out_of_sandbox_is_rejected(world, tmp_path):
    link = world / "link.txt"
    try:
        link.symlink_to(tmp_path / "secret.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")
    assert call(world, "read_file", path="link.txt").startswith("error")


def test_list_and_read_inside_sandbox(world):
    assert call(world, "list_dir", path=".") == "a.txt\nsub/"
    assert call(world, "read_file", path="a.txt") == "hello"
    assert "wrote" in call(world, "write_file", path="new/deep.md", content="x")
    assert (world / "new" / "deep.md").read_text(encoding="utf-8") == "x"


def test_unknown_tool_and_bad_arguments_return_errors(world):
    assert execute(world, "no_such_tool", "{}").startswith("error")
    assert execute(world, "read_file", "not json").startswith("error")
    assert execute(world, "read_file", json.dumps({"path": "a.txt", "extra": 1})).startswith("error")


# send_email -----------------------------------------------------------------


def test_send_email_only_queues_to_outbox(world, monkeypatch):
    def no_network(self, *args, **kwargs):
        raise AssertionError("send_email must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    assert call(world, "send_email", to="dana@lab.example", subject="Summary", body="Hi\nthere") == "queued"
    assert call(world, "send_email", to="b@x.example", subject="S2", body="B2") == "queued"
    lines = (world / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"to": "dana@lab.example", "subject": "Summary", "body": "Hi\nthere"},
        {"to": "b@x.example", "subject": "S2", "body": "B2"},
    ]


# segment_image --------------------------------------------------------------


def test_segment_image_without_key_returns_error(world, monkeypatch):
    (world / "desk.jpg").write_bytes(b"\xff\xd8")
    monkeypatch.setattr(tools, "load_api_key", lambda *a, **k: None)
    result = call(world, "segment_image", path="desk.jpg", prompt="mug")
    assert result.startswith("error")
    assert "MODEL_API_KEY" in result


def test_segment_image_summarises_records(world, monkeypatch):
    (world / "desk.jpg").write_bytes(b"\xff\xd8")
    monkeypatch.setattr(tools, "load_api_key", lambda *a, **k: "unit-key")
    records = [
        {"frame": 0, "id": 0, "box": [1, 2, 3, 4], "size": [10, 10], "confidence": 0.93},
        {"frame": 0, "id": 1, "box": [5, 6, 7, 8], "size": [10, 10], "confidence": None},
    ]
    seen = {}

    def fake_segment(path, prompt, api_key=None, **kwargs):
        seen.update(path=path, prompt=prompt, api_key=api_key)
        return records

    monkeypatch.setattr(sam, "segment", fake_segment)
    result = call(world, "segment_image", path="desk.jpg", prompt="mug")
    assert result == '2 x "mug": boxes [[1, 2, 3, 4], [5, 6, 7, 8]]; confidence [0.93]'
    assert seen["prompt"] == "mug"
    assert seen["api_key"] == "unit-key"
    assert seen["path"] == str(world / "desk.jpg")


def test_segment_image_no_matches(world, monkeypatch):
    (world / "desk.jpg").write_bytes(b"\xff\xd8")
    monkeypatch.setattr(tools, "load_api_key", lambda *a, **k: "unit-key")
    monkeypatch.setattr(sam, "segment", lambda *a, **k: [])
    assert call(world, "segment_image", path="desk.jpg", prompt="mug") == '0 x "mug": no matches'


# agent loop -----------------------------------------------------------------


def test_run_two_tool_calls_then_finish(world):
    client = FakeClient(
        [
            _msg(calls=[_call("call_1", "list_dir", {"path": "."})]),
            _msg(calls=[_call("call_2", "read_file", {"path": "a.txt"})]),
            _msg(content="The file says hello."),
        ]
    )
    result = run("Read a.txt and report it.", world, client)

    roles = [m["role"] for m in result["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert result["messages"][1]["content"] == "Read a.txt and report it."
    assert result["messages"][3]["tool_call_id"] == "call_1"
    assert "a.txt" in result["messages"][3]["content"]
    assert result["messages"][5]["tool_call_id"] == "call_2"
    assert result["messages"][5]["content"] == "hello"
    assert result["messages"][-1]["content"] == "The file says hello."
    assert result["task"] == "Read a.txt and report it."
    assert result["model"] == "fake"
    assert [t["function"]["name"] for t in result["tools"]] == [t.name for t in ALL]

    assert len(client.calls) == 3
    assert client.calls[0]["reasoning_effort"] == "low"
    assert client.calls[0]["tools"] == [t.schema for t in ALL]
    assert len(client.calls[0]["messages"]) == 2
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result


def test_run_stops_at_max_steps(world):
    client = FakeClient([_msg(calls=[_call(f"c{i}", "list_dir", {"path": "."})]) for i in range(5)])
    result = run("loop forever", world, client, max_steps=2)
    assert len(client.calls) == 2
    assert len(result["messages"]) == 2 + 2 * 2
    assert result["messages"][-1]["role"] == "tool"


def test_run_stops_on_plain_answer_without_tools(world):
    client = FakeClient([_msg(content="nothing to do")])
    result = run("hi", world, client, max_steps=5)
    assert len(client.calls) == 1
    assert result["messages"][-1] == {"role": "assistant", "content": "nothing to do"}


# scenarios and CLI ----------------------------------------------------------


def test_every_scenario_has_task_sandbox_and_readme():
    folders = [p for p in sorted(SCENARIOS.iterdir()) if p.is_dir()]
    assert {p.name for p in folders} >= {"injection", "missing_file", "count_mugs"}
    for folder in folders:
        assert (folder / "task.txt").is_file(), folder
        assert (folder / "sandbox").is_dir(), folder
        assert (folder / "README.md").is_file(), folder


def test_scenarios_live_at_repo_root():
    assert SCENARIOS == REPO / "demo" / "scenarios"


def test_cli_list_prints_scenarios(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "injection:" in out
    assert "missing_file:" in out
    assert "count_mugs:" in out


def test_cli_run_without_key_fails_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    out_file = tmp_path / "t.json"
    assert main(["run", "injection", "-o", str(out_file)]) == 1
    assert not out_file.exists()
    assert "MODEL_API_KEY" in capsys.readouterr().err
