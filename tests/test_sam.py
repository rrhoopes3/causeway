import base64
import json

import httpx
import pytest

from causeway import sam
from causeway.client import APIError

KEY = "test-key-sam-123"
DOC_LINE = "<0f>0<|box;x1=181;y1=365;x2=556;y2=486;w=1516;h=1600|><|mask;x=0;y=0;data=122,376,~AbC|>"


def _body(text):
    return {
        "id": "resp_1",
        "object": "response",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
    }


def _capture(captured, status=200, payload=None):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, json=payload if payload is not None else _body(DOC_LINE))

    return httpx.MockTransport(handler)


# parse ----------------------------------------------------------------------


def test_parse_doc_example_line():
    assert sam.parse(DOC_LINE) == [
        {"frame": 0, "id": 0, "box": [181, 365, 556, 486], "size": [1516, 1600], "confidence": None}
    ]


def test_parse_multiple_records_and_frames():
    text = (
        "<2f>0<|box;x1=1;y1=2;x2=3;y2=4;w=10;h=20|><|mask;x=0;y=0;data=5,6,7|>,"
        "1<|box;x1=5;y1=6;x2=7;y2=8;w=10;h=20;c=0.5|><|mask;x=0;y=0;data=9,10,11|>\n"
        "<3f>4<|box;x1=9;y1=9;x2=12;y2=14;w=20;h=20|><|mask;x=0;y=0;data=1|>"
    )
    records = sam.parse(text)
    assert [(r["frame"], r["id"]) for r in records] == [(2, 0), (2, 1), (3, 4)]
    assert records[0]["box"] == [1, 2, 3, 4]
    assert records[1]["box"] == [5, 6, 7, 8]
    assert records[2]["size"] == [20, 20]


def test_parse_reads_confidence():
    line = "<0f>0<|box;x1=1;y1=2;x2=3;y2=4;w=10;h=10;c=0.93|><|mask;x=0;y=0;data=1,2|>"
    assert sam.parse(line)[0]["confidence"] == pytest.approx(0.93)


def test_parse_ignores_unknown_fields():
    line = "<0f>7<|box;x1=1;y1=2;x2=3;y2=4;w=10;h=10;score=high;flag;zz=ok|><|mask;x=0;y=0;data=1|>"
    record = sam.parse(line)[0]
    assert record["id"] == 7
    assert record["box"] == [1, 2, 3, 4]
    assert record["confidence"] is None


def test_parse_mask_data_with_commas_does_not_create_records():
    line = "<0f>0<|box;x1=1;y1=2;x2=3;y2=4;w=10;h=10|><|mask;x=0;y=0;data=12,34,56,78,90|>"
    records = sam.parse(line)
    assert len(records) == 1
    assert records[0]["id"] == 0


def test_parse_empty_text():
    assert sam.parse("") == []
    assert sam.parse("   \n") == []
    assert sam.parse(None) == []


def test_parse_skips_box_without_id_and_keeps_missing_corners_as_none():
    assert sam.parse("<0f><|box;x1=1;y1=2;x2=3;y2=4;w=10;h=10|>") == []
    record = sam.parse("<0f>3<|box;x1=1;y1=2|>")[0]
    assert record["id"] == 3
    assert record["box"] is None


# segment --------------------------------------------------------------------


def test_segment_request_shape_auth_and_result(tmp_path):
    image = tmp_path / "desk.jpg"
    raw = b"\xff\xd8\xff fake jpeg bytes"
    image.write_bytes(raw)
    captured = []

    records = sam.segment(str(image), "mug", api_key=KEY, transport=_capture(captured))

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.meta.ai/v1/responses"
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(req.content)
    assert body["model"] == "sam-3.1"
    assert body["metadata"] == {"include_confidence": "true"}
    assert "stream" not in body
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "mug"},
                {
                    "type": "input_image",
                    "image_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
                },
            ],
        }
    ]
    assert records == sam.parse(DOC_LINE)


def test_segment_local_png_uses_png_data_url(tmp_path):
    image = tmp_path / "shot.PNG"
    image.write_bytes(b"\x89PNG fake")
    captured = []
    sam.segment(str(image), "cup", api_key=KEY, transport=_capture(captured))
    url = json.loads(captured[0].content)["input"][0]["content"][1]["image_url"]
    assert url == "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode("ascii")


def test_segment_passes_remote_url_through():
    captured = []
    url = "https://example.com/desk.png"
    sam.segment(url, "mug", api_key=KEY, transport=_capture(captured))
    assert json.loads(captured[0].content)["input"][0]["content"][1]["image_url"] == url


def test_segment_falls_back_to_top_level_output_text():
    captured = []
    payload = {"output_text": DOC_LINE}
    records = sam.segment("https://example.com/a.jpg", "mug", api_key=KEY, transport=_capture(captured, payload=payload))
    assert records == sam.parse(DOC_LINE)


def test_segment_only_reads_output_text_parts():
    captured = []
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "refusal", "text": "<0f>9<|box;x1=1;y1=1;x2=2;y2=2;w=3;h=3|>"},
                    {"type": "output_text", "text": "<0f>0<|box;x1=1;y1=2;x2=3;y2=4;w=10;h=10|>"},
                ],
            }
        ]
    }
    records = sam.segment("https://example.com/a.jpg", "mug", api_key=KEY, transport=_capture(captured, payload=payload))
    assert [r["id"] for r in records] == [0]


def test_segment_non_2xx_raises_apierror_without_key():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(401, text=f'{{"error": "invalid key {KEY}"}}')

    with pytest.raises(APIError) as info:
        sam.segment("https://example.com/a.jpg", "mug", api_key=KEY, transport=httpx.MockTransport(handler))
    assert info.value.status == 401
    assert KEY not in str(info.value)
    assert KEY not in info.value.body
    assert "[redacted]" in info.value.body
