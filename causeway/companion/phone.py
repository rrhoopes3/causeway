"""The owner's phone page (SPEC §12.3): Approve, Deny and Why for the open gate, on the MCP port, under a second secret.

Muse holds the MCP key, so that key must never open this page, or an agent could approve its own action. The page lives
under /o/<owner key>/, a link that only the console shows. Holding it means holding the approval, and it outranks the
MCP key, so the page may hand that key over for setup. It polls, because Cloudflare quick tunnels do not carry SSE.
Everything it shows came through the agent, so the script renders with textContent only and the policy on every
response allows no inline script, no outside request and no framing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sys
import threading
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

import anyio

from causeway.companion.checkpoint import Checkpoint
from causeway.companion.server import CONTENT_TYPES, OCTET_STREAM, STATIC_DIR, Companion, resolve_static
from causeway.companion.session import VERDICTS

OWNER_KEY_NAME = "CAUSEWAY_OWNER_KEY"
OWNER_KEY_SHAPE = re.compile(r"[A-Za-z0-9_-]{16,}")
OWNER_PREFIX = "/o/"
STATIC_PREFIX = "static/"
PAGE = "phone.html"
# Everything the page loads, and nothing else: not the companion page, its scene code or any other file under static/.
PAGE_FILES = frozenset({
    "phone.css",
    "phone.js",
    "phone3d.js",
    "state.js",
    "text.js",
    "companion.js",
    "companion.glb",
    "weebles_idle.jpg",
    "weebles_open.jpg",
    "vendor/three/three.module.min.js",
    "vendor/three/three.core.min.js",
    "vendor/three/addons/loaders/GLTFLoader.js",
    "vendor/three/addons/utils/BufferGeometryUtils.js",
})
MAX_POST = 8_000
MAX_SHOWN = 2_000
MAX_FINDINGS = 50  # thousands would make each poll too big to finish on a slow link, and the page would never draw
MAX_ERROR = 300
PAGE_THREADS = 8
IMPORT_MAP = re.compile(rb'<script type="importmap">(.*?)</script>', re.DOTALL)
BLOCKS = "█▀▄"
QR_BORDER = 2

SETUP_PROMPT = (
    "Add a custom connector for me. It is an MCP server called Causeway at {url}. Use streamable HTTP and a static "
    "bearer token, not OAuth. Ask me for the token through your secure credential prompt, not in this chat. Keep the "
    "connector for future chats. When it is connected, list its tools."
)
STANDING_RULE = (
    "Save this as a standing rule for future chats: before any action that sends, buys, books, pays, posts, deletes or "
    "submits for me, call the Causeway checkpoint tool with my request word for word, the raw text of everything you "
    "read, and the exact action and arguments, then follow its verdict."
)
NO_MODEL = "No model is configured on this server, so Why cannot run. Set MODEL_API_KEY and restart. Approve and Deny still work."
BILLING = "The Meta Model API returned a billing error (HTTP 402), so Why cannot run until billing is set up. Approve and Deny still work."

Answer = tuple[int, str, bytes]


def check_owner_key(owner_key: str, mcp_key: str) -> None:
    """Raise ValueError for a key that would not guard the page. The messages never hold either key."""
    if hmac.compare_digest(owner_key.encode(), mcp_key.encode()):
        raise ValueError(f"{OWNER_KEY_NAME} must differ from the MCP key: an agent holding that key could approve its own actions")
    if not OWNER_KEY_SHAPE.fullmatch(owner_key):
        raise ValueError(f"{OWNER_KEY_NAME} must be at least 16 letters, digits, - or _")


def security_headers(page: bytes) -> list[tuple[bytes, bytes]]:
    """The headers on every owner response. The import map is the page's one inline script, allowed by its hash."""
    digest = base64.b64encode(hashlib.sha256(IMPORT_MAP.search(page).group(1)).digest()).decode()  # type: ignore[union-attr]
    policy = (
        f"default-src 'none'; script-src 'self' 'sha256-{digest}'; style-src 'self'; img-src 'self' blob: data:; "
        "connect-src 'self' blob: data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return [
        (b"content-security-policy", policy.encode()),
        (b"referrer-policy", b"no-referrer"),
        (b"cache-control", b"no-store"),  # quick tunnels would otherwise cache by file extension
        (b"x-robots-tag", b"noindex"),
        (b"x-frame-options", b"DENY"),
        (b"x-content-type-options", b"nosniff"),
    ]


def _json(status: int, payload: Any) -> Answer:
    # ASCII, so a lone surrogate the agent sent stays an escape instead of failing to encode on every poll.
    return status, "application/json", json.dumps(payload).encode("ascii")


def _clip(value: Any) -> Any:
    """Text for the page. Whitespace collapses first, so padding cannot push the rest of a value past the cut, and the
    cut says how much went, so a recipient past it is at least known to be missing."""
    if not isinstance(value, str):
        return value
    value = re.sub(r"\s+", " ", value).strip()
    return f"{value[:MAX_SHOWN]}… (+{len(value) - MAX_SHOWN} more characters)" if len(value) > MAX_SHOWN else value


def _header(scope: dict[str, Any], name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in scope["headers"] if key == name]


def _same_origin(scope: dict[str, Any], public: str | None) -> bool:
    """Exactly one Origin, naming the host this request was sent to or the tunnel's public address, so a tap still works
    if the tunnel rewrites Host. A tunnel ends TLS, so either scheme can match the host."""
    origins, hosts = _header(scope, b"origin"), _header(scope, b"host")
    if len(origins) != 1 or len(hosts) != 1:
        return False
    origin = origins[0].lower()
    return origin in (f"http://{hosts[0].lower()}", f"https://{hosts[0].lower()}") or origin == public


def _is_json(scope: dict[str, Any]) -> bool:
    types = _header(scope, b"content-type")
    return len(types) == 1 and types[0].split(";")[0].strip().lower() == "application/json"


async def _read_body(receive: Any) -> bytes | None:
    """The request's body, or None when it passes MAX_POST or the client leaves first."""
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        body.extend(message.get("body", b""))
        if len(body) > MAX_POST:
            return None
        if not message.get("more_body"):
            return bytes(body)


def _latest_why(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest answer to a why question, or the newest failure of one."""
    for event in reversed(events):
        if event["type"] == "why" or (event["type"] == "error" and event.get("operation") == "why"):
            return event
    return None


def _plain_error(message: str) -> str:
    """What a failed why should say on the page: the two expected failures in plain words, anything else as it came."""
    if "why needs a model" in message:
        return NO_MODEL
    if "HTTP 402" in message or "billing" in message.lower():
        return BILLING
    return f"The model call failed: {message}"[:MAX_ERROR]


def _why_view(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest = _latest_why(events)
    if latest is None:
        return None
    view = {"seq": latest["seq"], "question": latest["question"]}
    if latest["type"] == "error":
        return {**view, "error": _plain_error(latest["message"])}
    return {**view, "result": {key: value for key, value in latest["result"].items() if key != "raw"}}


class OwnerPage:
    """ASGI wrapper: a path under /o/<owner key>/ is the owner's, and everything else goes to `app`, the key-gated MCP
    app. A wrong key falls through, so it gets the same answer as any other path. `mcp_url` is the public address Muse
    connects to, or None without a tunnel."""

    def __init__(self, app: Any, companion: Companion, owner_key: str, mcp_key: str, mcp_url: str | None = None) -> None:
        check_owner_key(owner_key, mcp_key)
        self.app = app
        self.companion = companion
        self.owner_key = owner_key.encode()
        self.mcp_key = mcp_key
        self.mcp_url = mcp_url
        self.page = (STATIC_DIR / PAGE).read_bytes()
        self.headers = security_headers(self.page)
        self._lock = threading.Lock()
        self._asked: tuple[str, int] | None = None
        self._threads: anyio.CapacityLimiter | None = None

    def _public_origin(self) -> str | None:
        parts = urlsplit(self.mcp_url or "")
        return f"{parts.scheme}://{parts.netloc}".lower() if parts.netloc else None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        rest = self._owner_path(scope)
        if rest is None:
            await self.app(scope, receive, send)
            return
        # Read first: answering a POST with its body unread can reach the client as a reset instead of the status.
        body = await _read_body(receive) if scope["method"] == "POST" else b""
        # Threads of its own: a flood of checkpoint calls, each holding a shared thread, must not delay a Deny.
        self._threads = self._threads or anyio.CapacityLimiter(PAGE_THREADS)
        try:
            status, content_type, data = await anyio.to_thread.run_sync(self._route, scope, rest, body, limiter=self._threads)
        except Exception as err:  # the type only: a message could hold the path, and the path holds the key
            print(f"phone page: {type(err).__name__}", file=sys.stderr)
            status, content_type, data = _json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "server error"})
        await send({
            "type": "http.response.start",
            "status": int(status),
            "headers": [
                (b"content-type", content_type.encode()),
                (b"content-length", str(len(data)).encode()),
                *self.headers,
            ],
        })
        await send({"type": "http.response.body", "body": data})

    def _owner_path(self, scope: dict[str, Any]) -> str | None:
        """What follows /o/<owner key>/ in this request's path, or None when the request is not the owner's."""
        if scope["type"] != "http" or not scope["path"].startswith(OWNER_PREFIX):
            return None
        given, slash, rest = scope["path"][len(OWNER_PREFIX):].partition("/")
        if slash and hmac.compare_digest(given.encode("utf-8", "replace"), self.owner_key):
            return rest
        return None

    def _route(self, scope: dict[str, Any], rest: str, body: bytes | None) -> Answer:
        """Runs in a worker thread, because the companion's calls take locks that a new checkpoint can hold."""
        method = scope["method"]
        if method == "POST" and rest in ("decide", "why"):
            return self._post(scope, rest, body)
        if method == "GET" and rest == "":
            return HTTPStatus.OK, CONTENT_TYPES[".html"], self.page
        if method == "GET" and rest == "state":
            return _json(HTTPStatus.OK, self.view())
        if method == "GET" and rest == "setup":
            return _json(HTTPStatus.OK, self.setup())
        if method == "GET" and rest.startswith(STATIC_PREFIX) and rest[len(STATIC_PREFIX):] in PAGE_FILES:
            target = resolve_static(STATIC_DIR, rest[len(STATIC_PREFIX):])
            if target is not None:
                return HTTPStatus.OK, CONTENT_TYPES.get(target.suffix.lower(), OCTET_STREAM), target.read_bytes()
        return _json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _post(self, scope: dict[str, Any], route: str, body: bytes | None) -> Answer:
        if not _same_origin(scope, self._public_origin()):
            return _json(HTTPStatus.FORBIDDEN, {"error": "same-origin request required"})
        if not _is_json(scope):
            return _json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "application/json required"})
        if body is None:
            return _json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            data = None
        gate_id = data.get("gate_id") if isinstance(data, dict) else None
        if not isinstance(gate_id, str) or not gate_id:
            return _json(HTTPStatus.BAD_REQUEST, {"error": "expected a JSON object with a gate_id"})
        if route == "why":
            question = data.get("question")
            if not isinstance(question, str) or not question.strip():
                return _json(HTTPStatus.BAD_REQUEST, {"error": "expected a question"})
            return self._ask(gate_id, question)
        if data.get("verdict") not in VERDICTS:  # never a default: only the owner's own word decides a gate
            return _json(HTTPStatus.BAD_REQUEST, {"error": "verdict must be approve or deny"})
        return _json(*self.companion.decide(gate_id, data["verdict"]))

    def _ask(self, gate_id: str, question: str) -> Answer:
        """Ask the why question for the open gate. A second tap never pays for a second call: it is ignored while one
        is out or after an answer, and only a failed call can be asked again."""
        session = self.companion.current()
        outcome = session.outcome() if isinstance(session, Checkpoint) else {}
        if outcome.get("verdict") != "waiting" or outcome.get("gate_id") != gate_id:
            return _json(HTTPStatus.NOT_FOUND, {"error": f"no open gate {gate_id}"})
        latest = _latest_why(self.companion.hub.events())
        asked = (gate_id, latest["seq"] if latest else -1)
        with self._lock:
            if self._asked == asked or (latest is not None and latest["type"] == "why"):
                return _json(HTTPStatus.ACCEPTED, {"ok": True})
            self._asked = asked
        return _json(*self.companion.why(question))

    def view(self) -> dict[str, Any]:
        """What the page polls. Findings are the open gate's taints, matched by gate id, so a poll that straddles a
        new checkpoint never shows the old session's findings under the new one's action."""
        session = self.companion.current()
        if not isinstance(session, Checkpoint):
            return {"verdict": "standby"}
        outcome = session.outcome()
        events = self.companion.hub.events()
        taints = next((e["taints"] for e in events if e["type"] == "gate" and e["gate_id"] == outcome["gate_id"]), [])
        taints = sorted(taints, key=lambda t: "~" in t["path"])  # whole arguments first, so the cap cuts atoms of prose
        return {
            "verdict": "error" if session.state == "error" else outcome["verdict"],
            "gate_id": outcome["gate_id"],
            "request": _clip(session.task),
            "action": _clip(session.action),
            "findings": [
                {"path": _clip(t["path"]), "value": _clip(t.get("value")), "label": _clip(t["label"]), "tag": t["tag"]}
                for t in taints[:MAX_FINDINGS]
            ],
            "more": max(0, len(taints) - MAX_FINDINGS),
            "why": _why_view(events),
        }

    def setup(self) -> dict[str, Any]:
        """The Set up Muse section: how the URL and key reach the phone without entering a chat."""
        return {
            "url": self.mcp_url,
            "key": self.mcp_key,
            "prompt": SETUP_PROMPT.format(url=self.mcp_url) if self.mcp_url else None,
            "rule": STANDING_RULE,
        }


def qr_lines(url: str, encoding: str | None) -> list[str]:
    """`url` as a QR code for a dark console, with light modules drawn solid: half blocks when the console can show
    them, else `##`. Empty when it cannot be drawn; the link is printed either way."""
    try:
        import qrcode  # in the muse extra

        code = qrcode.QRCode(border=QR_BORDER)
        code.add_data(url)
        code.make(fit=True)
        rows = code.get_matrix()
    except Exception as err:
        print(f"phone page: no QR code ({type(err).__name__})", file=sys.stderr)
        return []
    try:
        BLOCKS.encode(encoding or "ascii")
    except (UnicodeError, LookupError):
        return ["".join("  " if dark else "##" for dark in row) for row in rows]
    pairs = zip(rows[::2], rows[1::2] + [[True] * len(rows[0])])
    half = {(False, False): "█", (False, True): "▀", (True, False): "▄", (True, True): " "}
    return ["".join(half[cell] for cell in zip(top, bottom)) for top, bottom in pairs]


def phone_lines(public: str | None, port: int, owner_key: str, generated: bool, encoding: str | None) -> list[str]:
    """What the console says about the phone page: the link, a QR code when a phone can reach it, and the warning."""
    note = f"   (generated; set {OWNER_KEY_NAME} in ~/.config/causeway/.env to keep one)" if generated else ""
    warning = "Keep this window off camera: the phone link can approve actions."
    if public is None:
        return [
            f"phone page:         http://127.0.0.1:{port}{OWNER_PREFIX}{owner_key}/{note}",
            "                    This PC only. A phone needs --tunnel.",
            warning,
        ]
    url = f"{public}{OWNER_PREFIX}{owner_key}/"
    return [f"phone page:         {url}{note}", *qr_lines(url, encoding), warning]
