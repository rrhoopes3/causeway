"""OpenAI-compatible chat client for the Meta Model API and local Glimmer servers (SPEC 3, 8)."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"
KEY_NAME = "MODEL_API_KEY"
FORBIDDEN_EXTRA = ("n", "stop", "logit_bias", "tool_choice")
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def default_env_path() -> Path:
    """Return ~/.config/causeway/.env, resolved at call time so HOME can be redirected."""
    return Path.home() / ".config" / "causeway" / ".env"


def load_api_key(env_path: str | os.PathLike[str] | None = None) -> str | None:
    """Return MODEL_API_KEY from the environment, else from the .env file, else None.

    A blank environment value counts as unset. In the .env file, blank lines and
    lines starting with # are skipped, and one pair of matching quotes is stripped
    from the value. The key is never printed or logged.
    """
    env_value = os.environ.get(KEY_NAME, "").strip()
    if env_value:
        return env_value
    path = Path(env_path) if env_path is not None else default_env_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != KEY_NAME:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None


class APIError(RuntimeError):
    """A non-2xx response from the chat endpoint. The message never contains the API key."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"chat API returned HTTP {status}: {body}")


def _is_meta(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "") == "api.meta.ai"


class ChatClient:
    """Posts to {base_url}/chat/completions and returns the parsed JSON body.

    A missing key is an error only for api.meta.ai; local servers are called without
    an Authorization header. `transport` exists so tests can pass httpx.MockTransport.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = 120,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or load_api_key()
        if self.api_key is None and _is_meta(self.base_url):
            raise ValueError("MODEL_API_KEY is not set (environment or ~/.config/causeway/.env)")
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Make one chat completion call and return the parsed response.

        `n`, `stop`, `logit_bias` and `tool_choice` are rejected rather than sent.
        `tools` is omitted when empty or None.
        """
        clash = [k for k in extra if k in FORBIDDEN_EXTRA]
        if clash:
            raise ValueError(f"not sent to the Meta API (SPEC 3): {', '.join(clash)}")
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"reasoning_effort must be one of {REASONING_EFFORTS}")
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
        if response_format is not None:
            body["response_format"] = response_format
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort
        body.update(extra)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        resp = self._http.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        if not 200 <= resp.status_code < 300:
            text = resp.text
            if self.api_key:
                text = text.replace(self.api_key, "[redacted]")
            raise APIError(resp.status_code, text)
        return resp.json()

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._http.close()


class FakeClient:
    """Scripted stand-in for ChatClient: returns queued responses in order and records each call."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._queue = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Record the call's arguments and return the next scripted response."""
        self.calls.append(
            copy.deepcopy(
                {
                    "messages": messages,
                    "tools": tools,
                    "response_format": response_format,
                    "reasoning_effort": reasoning_effort,
                    **extra,
                }
            )
        )
        if not self._queue:
            raise RuntimeError(f"FakeClient has no scripted response left for call {len(self.calls)}")
        return self._queue.pop(0)


def message_of(response: dict[str, Any]) -> dict[str, Any]:
    """Return choices[0].message of a chat completion response."""
    return response["choices"][0]["message"]
