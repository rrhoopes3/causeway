"""`python -m causeway.companion`: serve live mode for one demo scenario on 127.0.0.1 (SPEC §11.3).

The scenario's sandbox is copied to a temporary folder for each session and removed when the session is replaced
or the server stops. --replay plays a recorded transcript's assistant messages instead of calling a model.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from causeway.cli import load_transcript
from causeway.client import DEFAULT_BASE_URL, DEFAULT_MODEL, ChatClient, FakeClient
from causeway.companion.server import DEFAULT_PORT, Companion
from causeway.companion.session import MAX_STEPS, Emit, Session
from causeway.demo.__main__ import _resolve

DEFAULT_SCENARIO = "address_swap"
DEFAULT_WATCH = "send_email,write_file"
REPLAY_PACE = 1.5
RETIRE_WAIT = 150.0
ADB_TIMEOUT = 20.0
QUEST_BROWSER = "com.oculus.browser"


def replay_responses(path: str | Path) -> list[dict[str, Any]]:
    """Each assistant message of a transcript, in order, wrapped as a chat completion response."""
    messages, _ = load_transcript(str(path))
    return [
        {
            "choices": [
                {"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}
            ]
        }
        for message in messages
        if message.get("role") == "assistant"
    ]


def replay_client(path: str | Path) -> FakeClient:
    """A FakeClient that answers with the transcript's own assistant messages, in order."""
    return FakeClient(replay_responses(path))


def transcript_model(path: str | Path) -> str | None:
    """The `model` a recorded transcript names, if any."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    model = data.get("model") if isinstance(data, dict) else None
    return model if isinstance(model, str) and model else None


class NoModel:
    """Client for rivals and why when no model can be reached: every call fails with the reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def complete(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(self.reason)


class Sessions:
    """Makes sessions for one scenario, each in its own temporary copy of the scenario's sandbox."""

    def __init__(
        self,
        folder: Path,
        make_client: Callable[[], Any],
        watch: list[str],
        helper: Any = None,
        rivals_k: int = 0,
        pace: float = 0.0,
        max_steps: int = MAX_STEPS,
        model: str | None = None,
        replay: bool = False,
    ) -> None:
        self.folder = Path(folder)
        self.task = (self.folder / "task.txt").read_text(encoding="utf-8").strip()
        self.make_client = make_client
        self.watch = watch
        self.helper = helper
        self.rivals_k = rivals_k
        self.pace = pace
        self.max_steps = max_steps
        self.model = model
        self.replay = replay
        self._lock = threading.Lock()
        self._workdirs: dict[Session, Path] = {}

    def new(self, emit: Emit, task: str | None = None) -> Session:
        """A fresh session on a fresh copy of the sandbox. task None means the scenario's task.txt."""
        workdir = Path(tempfile.mkdtemp(prefix=f"causeway-companion-{self.folder.name}-"))
        try:
            shutil.copytree(self.folder / "sandbox", workdir / "sandbox")
        except OSError:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        session = Session(
            task or self.task,
            workdir / "sandbox",
            self.make_client(),
            self.watch,
            rivals_k=self.rivals_k,
            rivals_client=self.helper,
            emit=emit,
            pace=self.pace,
            max_steps=self.max_steps,
            model=self.model,
            scenario=self.folder.name,
            replay=self.replay,
        )
        with self._lock:
            self._workdirs[session] = workdir
        return session

    def retire(self, session: Session) -> None:
        """Stop a session and remove its sandbox once its thread ends, without making the caller wait."""
        session.stop()
        threading.Thread(target=self._remove, args=(session,), name="causeway-retire", daemon=True).start()

    def close(self, wait: float = 2.0) -> None:
        """Remove every sandbox still on disk, after giving running sessions up to `wait` seconds to end."""
        with self._lock:
            sessions = list(self._workdirs)
        for session in sessions:
            self._remove(session, wait)

    def _remove(self, session: Session, wait: float = RETIRE_WAIT) -> None:
        """Remove a session's sandbox. The entry is dropped only after the removal, so close() never returns early."""
        session.join(wait)
        with self._lock:
            workdir = self._workdirs.get(session)
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
            with self._lock:
                self._workdirs.pop(session, None)


def quest(port: int) -> bool:
    """Forward the Quest's localhost:port to this machine with adb and open the page in the Quest browser.

    Prints what went wrong, and returns False, when adb is missing or a command fails.
    """
    url = f"http://localhost:{port}/"
    commands = (
        ["adb", "reverse", f"tcp:{port}", f"tcp:{port}"],
        ["adb", "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url, QUEST_BROWSER],
    )
    for command in commands:
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=ADB_TIMEOUT, check=False)
        except FileNotFoundError:
            print(f"quest: adb was not found on PATH. Install Android platform-tools, or open {url} on the Quest after `adb reverse tcp:{port} tcp:{port}`.", file=sys.stderr)
            return False
        except (OSError, subprocess.TimeoutExpired) as err:
            print(f"quest: `{' '.join(command)}` failed: {err}", file=sys.stderr)
            return False
        output = f"{done.stdout or ''}{done.stderr or ''}".strip()
        if done.returncode != 0 or "Error" in output:
            detail = output or f"exit code {done.returncode}"
            print(f"quest: `{' '.join(command)}` failed: {detail}. Is the headset connected and authorized (`adb devices`)?", file=sys.stderr)
            return False
    print(f"quest: opened {url} in the Quest browser")
    return True


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _non_negative_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number of seconds, got {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="causeway.companion", description="Run the demo agent live, gated by Causeway.")
    parser.add_argument("scenario", nargs="?", default=DEFAULT_SCENARIO, help=f"scenario folder, or a name under demo/scenarios (default {DEFAULT_SCENARIO})")
    parser.add_argument("--watch", metavar="TOOL,TOOL", default=DEFAULT_WATCH, help=f"tools whose tainted calls wait for you (default {DEFAULT_WATCH})")
    parser.add_argument("--rivals", metavar="K", type=_non_negative_int, default=0, help="rival samples at each gate (default 0: none)")
    parser.add_argument("--port", type=_non_negative_int, default=DEFAULT_PORT, help=f"port on 127.0.0.1 (default {DEFAULT_PORT})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="chat API base URL (default: the Meta Model API)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="model name sent with each request")
    parser.add_argument("--replay", metavar="TRANSCRIPT.json", help="play a recorded transcript's model replies instead of calling a model")
    parser.add_argument("--pace", metavar="SECONDS", type=_non_negative_float, help=f"wait before each model reply (default {REPLAY_PACE} in replay, else 0)")
    parser.add_argument("--quest", action="store_true", help="adb reverse the port and open the page in the Quest browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m causeway.companion`. Serves until Ctrl+C."""
    args = build_parser().parse_args(argv)
    folder = _resolve(args.scenario)
    if not (folder / "task.txt").is_file() or not (folder / "sandbox").is_dir():
        print(f"error: {folder} needs task.txt and sandbox/", file=sys.stderr)
        return 2
    watch = [name.strip() for name in args.watch.split(",") if name.strip()]
    clients: list[Any] = []
    if args.replay:
        try:
            responses = replay_responses(args.replay)
            model = transcript_model(args.replay) or args.model
        except (OSError, ValueError) as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        try:
            helper: Any = ChatClient(base_url=args.base_url, model=args.model)
            clients.append(helper)
        except ValueError as err:
            helper = NoModel(f"rivals and why need a model, and none is available in this replay: {err}")
            print("note: no MODEL_API_KEY, so rivals and why will report an error in this replay", file=sys.stderr)
        sessions = Sessions(
            folder,
            lambda: replay_client(args.replay),
            watch,
            helper=helper,
            rivals_k=args.rivals,
            pace=REPLAY_PACE if args.pace is None else args.pace,
            max_steps=max(MAX_STEPS, len(responses)),
            model=model,
            replay=True,
        )
    else:
        try:
            client = ChatClient(base_url=args.base_url, model=args.model)
        except ValueError as err:  # no MODEL_API_KEY; the message never includes a key
            print(f"error: {err}", file=sys.stderr)
            return 2
        clients.append(client)
        sessions = Sessions(
            folder, lambda: client, watch, rivals_k=args.rivals, pace=args.pace or 0.0, model=args.model
        )
    try:
        app = Companion(sessions.new, port=args.port, retire=sessions.retire)
    except OSError as err:
        print(f"error: cannot listen on 127.0.0.1:{args.port}: {err}", file=sys.stderr)
        sessions.close()
        for open_client in clients:
            open_client.close()
        return 1
    print(f"causeway companion: {app.url}")
    print(f"scenario {folder.name}, watching {', '.join(watch) or 'nothing'}{', replay' if args.replay else ''}. Ctrl+C stops.")
    if args.quest:
        quest(app.port)
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        print("stopping")
    finally:
        app.close()
        sessions.close()
        for open_client in clients:
            open_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
