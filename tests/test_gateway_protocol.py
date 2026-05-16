"""
Frame codec tests for the gateway protocol library.

Exercises :mod:`core.gateway.protocol` against the contract documented
in ``docs/architecture/gateway-protocol-v1.md`` §3 (framing).
"""

from __future__ import annotations

import io
import json
import struct

import pytest

from core.gateway import (
    MAX_FRAME_SIZE,
    FrameDecodeError,
    FrameTooLargeError,
    GatewayError,
    read_frame,
    write_frame,
)


# ---------------------------------------------------------------------------
# Round-trip.
# ---------------------------------------------------------------------------


def test_round_trip_simple_payload() -> None:
    payload = {"type": "hello", "gateway_versions": ["1.0"]}
    buf = io.BytesIO()
    write_frame(buf, payload)
    buf.seek(0)
    assert read_frame(buf) == payload


def test_round_trip_nested_payload() -> None:
    payload = {
        "type": "request",
        "request_id": "req-1",
        "envelope": {
            "method": "BOOK",
            "path": "/room",
            "input": {"guest": "Chris", "nights": 2},
            "agent_id": "abc123",
        },
    }
    buf = io.BytesIO()
    write_frame(buf, payload)
    buf.seek(0)
    assert read_frame(buf) == payload


def test_round_trip_multiple_frames_in_sequence() -> None:
    """Reader picks up each frame correctly when multiple are concatenated."""
    buf = io.BytesIO()
    payloads = [
        {"type": "hello", "gateway_versions": ["1.0"]},
        {"type": "request", "request_id": "r1", "envelope": {}},
        {"type": "request", "request_id": "r2", "envelope": {}},
        {"type": "goodbye", "reason": "shutdown"},
    ]
    for p in payloads:
        write_frame(buf, p)
    buf.seek(0)
    for expected in payloads:
        assert read_frame(buf) == expected


# ---------------------------------------------------------------------------
# Length prefix.
# ---------------------------------------------------------------------------


def test_length_prefix_is_4_byte_big_endian() -> None:
    """The on-wire length prefix MUST be 4-byte big-endian unsigned int."""
    buf = io.BytesIO()
    write_frame(buf, {"type": "ping", "nonce": "p1"})
    framed = buf.getvalue()
    expected_len = struct.unpack(">I", framed[:4])[0]
    assert expected_len == len(framed) - 4
    # Confirm the body is exactly that many bytes and parses as the payload.
    body = framed[4:]
    assert len(body) == expected_len
    assert json.loads(body.decode("utf-8")) == {"type": "ping", "nonce": "p1"}


def test_oversize_frame_refused_on_write() -> None:
    """write_frame raises before sending an oversize payload."""
    # 16 MiB + 1 byte after JSON encoding.
    big_string = "x" * (MAX_FRAME_SIZE + 1)
    payload = {"type": "request", "data": big_string}
    buf = io.BytesIO()
    with pytest.raises(FrameTooLargeError):
        write_frame(buf, payload)
    # Nothing should have been written.
    assert buf.getvalue() == b""


def test_oversize_frame_refused_on_read() -> None:
    """read_frame refuses before parsing JSON when the length prefix is too large."""
    buf = io.BytesIO(struct.pack(">I", MAX_FRAME_SIZE + 1) + b"{}")
    with pytest.raises(FrameTooLargeError):
        read_frame(buf)


# ---------------------------------------------------------------------------
# Error cases.
# ---------------------------------------------------------------------------


def test_missing_type_field_rejected_on_write() -> None:
    with pytest.raises(GatewayError, match="'type' field"):
        write_frame(io.BytesIO(), {"agent_id": "abc"})


def test_missing_type_field_rejected_on_read() -> None:
    """A frame whose JSON body has no 'type' is malformed."""
    body = json.dumps({"agent_id": "abc"}).encode("utf-8")
    buf = io.BytesIO(struct.pack(">I", len(body)) + body)
    with pytest.raises(FrameDecodeError, match="missing required 'type'"):
        read_frame(buf)


def test_truncated_header_rejected() -> None:
    buf = io.BytesIO(b"\x00\x00")  # 2 bytes, less than the 4-byte header
    with pytest.raises(FrameDecodeError, match="mid-frame"):
        read_frame(buf)


def test_truncated_body_rejected() -> None:
    """Length header says 100 bytes; reader gets EOF after 5."""
    buf = io.BytesIO(struct.pack(">I", 100) + b"{\"ty")
    with pytest.raises(FrameDecodeError, match="mid-frame"):
        read_frame(buf)


def test_empty_frame_rejected() -> None:
    """A zero-length frame is not a legal idle marker; it's malformed."""
    buf = io.BytesIO(struct.pack(">I", 0))
    with pytest.raises(FrameDecodeError, match="empty frame"):
        read_frame(buf)


def test_non_json_body_rejected() -> None:
    body = b"this is not json"
    buf = io.BytesIO(struct.pack(">I", len(body)) + body)
    with pytest.raises(FrameDecodeError, match="not valid JSON"):
        read_frame(buf)


def test_non_object_body_rejected() -> None:
    """JSON array at top level is malformed for the gateway protocol."""
    body = b'["not", "an", "object"]'
    buf = io.BytesIO(struct.pack(">I", len(body)) + body)
    with pytest.raises(FrameDecodeError, match="must be a JSON object"):
        read_frame(buf)


def test_invalid_utf8_body_rejected() -> None:
    body = b"\xff\xfe"  # not valid UTF-8
    buf = io.BytesIO(struct.pack(">I", len(body)) + body)
    with pytest.raises(FrameDecodeError, match="UTF-8"):
        read_frame(buf)
