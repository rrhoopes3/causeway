"""`python -m causeway.companion.muse`: Causeway as a custom MCP server for Meta's Muse agent (SPEC §12).

Muse is asked, by these instructions and a standing rule, to call `checkpoint` before it sends, buys, books, posts,
deletes or submits anything for the user, passing the user's request, the raw text of what it read, and the action.
Nothing enforces that: a call it skips is a call Causeway never sees. The action is tagged (§12.1). A tainted one opens a gate
on the user's Causeway page, on their phone, PC or headset, and waits for Approve or Deny. Muse is never given that
page's address or any secret. It runs in Meta's cloud, so it reaches the MCP endpoint through `--tunnel`. Two things
face the internet on that port: the endpoint, behind the MCP key, and the owner's phone page (§12.3), behind a second
key that only this console shows. The companion page stays on 127.0.0.1 as in §11.3.

Needs the `muse` extra: pip install -e .[muse]
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Annotated, Any

import anyio
import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from causeway.client import DEFAULT_BASE_URL, DEFAULT_MODEL, ChatClient, load_api_key
from causeway.companion.__main__ import NoModel, quest
from causeway.companion.checkpoint import Checkpoint, Standby
from causeway.companion.phone import OWNER_KEY_NAME, OwnerPage, check_owner_key, phone_lines
from causeway.companion.server import DEFAULT_PORT, Companion

KEY_NAME = "CAUSEWAY_MCP_KEY"
DEFAULT_MCP_PORT = 8771
DEFAULT_WAIT = 20.0  # the MCP TypeScript SDK gives up on a call at 60 s, and Muse's own limit is unknown
MCP_PATH = "/mcp"
KEY_PREFIX = "/k/"
DISCOVERY = "/.well-known/"
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
SECRET_PATH = re.compile(r"/([ko])/[^/\s\"']+")
TUNNEL_TIMEOUT = 45.0
MAX_LOGGED = 120
MAX_INITIALIZE = 64_000

INSTRUCTIONS = """Causeway shows the user where the values in your actions came from, and lets them approve or deny an action on their Causeway page, on their phone, PC or headset, before you take it. The user already has that page. You are never given its address.

Before any action that sends, buys, books, pays, posts, deletes, submits a form, or otherwise acts outside the conversation for the user, call `checkpoint`. Check one action at a time and finish with its verdict before the next, because a new checkpoint replaces the one before it.
- request: the user's request for this errand, word for word as they wrote or said it.
- sources: everything you read that informed the action (emails, web pages, files, notes, tool results), each with a short label, such as a file name rather than a whole URL, and its raw text copied exactly. Do not summarize or paraphrase: Causeway looks for each value in what you read, and a paraphrased source makes its values look invented.
- action and arguments: the exact tool name and arguments you are about to use.

Then follow the verdict:
- "clear" or "approve": take the action.
- "waiting": the user has not decided. Show them the findings right away, tell them to decide on their Causeway page, then call `checkpoint_result` with the gate_id until there is a verdict. Do not act meanwhile.
- "deny": do not act. Tell the user what you were going to do and show them the findings.
- "superseded" or "unknown": the check was replaced or cleared, and the user did not deny it. Do not act. Call `checkpoint` again if the action is still wanted.

If the user tells you in the conversation to go ahead or to stop, do what they said, and say that Causeway did not record that decision.

If `checkpoint` or `checkpoint_result` fails or cannot be reached, do not act, and tell the user so."""


class Source(BaseModel):
    label: str = Field(description="Short name for what you read, e.g. 'team.md' or 'email from Dana, Oct 28'. For a web page, its file name or title rather than the whole URL.")
    text: str = Field(description="The raw text you read, copied exactly. Not a summary.")


def _quoted(value: Any) -> str:
    """Text that came from the agent, for one console line: control characters stripped, capped, quoted, ASCII."""
    return json.dumps("".join(char for char in str(value) if char.isprintable())[:MAX_LOGGED])


class PrintableLog(logging.Filter):
    """The SDK logs names the agent chose, such as an unknown tool's, and they reach the console as they came. This
    strips anything in them that could drive the console."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = "".join(char for char in record.getMessage() if char.isprintable() or char == "\n")
        record.args = None
        return True


def log_call(tool: str, session: Checkpoint | None, outcome: dict[str, Any], started: float, user_agent: str) -> None:
    """One console line per tool call. Names and counts only: never a source's text, an argument's value or a key."""
    action, arguments, sources = (session.action, session.arguments, session.sources) if session else ("-", {}, [])
    print(
        f"{time.strftime('%H:%M:%S')} tool={_quoted(tool)} action={_quoted(action)} args={len(arguments)} "
        f"sources={len(sources)} source_chars={sum(len(source['text']) for source in sources)} "
        f"verdict={outcome['verdict']} secs={time.monotonic() - started:.1f} "
        f"gate={_quoted(outcome['gate_id'] or '-')} ua={_quoted(user_agent)}",
        flush=True,
    )


def log_client(body: bytes, user_agent: str) -> None:
    """One console line for an `initialize` request: who connected and what it offers, elicitation for one."""
    try:
        message = json.loads(body)
        if message["method"] != "initialize":
            return
        params = message["params"]
        client = params["clientInfo"]
        line = (
            f"client={_quoted(client['name'])} version={_quoted(client.get('version', '-'))} "
            f"protocol={_quoted(params.get('protocolVersion', '-'))} "
            f"capabilities={_quoted(','.join(sorted(params.get('capabilities', {}))))}"
        )
    except (ValueError, KeyError, TypeError, AttributeError):  # not an initialize we can read; the SDK answers it
        return
    print(f"{time.strftime('%H:%M:%S')} initialize {line} ua={_quoted(user_agent)}", flush=True)


PRINTABLE_LOG = PrintableLog()


class Muse:
    """The checkpoint tools over a Companion: each checkpoint replaces the companion's session, so the page shows
    the newest action. Calls block for at most `wait` seconds; then the verdict is `waiting`."""

    def __init__(self, companion: Companion, wait: float = DEFAULT_WAIT, why_client: Any = None) -> None:
        self.companion = companion
        self.wait = wait
        self.why_client = why_client

    def checkpoint(self, request: str, action: str, arguments: dict[str, Any], sources: list[dict[str, str]], user_agent: str = "-") -> dict[str, Any]:
        started = time.monotonic()
        session = self.companion.adopt(
            lambda emit: Checkpoint(request, action, arguments, sources, emit=emit, why_client=self.why_client)
        )
        session.join(self.wait)
        outcome = session.outcome()
        log_call("checkpoint", session, outcome, started, user_agent)
        return outcome

    def result(self, gate_id: str, user_agent: str = "-") -> dict[str, Any]:
        started = time.monotonic()
        session = self.companion.current()
        if not isinstance(session, Checkpoint) or session.outcome()["gate_id"] != gate_id:
            outcome = {"verdict": "unknown", "gate_id": gate_id, "findings": [], "message": "No such open check. It was replaced by a newer checkpoint or the server restarted. Call checkpoint again before acting."}
            log_call("checkpoint_result", None, outcome, started, user_agent)
            return outcome
        session.join(self.wait)
        outcome = session.outcome()
        log_call("checkpoint_result", session, outcome, started, user_agent)
        return outcome


def _user_agent(ctx: Context) -> str:
    try:
        return ctx.request_context.request.headers.get("user-agent", "-")
    except (ValueError, AttributeError):  # not inside an HTTP request
        return "-"


class LoggedMCP(FastMCP):
    """FastMCP that also logs a tool call that fails: parameters it rejects never reach the tool, and the console
    must still show the call came."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        started = time.monotonic()
        try:
            return await super().call_tool(name, arguments)
        except Exception:
            log_call(name, None, {"verdict": "error", "gate_id": None}, started, _user_agent(self.get_context()))
            raise


def build_mcp(muse: Muse) -> FastMCP:
    """The FastMCP server with `checkpoint` and `checkpoint_result`. Stateless JSON over streamable HTTP. Host checks
    are off because the server is reached through a tunnel's hostname; the key (KeyGate) is what guards it."""
    mcp = LoggedMCP(
        "causeway",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path=MCP_PATH,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    for handler in logging.getLogger().handlers:  # FastMCP has just set these up
        handler.addFilter(PRINTABLE_LOG)

    @mcp.tool()
    async def checkpoint(
        request: Annotated[str, Field(description="The user's request for this errand, word for word as they wrote or said it.")],
        action: Annotated[str, Field(description="The exact name of the tool you are about to use.")],
        arguments: Annotated[dict[str, Any], Field(description="The exact arguments you are about to pass to that tool.")],
        ctx: Context,
        sources: Annotated[list[Source], Field(description="Everything you read that informed the action, as raw text. Without them, values not in the request show as UNSOURCED.")] = [],
    ) -> dict[str, Any]:
        """Check an action with the user before you take it, one action at a time. Call this before anything that
        sends, buys, books, pays, posts, deletes or submits for the user. If this call fails or cannot be reached, do
        not act and say so. Returns a verdict: clear or approve (go ahead), deny (do not act), waiting (show the user
        the findings now, tell them to decide on their Causeway page, then call checkpoint_result with gate_id until
        there is a verdict), or superseded (replaced or cleared, not denied by the user: do not act, and check again
        if the action is still wanted)."""
        return await anyio.to_thread.run_sync(
            muse.checkpoint, request, action, arguments, [source.model_dump() for source in sources], _user_agent(ctx)
        )

    @mcp.tool()
    async def checkpoint_result(gate_id: str, ctx: Context) -> dict[str, Any]:
        """Keep waiting for the user's decision on a checkpoint whose verdict was waiting. Call it again until the
        verdict is no longer waiting, and do not act before then."""
        return await anyio.to_thread.run_sync(muse.result, gate_id, _user_agent(ctx))

    return mcp


class ClientLog:
    """ASGI middleware: reads each POST's body as it goes by and logs an `initialize`. In stateless mode this is the
    only place to see it, since a tool call gets a new session that knows nothing of the client. Only the first
    MAX_INITIALIZE bytes are kept, far more than an initialize needs."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        body = bytearray()

        async def tee() -> dict[str, Any]:
            message = await receive()
            if message["type"] == "http.request":
                if len(body) < MAX_INITIALIZE:
                    body.extend(message.get("body", b""))
                if not message.get("more_body"):
                    log_client(bytes(body), dict(scope.get("headers", [])).get(b"user-agent", b"-").decode("latin-1"))
            return message

        await self.app(scope, tee, send)


async def _refuse(send: Any, status: int, error: str, *headers: tuple[bytes, bytes]) -> None:
    body = json.dumps({"error": error}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            *headers,
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _at(scope: dict[str, Any], path: str) -> dict[str, Any]:
    """The scope with its path replaced and without a trailing slash."""
    path = path.rstrip("/") or "/"
    return {**scope, "path": path, "raw_path": path.encode("utf-8")}


class KeyGate:
    """ASGI middleware: a request needs `Authorization: Bearer <key>` or `X-API-Key: <key>`, or a path under
    /k/<key>/ for clients that cannot send a header. Anything else gets 401, except a path under /.well-known/: that
    gets 404, so an OAuth-aware client finds no metadata instead of concluding OAuth is required. A trailing slash is
    dropped here, because the redirect the app would answer with has lost the /k/<key>/ prefix. Lifespan events pass
    through."""

    def __init__(self, app: Any, key: str) -> None:
        self.app = app
        self.key = key

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        given, _, rest = path.removeprefix(KEY_PREFIX).partition("/")
        if path.startswith(KEY_PREFIX) and hmac.compare_digest(given.encode("utf-8", "replace"), self.key.encode()):
            await self.app(_at(scope, f"/{rest}"), receive, send)
            return
        if self._authorized(scope):
            await self.app(_at(scope, path), receive, send)
            return
        if path.startswith(DISCOVERY):
            await _refuse(send, 404, "not found")
            return
        await _refuse(send, 401, "missing or wrong key", (b"www-authenticate", b'Bearer realm="causeway"'))

    def _authorized(self, scope: dict[str, Any]) -> bool:
        headers = {name.lower(): value.decode("latin-1") for name, value in scope.get("headers", [])}
        auth = headers.get(b"authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get(b"x-api-key", "").strip()
        return bool(token) and hmac.compare_digest(token.encode(), self.key.encode())


def app_for(muse: Muse, key: str) -> Any:
    """The ASGI app to serve: the MCP endpoint behind the key."""
    return KeyGate(ClientLog(build_mcp(muse).streamable_http_app()), key)


def scrubbed(line: str) -> str:
    """A cloudflared log line without the key in any /k/<key>/ or /o/<key>/ path: its errors name the request's URL."""
    return SECRET_PATH.sub(r"/\1/[redacted]", line)


def tunnel(port: int) -> tuple[subprocess.Popen | None, str | None]:
    """Start a cloudflared quick tunnel to 127.0.0.1:port and return (process, public base URL). The URL is None
    when cloudflared is missing or prints no URL in time; the process keeps running until terminated.

    cloudflared reads ~/.cloudflared/config.yaml even for a quick tunnel, and a named tunnel's ingress there (ending
    in `http_status:404`) overrides --url, so every request gets 404. It is given a config of its own instead."""
    config = Path(tempfile.gettempdir()) / "causeway-cloudflared.yml"
    config.write_text(f"url: http://127.0.0.1:{port}\n", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--no-autoupdate", "--config", str(config), "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("tunnel: cloudflared was not found on PATH", file=sys.stderr)
        return None, None
    found: list[str] = []
    ready = threading.Event()

    def drain() -> None:  # keep reading so cloudflared never blocks on a full pipe
        for line in proc.stderr:  # type: ignore[union-attr]
            if " ERR " in line:
                print(f"cloudflared: {scrubbed(line.rstrip())}", file=sys.stderr)
            match = TUNNEL_URL.search(line)
            if match and not found:
                found.append(match.group(0))
                ready.set()
        ready.set()

    threading.Thread(target=drain, name="causeway-tunnel", daemon=True).start()
    ready.wait(TUNNEL_TIMEOUT)
    if not found:
        print("tunnel: cloudflared started but printed no trycloudflare URL", file=sys.stderr)
    return proc, found[0] if found else None


def connect_lines(public: str | None, port: int) -> list[str]:
    """What to give Muse. It runs in Meta's cloud, so a 127.0.0.1 address is never offered as its URL."""
    if public is None:
        return [
            f"local MCP endpoint: http://127.0.0.1:{port}{MCP_PATH}",
            "no public URL:      Muse cannot reach a local address, so it cannot connect. It needs --tunnel.",
        ]
    return [f"MCP URL for Muse:   {public}{MCP_PATH}", f"no header support?  {public}/k/<key>{MCP_PATH}"]


class Serving(uvicorn.Server):
    """uvicorn waits for calls in flight at exit, and a checkpoint call waits on its gate, so Ctrl+C ends the gate first.
    The agent gets `superseded` rather than a dropped connection. A thread, because a signal handler must not wait on a lock."""

    def __init__(self, config: uvicorn.Config, companion: Companion) -> None:
        super().__init__(config)
        self.companion = companion

    def handle_exit(self, sig: int, frame: Any) -> None:
        threading.Thread(target=lambda: self.companion.current().stop(), name="causeway-exit", daemon=True).start()
        super().handle_exit(sig, frame)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="causeway.companion.muse", description="Serve Causeway's checkpoint to Muse over MCP, gated in the companion.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"companion page on 127.0.0.1 (default {DEFAULT_PORT})")
    parser.add_argument("--mcp-port", type=int, default=DEFAULT_MCP_PORT, help=f"MCP endpoint on 127.0.0.1 (default {DEFAULT_MCP_PORT})")
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT, help=f"seconds a checkpoint call waits for your decision (default {DEFAULT_WAIT:g})")
    parser.add_argument("--tunnel", action="store_true", help="expose the MCP endpoint through a cloudflared quick tunnel")
    parser.add_argument("--quest", action="store_true", help="adb reverse the companion port and open it in the Quest browser")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="chat API for the companion's why questions (default: the Meta Model API)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="model for why questions")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Serves until Ctrl+C."""
    args = build_parser().parse_args(argv)
    key = load_api_key(name=KEY_NAME)
    generated = key is None
    key = key or secrets.token_urlsafe(24)
    owner_key = load_api_key(name=OWNER_KEY_NAME)
    owner_generated = owner_key is None
    owner_key = owner_key or secrets.token_urlsafe(24)
    try:
        check_owner_key(owner_key, key)
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    try:
        why_client: Any = ChatClient(base_url=args.base_url, model=args.model)
    except ValueError as err:
        why_client = NoModel(f"why needs a model: {err}")
    try:
        app = Companion(lambda emit, task: Standby(emit, task), port=args.port)
    except OSError as err:
        print(f"error: cannot listen on 127.0.0.1:{args.port}: {err}", file=sys.stderr)
        return 1
    threading.Thread(target=app.serve_forever, name="causeway-companion", daemon=True).start()
    muse = Muse(app, wait=args.wait, why_client=why_client)
    proc, public = tunnel(args.mcp_port) if args.tunnel else (None, None)
    page = OwnerPage(app_for(muse, key), app, owner_key, key, f"{public}{MCP_PATH}" if public else None)
    server = Serving(uvicorn.Config(page, host="127.0.0.1", port=args.mcp_port, log_level="warning"), app)
    print(f"causeway companion: {app.url}")
    print("\n".join(connect_lines(public, args.mcp_port)))
    print(f"key:                {key}" + (f"   (generated; set {KEY_NAME} in ~/.config/causeway/.env to keep one)" if generated else ""))
    print("\n".join(phone_lines(public, args.mcp_port, owner_key, owner_generated, sys.stdout.encoding)), flush=True)
    if args.quest:
        quest(app.port)
    try:
        server.run()
    finally:
        if proc is not None:
            proc.terminate()
        app.close()
        if hasattr(why_client, "close"):
            why_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
