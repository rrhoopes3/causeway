import json

import httpx
import pytest

from causeway.client import APIError, ChatClient, FakeClient, load_api_key, message_of

KEY = "test-key-abc123"
TOOL = {
    "type": "function",
    "function": {"name": "read_file", "description": "Read a file", "parameters": {"type": "object"}},
}
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}, "strict": True}}
OK_BODY = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]}


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    return tmp_path


def _recording_transport(captured, status=200, body=None):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, json=body if body is not None else OK_BODY)

    return httpx.MockTransport(handler)


def test_load_api_key_prefers_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setenv("MODEL_API_KEY", "env-key")
    assert load_api_key(env_file) == "env-key"


def test_load_api_key_ignores_blank_environment_value(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setenv("MODEL_API_KEY", "   ")
    assert load_api_key(env_file) == "file-key"


def test_load_api_key_from_env_file_with_comments_and_quotes(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# causeway settings\n\n   \nOTHER=1\nMODEL_API_KEY=\"file-key-xyz\"\n", encoding="utf-8"
    )
    assert load_api_key(env_file) == "file-key-xyz"


def test_load_api_key_accepts_single_quotes_and_spaces(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_API_KEY = 'spaced-key'\n", encoding="utf-8")
    assert load_api_key(env_file) == "spaced-key"


def test_load_api_key_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert load_api_key(tmp_path / "missing.env") is None
    other = tmp_path / "other.env"
    other.write_text("OTHER=1\n", encoding="utf-8")
    assert load_api_key(other) is None


def test_load_api_key_default_path_under_home(isolated_home):
    target = isolated_home / ".config" / "causeway"
    target.mkdir(parents=True)
    (target / ".env").write_text("MODEL_API_KEY=home-key\n", encoding="utf-8")
    assert load_api_key() == "home-key"


def test_chat_client_posts_expected_request(isolated_home):
    captured: list[httpx.Request] = []
    client = ChatClient(api_key=KEY, transport=_recording_transport(captured))
    response = client.complete(
        [{"role": "user", "content": "hi"}],
        tools=[TOOL],
        response_format=RESPONSE_FORMAT,
        reasoning_effort="high",
        max_tokens=50,
    )
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.meta.ai/v1/chat/completions"
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(req.content)
    assert body["model"] == "muse-spark-1.3"
    assert body["tools"] == [TOOL]
    assert body["response_format"] == RESPONSE_FORMAT
    assert body["reasoning_effort"] == "high"
    assert body["max_tokens"] == 50
    for omitted in ("n", "stop", "logit_bias", "tool_choice"):
        assert omitted not in body
    assert message_of(response)["content"] == "hi"


def test_chat_client_omits_optional_fields_when_unset(isolated_home):
    captured: list[httpx.Request] = []
    client = ChatClient(api_key=KEY, transport=_recording_transport(captured))
    client.complete([{"role": "user", "content": "hi"}])
    body = json.loads(captured[0].content)
    assert set(body) == {"model", "messages"}


def test_chat_client_reads_key_from_environment(isolated_home, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    captured: list[httpx.Request] = []
    client = ChatClient(transport=_recording_transport(captured))
    client.complete([{"role": "user", "content": "hi"}])
    assert captured[0].headers["Authorization"] == f"Bearer {KEY}"


def test_chat_client_requires_key_for_meta(isolated_home):
    with pytest.raises(ValueError):
        ChatClient(transport=_recording_transport([]))


def test_chat_client_local_server_needs_no_key(isolated_home):
    captured: list[httpx.Request] = []
    client = ChatClient(base_url="http://127.0.0.1:8080/v1", transport=_recording_transport(captured))
    client.complete([{"role": "user", "content": "hi"}])
    assert str(captured[0].url) == "http://127.0.0.1:8080/v1/chat/completions"
    assert "Authorization" not in captured[0].headers


@pytest.mark.parametrize("name", ["n", "stop", "logit_bias", "tool_choice"])
def test_chat_client_rejects_forbidden_extras(isolated_home, name):
    captured: list[httpx.Request] = []
    client = ChatClient(api_key=KEY, transport=_recording_transport(captured))
    with pytest.raises(ValueError):
        client.complete([{"role": "user", "content": "hi"}], **{name: 1})
    assert captured == []


def test_chat_client_rejects_unknown_reasoning_effort(isolated_home):
    client = ChatClient(api_key=KEY, transport=_recording_transport([]))
    with pytest.raises(ValueError):
        client.complete([{"role": "user", "content": "hi"}], reasoning_effort="max")


def test_api_error_carries_status_and_excludes_key(isolated_home):
    captured: list[httpx.Request] = []
    transport = _recording_transport(captured, status=401, body={"error": "invalid api key"})
    client = ChatClient(api_key=KEY, transport=transport)
    with pytest.raises(APIError) as info:
        client.complete([{"role": "user", "content": "hi"}])
    assert info.value.status == 401
    assert "invalid api key" in info.value.body
    assert KEY not in str(info.value)


def test_api_error_redacts_key_echoed_by_server(isolated_home):
    transport = _recording_transport([], status=400, body={"error": f"bad key {KEY}"})
    client = ChatClient(api_key=KEY, transport=transport)
    with pytest.raises(APIError) as info:
        client.complete([{"role": "user", "content": "hi"}])
    assert KEY not in str(info.value)
    assert "[redacted]" in str(info.value)


def test_fake_client_returns_in_order_and_records_calls():
    first = {"choices": [{"message": {"role": "assistant", "content": "one"}}]}
    second = {"choices": [{"message": {"role": "assistant", "content": "two"}}]}
    fake = FakeClient([first, second])
    messages = [{"role": "user", "content": "q"}]
    assert fake.complete(messages, tools=[TOOL], reasoning_effort="low") is first
    assert fake.complete(messages) is second
    assert len(fake.calls) == 2
    assert fake.calls[0]["messages"] == messages
    assert fake.calls[0]["tools"] == [TOOL]
    assert fake.calls[0]["reasoning_effort"] == "low"
    assert fake.calls[1]["tools"] is None


def test_fake_client_raises_when_exhausted():
    fake = FakeClient([])
    with pytest.raises(RuntimeError):
        fake.complete([{"role": "user", "content": "q"}])


def test_message_of_returns_first_choice_message():
    message = {"role": "assistant", "content": "hello"}
    assert message_of({"choices": [{"message": message}, {"message": {"content": "other"}}]}) == message


def test_stored_key_is_never_sent_to_a_non_meta_server(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    captured = []
    client = ChatClient(base_url="http://127.0.0.1:1234/v1", model="local", transport=_recording_transport(captured))
    client.complete([{"role": "user", "content": "hi"}])
    assert "authorization" not in captured[0].headers
    assert client.api_key is None


def test_stored_key_is_loaded_for_meta(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    captured = []
    client = ChatClient(transport=_recording_transport(captured))
    client.complete([{"role": "user", "content": "hi"}])
    assert captured[0].headers["authorization"] == f"Bearer {KEY}"
