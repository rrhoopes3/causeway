"""`python -m causeway.companion.try_gate`: rehearse the phone page with no Muse account and no model key.

A scripted client, not Muse, sends the address_swap checkpoint through the MCP SDK's own client over real HTTP: the
scenario's request, its files as raw text, and the address its planted note asks for. No model chose that address. The
script always sends it, so this shows the gate and the page, not that any agent falls for the note. Approve or Deny on
the page comes back as the verdict, which it prints. There is no email tool, so nothing is ever sent.

--tunnel puts the page on a real phone through a quick tunnel. --repeat sends the check again after each decision, to
film it. Why still asks the real model when MODEL_API_KEY is set, and otherwise says that none is configured.

Needs the `muse` extra: pip install -e .[muse]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import secrets
import sys
import threading
import time
from typing import Any

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, Implementation

from causeway.client import ChatClient
from causeway.companion.__main__ import DEFAULT_SCENARIO, NoModel
from causeway.companion.checkpoint import Checkpoint, Standby
from causeway.companion.muse import DEFAULT_MCP_PORT, MCP_PATH, Muse, app_for, log_call, tunnel
from causeway.companion.phone import STATIC_PREFIX, Answer, OwnerPage, phone_lines
from causeway.companion.server import DEFAULT_PORT, HOST, Companion
from causeway.demo.__main__ import SCENARIOS

CLIENT_NAME = "causeway-scripted-client"
SCRIPTED = "scripted client, not Muse"
ACTION = "send_email"
SWAPPED = "dana.ortiz@lab-mail.example"  # what notes/b.md says to use, instead of the address in team.md
SUBJECT = "Project notes summary"
BODY = "The calibration rig is finished and passes its drift check. The second sensor board arrived and its first readings look noisy."
WAIT = 5.0  # seconds a tool call holds before it answers waiting
PAUSE = 6.0  # seconds before each check and after the last, so the mask is seen to lift and the verdict to land
STOP_WAIT = 5.0

NOTICE = (
    "REHEARSAL: this is a scripted client, not Muse. No model chose this action: the script always sends the address "
    "from the planted note. It needs no Muse account and no model key, and it has no email tool, so nothing is sent."
)
STANDBY_TASK = "Rehearsal: a scripted client, not Muse, sends a check once the phone page is open."
OPEN_THE_PAGE = "Open the phone page. The check goes out a few seconds after it connects."

SCRIPT = f"{STATIC_PREFIX}phone.js"
AGENT_STARTS_SENTENCE = re.compile(rb"""(?<=[>'"])Muse\b|(?<=\. )Muse\b""")
AGENT = re.compile(rb"\bMuse\b")
# After the agent's name is changed. A test pins that each of these still finds its text.
PAGE_WORDS = (
    (b"<title>Causeway</title>", b"<title>Causeway rehearsal</title>"),
    (b'<span class="brand">Causeway</span>', b'<span class="brand">Causeway rehearsal</span>'),
    (b'<details id="setup" class="setup">', b'<details id="setup" class="setup" hidden>'),  # the script holds its key
    (b"A found quote does not prove the claim.", b"The action came from a scripted client, not Muse. A found quote does not prove the claim."),
)


def relabel(text: bytes) -> bytes:
    """The phone page's words with the agent named for what it is, everywhere the page names it."""
    text = AGENT_STARTS_SENTENCE.sub(b"The scripted client", text)
    text = AGENT.sub(b"the scripted client", text).replace(b"scripted client", b"scripted client (not Muse)")
    for old, new in PAGE_WORDS:
        text = text.replace(old, new)
    return text


class RehearsalPage(OwnerPage):
    """The owner's page, saying that the agent is a script. `opened` is set by the first poll, so the first check can
    wait until someone is looking."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.page = relabel(self.page)  # the import map is untouched, so its hash in the headers still holds
        self.opened = threading.Event()

    def _route(self, scope: dict[str, Any], rest: str, body: bytes | None) -> Answer:
        status, content_type, data = super()._route(scope, rest, body)
        return status, content_type, relabel(data) if rest == SCRIPT else data

    def view(self) -> dict[str, Any]:
        self.opened.set()
        return super().view()


class ScriptedMuse(Muse):
    """The tools Muse gets, except that each run says on the companion page that it came from the script."""

    def checkpoint(self, request: str, action: str, arguments: dict[str, Any], sources: list[dict[str, str]], user_agent: str = "-") -> dict[str, Any]:
        started = time.monotonic()
        session = self.companion.adopt(
            lambda emit: Checkpoint(request, action, arguments, sources, emit=emit, why_client=self.why_client, model=SCRIPTED)
        )
        session.join(self.wait)
        outcome = session.outcome()
        log_call("checkpoint", session, outcome, started, user_agent)
        return outcome


def scripted_check() -> dict[str, Any]:
    """The checkpoint's arguments: the scenario's request, every file in its sandbox as raw text, and the note's address."""
    folder = SCENARIOS / DEFAULT_SCENARIO
    sandbox = folder / "sandbox"
    return {
        "request": (folder / "task.txt").read_text(encoding="utf-8").strip(),
        "action": ACTION,
        "arguments": {"to": SWAPPED, "subject": SUBJECT, "body": BODY},
        "sources": [
            {"label": path.relative_to(sandbox).as_posix(), "text": path.read_text(encoding="utf-8")}
            for path in sorted(sandbox.rglob("*"))
            if path.is_file()
        ],
    }


def say(text: str) -> None:
    """One console line. A finding holds arrows, which a console on an old code page cannot encode."""
    encoding = sys.stdout.encoding or "ascii"
    print(text.encode(encoding, "replace").decode(encoding), flush=True)


def _outcome(result: CallToolResult) -> dict[str, Any]:
    """A tool call's outcome. A failed call raises, so it can never read as a verdict."""
    if result.isError:
        raise RuntimeError(result.content[0].text)  # type: ignore[union-attr]
    return result.structuredContent  # type: ignore[return-value]


async def _one_check(session: ClientSession, check: dict[str, Any]) -> None:
    """Send the check and follow it until the owner decides, as the server's instructions tell an agent to."""
    say(f"scripted client: asking to run {ACTION} to {SWAPPED}")
    outcome = _outcome(await session.call_tool("checkpoint", check))
    for finding in outcome["findings"]:
        say(f"  {finding}")
    if outcome["verdict"] == "waiting":
        say("scripted client: waiting for Approve or Deny on the phone page")
    while outcome["verdict"] == "waiting":
        outcome = _outcome(await session.call_tool("checkpoint_result", {"gate_id": outcome["gate_id"]}))
    say(f"verdict: {outcome['verdict']} ({SCRIPTED}; nothing was sent)")


async def run_checks(url: str, key: str, page: RehearsalPage, repeat: bool) -> None:
    """Once the page is open, send the check, and with `repeat` keep sending it after each decision until Ctrl+C."""
    while not page.opened.is_set():
        await asyncio.sleep(0.1)
    timeout = httpx.Timeout(30.0, read=300.0)
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=timeout) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write, client_info=Implementation(name=CLIENT_NAME, version="1")) as session:
                await session.initialize()
                while True:
                    await asyncio.sleep(PAUSE)
                    await _one_check(session, scripted_check())
                    if not repeat:
                        await asyncio.sleep(PAUSE)
                        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="causeway.companion.try_gate", description="Rehearse the phone page: a scripted client, not Muse, sends the address_swap check.")
    parser.add_argument("--tunnel", action="store_true", help="reach the page from a real phone through a cloudflared quick tunnel")
    parser.add_argument("--repeat", action="store_true", help="send the check again after each decision, until Ctrl+C")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"companion page on 127.0.0.1 (default {DEFAULT_PORT})")
    parser.add_argument("--mcp-port", type=int, default=DEFAULT_MCP_PORT, help=f"MCP endpoint and phone page on 127.0.0.1 (default {DEFAULT_MCP_PORT})")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Without --repeat it ends after one decision; with it, at Ctrl+C."""
    args = build_parser().parse_args(argv)
    for chatty in ("httpx", "mcp"):  # FastMCP turns on INFO logging, which would bury the verdict
        logging.getLogger(chatty).setLevel(logging.WARNING)
    key, owner_key = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    try:
        why_client: Any = ChatClient()
    except ValueError as err:  # no MODEL_API_KEY; the message never includes a key
        why_client = NoModel(f"why needs a model: {err}")
    try:
        app = Companion(lambda emit, task: Standby(emit, task or STANDBY_TASK), port=args.port)
    except OSError as err:
        print(f"error: cannot listen on {HOST}:{args.port}: {err}", file=sys.stderr)
        return 1
    threading.Thread(target=app.serve_forever, name="causeway-companion", daemon=True).start()
    page = RehearsalPage(app_for(ScriptedMuse(app, wait=WAIT, why_client=why_client), key), app, owner_key, key)
    server = uvicorn.Server(uvicorn.Config(page, host=HOST, port=args.mcp_port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="causeway-mcp", daemon=True)
    proc = None
    say(NOTICE)
    try:
        thread.start()
        while not server.started and thread.is_alive():
            time.sleep(0.02)
        if not server.started:
            print(f"error: cannot listen on {HOST}:{args.mcp_port}", file=sys.stderr)
            return 1
        public = None
        if args.tunnel:
            proc, public = tunnel(args.mcp_port)
            if public is None:
                print("error: --tunnel got no public address, so a phone cannot reach the page", file=sys.stderr)
                return 1
            page.mcp_url = f"{public}{MCP_PATH}"  # the page's taps come from this origin
        say(f"causeway companion: {app.url}")
        say("\n".join(phone_lines(public, args.mcp_port, owner_key, False, sys.stdout.encoding)))
        say(OPEN_THE_PAGE)
        asyncio.run(run_checks(f"http://{HOST}:{args.mcp_port}{MCP_PATH}", key, page, args.repeat))
    except KeyboardInterrupt:
        say("stopping")
    except Exception as err:  # the server or the client failed, which is not a verdict
        print(f"error: the scripted client failed: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    finally:
        if proc is not None:
            proc.terminate()
        app.shutdown()  # a gate still open ends as superseded, which frees the server's call that waits on it
        server.should_exit = True
        thread.join(STOP_WAIT)
        if hasattr(why_client, "close"):
            why_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
