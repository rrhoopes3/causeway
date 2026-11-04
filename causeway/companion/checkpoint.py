"""One action an outside agent is about to take, tagged against what it says it read (SPEC §12.1).

The agent reports the user's request, the raw text of each source it read, and the call it wants to make. That is
turned into a transcript (the request, a listing of the sources, one `read` call per source, the action) and tagged by the same rules as any
other run, with the action's tool watched. A tainted action, or one with no value to trace, opens a gate in the
companion and waits for the user. Pure logic, no HTTP.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from causeway import tagger
from causeway.companion.session import Emit, Session

READ = "read"
SOURCES = "sources"
MUSE = "muse"
KEY_ATOMS = "(in argument names)"
MAX_VALUE = 80
STANDBY_TASK = "Waiting for Muse to check an action. Give Muse this server's MCP URL and key."

SUPERSEDED = "superseded"
# The one finding of an action with nothing traced: empty arguments, all null, or only literals. Drawn as a taint.
NO_VALUES = {"action": 0, "path": "action", "value": "no traceable values reported", "source": None, "label": "none"}

MESSAGES = {
    "clear": "No traced value came from anywhere but the user's request. This checks where values came from, not whether they are right. Go ahead.",
    "approve": "The user approved this action in Causeway after seeing where its values came from. Go ahead.",
    "deny": "The user denied this action in Causeway. Do not take it. Tell the user what you were about to do and where its values came from, using the findings.",
    "waiting": "The user has not decided yet. Show the user these findings now and tell them to decide on their Causeway page, then call checkpoint_result with this gate_id until there is a verdict. If the user tells you in the conversation to go ahead or stop, do that and say Causeway did not record it. Otherwise do not take the action until it is approved.",
    SUPERSEDED: "This check was replaced by a newer checkpoint or cleared. The user did not deny it. Do not take the action. If it is still wanted, call checkpoint again.",
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
    tainted or has no value to trace, opens a gate. outcome() says where it stands. Gate ids are unique across
    sessions, so a late checkpoint_result never reads a newer check's decision."""

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
        )

    def _loop(self) -> None:
        self._messages = transcript_of(self.task, self.action, _with_key_atoms(self.arguments), self.sources)
        decision = self._publish()
        if "watch" not in decision:  # a tagger that stopped watching must end in an error, never in clear
            raise RuntimeError(f"the tagger did not watch {self.action!r}")
        taints = self._taints(decision, 0)
        if not decision["causes"]:  # nothing was traced, so nothing here says the values are the user's
            taints = [{**NO_VALUES, "tag": decision["tag"]}]
        with self._lock:
            self._taint_list = taints
        if not taints:
            with self._lock:
                self._cleared = True
            return
        self._gate(decision, 0, taints, ([], {}, []))

    def stop(self) -> None:
        """Replaced or cleared, not decided: an open gate ends as superseded, so the agent is never told the user
        denied it. No `resolved` event goes out, because the next session's hello resets the page."""
        self._stop.set()
        with self._lock:
            gate = self._open
            if gate is None or gate.verdict is not None:
                return
            gate.verdict = SUPERSEDED
        gate.resolved.set()

    def outcome(self) -> dict[str, Any]:
        """{verdict, gate_id, findings, message}. verdict is clear, approve, deny, waiting (a gate is open),
        superseded (replaced or cleared before the user decided), or pending (not tagged yet)."""
        with self._lock:
            gate = next(iter(self._gates.values()), None)
            findings = [finding(self.action, taint) for taint in self._taint_list]
            if self._cleared:
                verdict = "clear"
            elif gate is not None:
                verdict = gate.verdict or "waiting"
            else:
                verdict = SUPERSEDED if self._stop.is_set() else "pending"
        return {
            "verdict": verdict,
            "gate_id": gate.gate_id if gate else None,
            "findings": findings,
            "message": MESSAGES.get(verdict, "Causeway has not finished tagging this action. Do not take it. Wait a few seconds, then call checkpoint again."),
        }


class Standby(Session):
    """The companion's session before any checkpoint arrives: it says what to do and has nothing to run. Its scenario,
    "standby", is what tells the page not to offer Start."""

    def __init__(self, emit: Emit | None = None, task: str | None = None) -> None:
        super().__init__(task or STANDBY_TASK, Path("."), None, [], emit=emit, model=MUSE, scenario="standby")

    def _loop(self) -> None:
        return


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


def _with_key_atoms(arguments: dict[str, Any]) -> dict[str, Any]:
    """The arguments to tag: as reported, plus any address, URL or path found in a dict key, listed as values. Only
    values are traced, so a tool taking a map keyed by recipient would otherwise hide a recipient from the check. A
    number or a long plain name in a key, such as custom_field_10023, is the tool's own vocabulary and stays out."""
    atoms = [atom for key in _keys(arguments) for atom, _ in tagger.extract_atoms(key) if set(atom) & set("@/\\")]
    return {**arguments, KEY_ATOMS: atoms} if atoms else arguments


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
