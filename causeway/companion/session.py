"""One live run of the demo agent, gated at tainted watched calls, reported as events (SPEC §11.1, §11.2).

Pure logic, no HTTP. The loop runs in a daemon thread. Every event goes through one lock, so `seq` rises by one
per event and two threads never interleave their events.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from causeway import cli, explain, rivals, transcript
from causeway.client import message_of
from causeway.demo.agent import REASONING_EFFORT, SYSTEM
from causeway.demo.tools import ALL, Tool, execute

DENIED = "denied: the user declined this action"
VERDICTS = ("approve", "deny")
NOWHERE = "nowhere in the run"
MAX_STEPS = 12

Emit = Callable[[dict[str, Any]], None]
# What rivals need to re-ask one step: the messages before it, the assistant message it returned, the tool schemas.
Step = tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]


@dataclass
class _Gate:
    gate_id: str
    verdict: str | None = None
    resolved: threading.Event = field(default_factory=threading.Event)


class Session:
    """The demo agent loop (causeway.demo.agent) with the ledger rebuilt after every model response.

    A call to a watched tool whose decision has watch entries for that call's index waits for decide(). The
    constructor emits `hello` and `status idle`. `rivals_client` (default: client) answers rival samples and
    `why_client` (default: rivals_client) answers why(), so neither shares the agent's scripted queue in tests.
    `pace` seconds pass before each model call. `model` labels the run (default: the client's model). Gate ids are
    gate_prefix plus a number (g1, g2, ...).
    """

    def __init__(
        self,
        task: str,
        sandbox: str | Path,
        client: Any,
        watch: Iterable[str],
        rivals_k: int = 0,
        rivals_client: Any = None,
        tools: tuple[Tool, ...] = ALL,
        emit: Emit | None = None,
        pace: float = 0.0,
        max_steps: int = MAX_STEPS,
        model: str | None = None,
        why_client: Any = None,
        scenario: str | None = None,
        replay: bool = False,
        gate_prefix: str = "g",
    ) -> None:
        if rivals_k < 0:
            raise ValueError("rivals_k must be >= 0")
        self.task = task
        self.sandbox = Path(sandbox)
        self.client = client
        self.watch = list(dict.fromkeys(watch))
        self.rivals_k = rivals_k
        self.rivals_client = client if rivals_client is None else rivals_client
        self.why_client = self.rivals_client if why_client is None else why_client
        self.tools = tools
        self.pace = pace
        self.max_steps = max_steps
        self.model = model if model is not None else getattr(client, "model", "unknown")
        self.gate_prefix = gate_prefix
        self._send = emit or (lambda event: None)
        self._lock = threading.RLock()
        self._seq = 0
        self._state = "idle"
        self._messages: list[dict[str, Any]] = []
        self._ledger: list[dict[str, Any]] = []
        self._emitted = 0
        self._gates: dict[str, _Gate] = {}
        self._open: _Gate | None = None
        self._rivals_for: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._emit("hello", task=task, watch=list(self.watch), model=self.model, scenario=scenario, replay=replay)
        self._emit("status", state="idle")

    @property
    def state(self) -> str:
        """idle, running, waiting, done or error (§11.2 status)."""
        with self._lock:
            return self._state

    @property
    def started(self) -> bool:
        return self._thread is not None

    @property
    def running(self) -> bool:
        """True from start() until the run is done or failed, gates included."""
        return self.state in ("running", "waiting")

    def ledger(self) -> list[dict[str, Any]]:
        """The ledger as last rebuilt: every record emitted so far, in order."""
        with self._lock:
            return list(self._ledger)

    def start(self) -> None:
        """Run the loop in a daemon thread. One run per session: a second start raises RuntimeError."""
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("this session has already run; start a new session")
            self._thread = threading.Thread(target=self._run, name="causeway-session", daemon=True)
            self._set_state("running")
            self._thread.start()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the run's thread. True when it has ended, or was never started."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def decide(self, gate_id: str, verdict: str) -> bool:
        """Resolve an open gate with approve or deny. False for an unknown or already resolved gate."""
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
        with self._lock:
            gate = self._gates.get(gate_id)
            if gate is None or gate.verdict is not None:
                return False
            gate.verdict = verdict
            self._emit("resolved", gate_id=gate_id, verdict=verdict)
        gate.resolved.set()
        return True

    def stop(self) -> None:
        """End the run: an open gate is denied, later gated calls of the step are denied, and no model call follows."""
        self._stop.set()
        with self._lock:
            gate = self._open
        if gate is not None:
            self.decide(gate.gate_id, "deny")

    def why(self, question: str) -> threading.Thread:
        """Ask why_client about the current ledger in a thread. Emits `why`, or `error` when the call fails."""
        with self._lock:
            records = list(self._ledger)
        thread = threading.Thread(target=self._why, args=(question, records), name="causeway-why", daemon=True)
        thread.start()
        return thread

    def _emit(self, kind: str, **fields: Any) -> None:
        with self._lock:
            event = {"seq": self._seq, "type": kind, **fields}
            self._seq += 1
            self._send(event)

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state
            self._emit("status", state=state)

    def _run(self) -> None:
        try:
            self._loop()
            self._publish()
        except Exception as err:  # a failed model call ends the run with an error event, never the server
            self._emit("error", message=self._describe(err))
            self._set_state("error")
        else:
            self._set_state("done")

    def _loop(self) -> None:
        schemas = [tool.schema for tool in self.tools]
        self._messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": self.task}]
        for _ in range(self.max_steps):
            if self._stop.wait(self.pace):
                return
            before = list(self._messages)
            response = self.client.complete(self._messages, tools=schemas, reasoning_effort=REASONING_EFFORT)
            message = message_of(response)
            self._messages.append(message)
            decision = self._publish()
            calls = message.get("tool_calls") or []
            if not calls:
                self._emit("final", decision=decision["id"], text=decision["text"] or "")
                return
            for index, call in enumerate(calls):
                result = self._call(index, call, decision, (before, message, schemas))
                self._messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": result})
            if self._stop.is_set():
                return

    def _call(self, index: int, call: dict[str, Any], decision: dict[str, Any], step: Step) -> str:
        """Run one call, after a gate when its arguments are tainted (§11.1). A denied call returns DENIED."""
        function = call.get("function") or {}
        name = function.get("name", "")
        taints = self._taints(decision, index) if name in self.watch else []
        if taints and not self._gate(decision, index, taints, step):
            return DENIED
        return execute(self.sandbox, name, function.get("arguments", ""), self.tools)

    def _gate(self, decision: dict[str, Any], index: int, taints: list[dict[str, Any]], step: Step) -> bool:
        """Emit `gate`, start rivals for the decision if asked, and block until decide() or stop(). True on approve."""
        with self._lock:
            if self._stop.is_set():
                return False
            gate = _Gate(f"{self.gate_prefix}{len(self._gates) + 1}")
            self._gates[gate.gate_id] = gate
            self._open = gate
            action = decision["actions"][index]
            self._emit(
                "gate",
                gate_id=gate.gate_id,
                decision=decision["id"],
                action=index,
                call={"name": action["name"], "args": action["args"]},
                taints=taints,
            )
            self._set_state("waiting")
        if self.rivals_k > 0 and decision["id"] not in self._rivals_for:
            self._rivals_for.add(decision["id"])
            threading.Thread(
                target=self._rivals, args=(decision["id"], *step), name="causeway-rivals", daemon=True
            ).start()
        gate.resolved.wait()
        with self._lock:
            self._open = None
            if not self._stop.is_set():
                self._set_state("running")
        return gate.verdict == "approve"

    def _taints(self, decision: dict[str, Any], index: int) -> list[dict[str, Any]]:
        """The decision's watch entries for action index, each plus the leaf or atom value and its source's label."""
        entries = [entry for entry in decision.get("watch") or [] if entry["action"] == index]
        if not entries:
            return []
        values = {(cause["action"], cause["path"]): cause.get("value") for cause in decision["causes"]}
        labels = cli.origins(self._ledger)
        return [
            {**entry, "value": values.get((index, entry["path"])), "label": _label(entry["source"], labels)}
            for entry in entries
        ]

    def _publish(self) -> dict[str, Any] | None:
        """Rebuild the ledger from the messages so far, emit each record not emitted yet, and return the last decision.

        A record's tags depend only on earlier records, so a record never changes once emitted.
        """
        records = transcript.to_ledger(self._messages, model=self.model, watch=self.watch)
        labels = cli.origins(records)
        with self._lock:
            self._ledger = records
            for record in records[self._emitted:]:
                self._emit("record", record=record, origin=labels.get(record.get("id", "")))
            self._emitted = len(records)
        return next((record for record in reversed(records) if record["kind"] == "decision"), None)

    def _rivals(self, decision_id: str, before: list, message: dict[str, Any], schemas: list) -> None:
        try:
            record = rivals.rivals(
                before, schemas, message, self.rivals_k, self.rivals_client, reasoning_effort=REASONING_EFFORT
            )
        except Exception as err:  # rivals are advisory; their failure is reported, the gate stays open
            self._emit("error", message=f"rivals for {decision_id} failed: {self._describe(err)}")
            return
        self._emit("rivals", decision=decision_id, rivals=record)

    def _why(self, question: str, records: list[dict[str, Any]]) -> None:
        try:
            result = explain.why(records, question, self.why_client)
        except Exception as err:  # reported as an event; the run goes on
            self._emit("error", message=f"why failed: {self._describe(err)}")
            return
        result["model"] = getattr(self.why_client, "model", self.model)
        self._emit("why", question=question, result=result)

    def _describe(self, err: BaseException) -> str:
        """Error text for an `error` event, with any client's API key replaced."""
        text = f"{type(err).__name__}: {err}"
        for client in (self.client, self.rivals_client, self.why_client):
            key = getattr(client, "api_key", None)
            if isinstance(key, str) and key:
                text = text.replace(key, "[redacted]")
        return text


def _label(source: str | None, labels: dict[str, str]) -> str:
    """Where a taint came from: the source's origin, its id when it has none (an earlier decision), or nowhere."""
    if source is None:
        return NOWHERE
    return labels.get(source, source)
