"""One action an outside agent is about to take, tagged against what it says it read (SPEC §12.1).

The agent reports the user's request, the raw text of each source it read, and the call it wants to make. That is
turned into a transcript (the request, a listing of the sources, one `read` call per source, the action) and tagged by the same rules as any
other run, with the action's tool watched. A tainted action opens a gate in the companion and waits for the user.
Pure logic, no HTTP.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

from causeway.companion.session import Emit, Session

READ = "read"
SOURCES = "sources"
MUSE = "muse"
MAX_VALUE = 80
_NUMBERS = itertools.count(1)
STANDBY_TASK = "Waiting for Muse to check an action. Give Muse this server's MCP URL and key."

MESSAGES = {
    "clear": "Every value in this action comes from the user's request. Go ahead.",
    "approve": "The user approved this action in Causeway after seeing where its values came from. Go ahead.",
    "deny": "The user denied this action in Causeway. Do not take it. Tell the user what you were about to do and where its values came from, using the findings.",
    "waiting": "The user has not decided yet. Call checkpoint_result with this gate_id to keep waiting, or show the user the findings and ask them directly. Do not take the action until it is approved.",
}


def transcript_of(request: str, action: str, arguments: dict[str, Any], sources: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Chat messages for a checkpoint: the request as the user's message; a `sources()` call whose result lists the
    labels, then a `read` call and result per source, as an agent lists a folder and then reads its files; then the
    action as the last assistant message. Listing first means each read traces to the list instead of to nowhere."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": request}]
    if sources:
        messages.append({"role": "assistant", "content": None, "tool_calls": [_call("sources", SOURCES, {})]})
        messages.append({"role": "tool", "tool_call_id": "sources", "content": "\n".join(s["label"] for s in sources)})
        calls = [_call(f"src_{i}", READ, {"source": source["label"]}) for i, source in enumerate(sources, 1)]
        messages.append({"role": "assistant", "content": None, "tool_calls": calls})
        for call, source in zip(calls, sources):
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": source["text"]})
    messages.append({"role": "assistant", "content": None, "tool_calls": [_call("action", action, arguments)]})
    return messages


def finding(action: str, taint: dict[str, Any]) -> str:
    """One gate-panel line: `send_email.to = dana@x.example ← read(Week 42 notes)  [IN]`."""
    value = taint.get("value")
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) > MAX_VALUE:
        text = text[: MAX_VALUE - 1] + "…"
    return f"{action}.{taint['path']} = {text} ← {taint['label']}  [{taint['tag']}]"


class Checkpoint(Session):
    """A session whose run is one reported action instead of an agent loop. start() tags it and, when it is
    tainted, opens a gate. outcome() says where it stands. Gate ids (c7-g1) are unique across checkpoints, so a late
    checkpoint_result never reads a newer check's decision."""

    def __init__(
        self,
        request: str,
        action: str,
        arguments: dict[str, Any],
        sources: list[dict[str, str]],
        emit: Emit | None = None,
        why_client: Any = None,
        model: str = MUSE,
    ) -> None:
        self.action = action
        self.arguments = arguments
        self.sources = sources
        self._taint_list: list[dict[str, Any]] = []
        self._cleared = False
        super().__init__(
            request, Path("."), None, [action], emit=emit, model=model, why_client=why_client,
            gate_prefix=f"c{next(_NUMBERS)}-g",
        )

    def _loop(self) -> None:
        self._messages = transcript_of(self.task, self.action, self.arguments, self.sources)
        decision = self._publish()
        taints = self._taints(decision, 0)
        with self._lock:
            self._taint_list = taints
        if not taints:
            with self._lock:
                self._cleared = True
            return
        self._gate(decision, 0, taints, ([], {}, []))

    def outcome(self) -> dict[str, Any]:
        """{verdict, gate_id, findings, message}. verdict is clear, approve, deny, waiting (a gate is open), or
        pending (not tagged yet)."""
        with self._lock:
            gate = next(iter(self._gates.values()), None)
            findings = [finding(self.action, taint) for taint in self._taint_list]
            if self._cleared:
                verdict = "clear"
            elif gate is None:
                verdict = "pending"
            else:
                verdict = gate.verdict or "waiting"
        return {
            "verdict": verdict,
            "gate_id": gate.gate_id if gate else None,
            "findings": findings,
            "message": MESSAGES.get(verdict, "Causeway has not finished tagging this action. Call checkpoint_result."),
        }


class Standby(Session):
    """The companion's session before any checkpoint arrives: it says what to do and has nothing to run."""

    def __init__(self, emit: Emit | None = None, task: str | None = None) -> None:
        super().__init__(task or STANDBY_TASK, Path("."), None, [], emit=emit, model=MUSE)

    def _loop(self) -> None:
        return


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
