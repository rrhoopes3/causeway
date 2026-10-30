"""SAM 3.1 segmentation over the Meta Model API: request, and parsing of its tokenised output (SPEC 3, demo tool)."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

import httpx

from causeway.client import DEFAULT_BASE_URL, APIError, check_key_transport, is_meta_https, load_api_key

SAM_MODEL = "sam-3.1"
TIMEOUT = 120

_FRAME = re.compile(r"<(\d+)f>")
_TOKEN = re.compile(r"<\|([A-Za-z_]+);(.*?)\|>", re.DOTALL)
_TRAILING_ID = re.compile(r"(\d+)\s*$")
_IMAGE_TYPES = {"jpg": "jpeg"}


def _fields(body: str) -> dict[str, str]:
    """Split a token body such as 'x1=1;y1=2;c=0.9' into a dict. Pieces without '=' are ignored."""
    out: dict[str, str] = {}
    for part in body.split(";"):
        key, sep, value = part.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _number(text: str | None) -> int | float | None:
    """Return text as an int when it is one, else a float, else None."""
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return None


def _coords(fields: dict[str, str], keys: tuple[str, ...]) -> list[int | float] | None:
    """Return the numbers under keys in order, or None if any is missing or not numeric."""
    values = [_number(fields.get(k)) for k in keys]
    if any(v is None for v in values):
        return None
    return values  # type: ignore[return-value]


def parse(text: str | None) -> list[dict[str, Any]]:
    """Parse SAM 3.1 output text into one record per (frame, object id).

    Frame markers look like `<0f>`. Within a frame, each object is an integer id followed by a
    `<|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..[;c=..]|>` token and a `<|mask;...|>` token. Mask data
    can contain commas, so the scan works on tokens, not on commas. Masks are ignored. Unknown
    `k=v` fields are skipped. A box token with no integer id before it is skipped. Missing or
    non-numeric corners or size yield None for that field.
    """
    if not text:
        return []
    frames = list(_FRAME.finditer(text))
    records: list[dict[str, Any]] = []
    for index, marker in enumerate(frames):
        end = frames[index + 1].start() if index + 1 < len(frames) else len(text)
        body = text[marker.end() : end]
        frame = int(marker.group(1))
        pos = 0
        for token in _TOKEN.finditer(body):
            gap = body[pos : token.start()]
            pos = token.end()
            if token.group(1) != "box":
                continue
            id_match = _TRAILING_ID.search(gap)
            if id_match is None:
                continue
            fields = _fields(token.group(2))
            size = _coords(fields, ("w", "h"))
            records.append(
                {
                    "frame": frame,
                    "id": int(id_match.group(1)),
                    "box": _coords(fields, ("x1", "y1", "x2", "y2")),
                    "size": size,
                    "confidence": _float_or_none(fields.get("c", fields.get("confidence"))),
                }
            )
    return records


def _float_or_none(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _image_url(image_path_or_url: str) -> str:
    """Pass http(s) URLs through; turn a local file into a base64 data URL."""
    if image_path_or_url.startswith(("http://", "https://")):
        return image_path_or_url
    path = Path(image_path_or_url)
    ext = path.suffix.lower().lstrip(".")
    if not ext:
        raise ValueError(f"image file has no extension: {path.name}")
    mime = _IMAGE_TYPES.get(ext, ext)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/{mime};base64,{encoded}"


def _output_text(data: dict[str, Any]) -> str:
    """Concatenate output_text parts of a responses payload, falling back to a top-level output_text.

    Parts are joined with newlines so that frame lines from different parts never merge.
    """
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    if parts:
        return "\n".join(parts)
    top = data.get("output_text")
    return top if isinstance(top, str) else ""


def segment(
    image_path_or_url: str,
    prompt: str,
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Ask SAM 3.1 to segment `prompt` in an image and return the parsed records (see parse).

    `api_key` defaults to load_api_key() for https://api.meta.ai and to none elsewhere. A key is never sent over
    plain http except to a loopback server (ValueError otherwise). Non-2xx responses raise APIError with the key
    redacted.
    """
    key = api_key if api_key is not None else (load_api_key() if is_meta_https(base_url) else None)
    check_key_transport(base_url, key)
    body = {
        "model": SAM_MODEL,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": _image_url(image_path_or_url)},
                ],
            }
        ],
        "metadata": {"include_confidence": "true"},
    }
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    with httpx.Client(timeout=TIMEOUT, transport=transport) as http:
        resp = http.post(f"{base_url.rstrip('/')}/responses", json=body, headers=headers)
    if not 200 <= resp.status_code < 300:
        text = resp.text
        if key:
            text = text.replace(key, "[redacted]")
        raise APIError(resp.status_code, text)
    return parse(_output_text(resp.json()))
