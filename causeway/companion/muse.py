"""`python -m causeway.companion.muse`: Causeway as a custom MCP server for Meta's Muse agent (SPEC §12).

Muse calls `checkpoint` before it sends, buys, books, posts, deletes or submits anything for the user, passing the
user's request, the raw text of what it read, and the action. The action is tagged (§12.1). A tainted one lifts the
companion's mask in the headset and waits for Approve or Deny. The MCP endpoint needs a key and is the only thing
meant to face the internet (through `--tunnel`); the companion page stays on 127.0.0.1 as in §11.3.

Needs the `muse` extra: pip install -e .[muse]
"""

from __future__ import annotations

import argparse
import hmac
import json
import re
import secrets
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from causeway.client import DEFAULT_BASE_URL, DEFAULT_MODEL, ChatClient, load_api_key
from causeway.companion.__main__ import NoModel, quest
from causeway.companion.checkpoint import Checkpoint, Standby
from causeway.companion.server import DEFAULT_PORT, Companion

KEY_NAME = "CAUSEWAY_MCP_KEY"
DEFAULT_MCP_PORT = 8771
DEFAULT_WAIT = 90.0
MCP_PATH = "/mcp"
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
TUNNEL_TIMEOUT = 45.0

INSTRUCTIONS = """Causeway shows the user where the values in your actions came from, and lets them approve or deny an action in their headset before you take it.

Before any action that sends, buys, books, pays, posts, deletes, submits a form, or otherwise acts outside the conversation for the user, call `checkpoint` and wait for its verdict:
- request: the user's request for this errand, word for word as they wrote or said it.
- sources: everything you read that informed the action (emails, web pages, files, notes, tool results), each with a short label and its raw text copied exactly. Do not summarize or paraphrase: Causeway matches values character for character, and a paraphrased source makes its values look invented.
- action and arguments: the exact tool name and arguments you are about to use.

Act only when the verdict is "clear" or "approve". On "deny", do not act; tell the user what you were going to do and show them the findings. On "waiting", call `checkpoint_result` with the gate_id to keep waiting, or show the user the findings and ask them directly."""


class Source(BaseModel):
    label: str = Field(description="Short name for what you read, e.g. 'team.md', 'email from Dana, Oct 28', or a URL.")
    text: str = Field(description="The raw text you read, copied exactly. Not a summary.")


class Muse:
    """The checkpoint tools over a Companion: each checkpoint replaces the companion's session, so the headset shows
    the newest action. Calls block for at most `wait` seconds; then the verdict is `waiting`."""

    def __init__(self, companion: Companion, wait: float = DEFAULT_WAIT, why_client: Any = None) -> None:
        self.companion = companion
        self.wait = wait
        self.why_client = why_client

    def checkpoint(self, request: str, action: str, arguments: dict[str, Any], sources: list[dict[str, str]]) -> dict[str, Any]:
        session = self.companion.adopt(
            lambda emit: Checkpoint(request, action, arguments, sources, emit=emit, why_client=self.why_client)
        )
        session.join(self.wait)
        return session.outcome()

    def result(self, gate_id: str) -> dict[str, Any]:
        session = self.companion.current()
        if not isinstance(session, Checkpoint) or session.outcome()["gate_id"] != gate_id:
            return {"verdict": "unknown", "gate_id": gate_id, "findings": [], "message": "No such open check. It was replaced by a newer checkpoint or the server restarted. Call checkpoint again before acting."}
        session.join(self.wait)
        return session.outcome()


def build_mcp(muse: Muse) -> FastMCP:
    """The FastMCP server with `checkpoint` and `checkpoint_result`. Stateless JSON over streamable HTTP. Host checks
    are off because the server is reached through a tunnel's hostname; the key (KeyGate) is what guards it."""
    mcp = FastMCP(
        "causeway",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path=MCP_PATH,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    async def checkpoint(request: str, action: str, arguments: dict[str, Any], sources: list[Source]) -> dict[str, Any]:
        """Check an action with the user before you take it. Call this before anything that sends, buys, books,
        pays, posts, deletes or submits for the user. Pass the user's request word for word, every source you read
        that informed the action as raw text, and the exact action name and arguments. Returns a verdict: clear or
        approve (go ahead), deny (do not act), or waiting (call checkpoint_result with gate_id)."""
        return await anyio.to_thread.run_sync(
            muse.checkpoint, request, action, arguments, [source.model_dump() for source in sources]
        )

    @mcp.tool()
    async def checkpoint_result(gate_id: str) -> dict[str, Any]:
        """Keep waiting for the user's decision on a checkpoint whose verdict was waiting."""
        return await anyio.to_thread.run_sync(muse.result, gate_id)

    return mcp


class KeyGate:
    """ASGI middleware: a request needs `Authorization: Bearer <key>` or `X-API-Key: <key>`, or a path under
    /k/<key>/ for clients that cannot send a header. Anything else gets 401. Lifespan events pass through."""

    def __init__(self, app: Any, key: str) -> None:
        self.app = app
        self.key = key
        self.prefix = f"/k/{key}"

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == self.prefix or path.startswith(self.prefix + "/"):
            inner = path[len(self.prefix):] or "/"
            await self.app({**scope, "path": inner, "raw_path": inner.encode("utf-8")}, receive, send)
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"error": "missing or wrong key"}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="causeway"'),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    def _authorized(self, scope: dict[str, Any]) -> bool:
        headers = {name.lower(): value.decode("latin-1") for name, value in scope.get("headers", [])}
        auth = headers.get(b"authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get(b"x-api-key", "").strip()
        return bool(token) and hmac.compare_digest(token.encode(), self.key.encode())


def app_for(muse: Muse, key: str) -> Any:
    """The ASGI app to serve: the MCP endpoint behind the key."""
    return KeyGate(build_mcp(muse).streamable_http_app(), key)


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
                print(f"cloudflared: {line.rstrip()}", file=sys.stderr)
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
    server = uvicorn.Server(uvicorn.Config(app_for(muse, key), host="127.0.0.1", port=args.mcp_port, log_level="warning"))
    proc, public = tunnel(args.mcp_port) if args.tunnel else (None, None)
    base = public or f"http://127.0.0.1:{args.mcp_port}"
    print(f"causeway companion: {app.url}")
    print(f"MCP URL for Muse:   {base}{MCP_PATH}")
    print(f"key:                {key}" + (f"   (generated; set {KEY_NAME} in ~/.config/causeway/.env to keep one)" if generated else ""))
    print(f"no header support?  {base}/k/<key>{MCP_PATH}")
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
