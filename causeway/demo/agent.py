"""The demo agent loop: a file assistant that calls tools inside a sandbox until it answers (SPEC 9, slice 4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from causeway.client import message_of
from causeway.demo.tools import ALL, Tool, execute

SYSTEM = (
    "You are a file assistant working in a sandbox folder. Use the tools to look at files and do "
    "the task. When the task is finished, reply with a short final answer and no tool calls."
)
REASONING_EFFORT = "low"


def run(
    task: str,
    sandbox: str | Path,
    client: Any,
    max_steps: int = 12,
    tools: tuple[Tool, ...] = ALL,
) -> dict[str, Any]:
    """Run the agent on `task` until it answers without tool calls or `max_steps` model calls are made.

    Each model call is one step. Tool results are appended as `role: tool` messages matched by
    tool_call_id. Returns the full transcript: messages, tool schemas, task and model name.
    """
    schemas = [t.schema for t in tools]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": task},
    ]
    for _ in range(max_steps):
        response = client.complete(messages, tools=schemas, reasoning_effort=REASONING_EFFORT)
        message = message_of(response)
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            break
        for call in calls:
            function = call.get("function", {})
            result = execute(Path(sandbox), function.get("name", ""), function.get("arguments", ""), tools)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    return {
        "messages": messages,
        "tools": schemas,
        "task": task,
        "model": getattr(client, "model", "fake"),
    }


def dumps(transcript: dict[str, Any]) -> str:
    """Serialise a transcript as UTF-8 friendly JSON with indent 2."""
    return json.dumps(transcript, indent=2, ensure_ascii=False)
