"""
Gateway protocol v1 — frame codec and shared types.

Each frame is a 4-byte big-endian unsigned length prefix followed by
a UTF-8 JSON object. Maximum payload is 16 MiB; oversize frames are
refused with :class:`FrameTooLargeError` before the JSON parser runs.

The codec is sync-only. Asynchronous variants can ride later as the
runtime modules need them; PHP-FPM and the Python reference module
are both happy with sync sockets.
"""

from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO, Dict


#: Gateway protocol version this implementation speaks. See
#: ``docs/architecture/gateway-protocol-v1.md`` §12.
GATEWAY_VERSION = "1.0"


#: Hard cap on a single frame's JSON payload. The length prefix itself
#: is excluded. Daemons and modules MUST refuse larger frames before
#: parsing JSON.
MAX_FRAME_SIZE = 16 * 1024 * 1024  # 16 MiB


_LENGTH_HEADER = struct.Struct(">I")  # 4-byte big-endian unsigned int
_LENGTH_HEADER_SIZE = _LENGTH_HEADER.size  # 4


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class GatewayError(Exception):
    """Base class for gateway-protocol failures."""


class FrameDecodeError(GatewayError):
    """A frame could not be decoded (truncated, non-JSON, not an object)."""


class FrameTooLargeError(GatewayError):
    """A frame's length header exceeds :data:`MAX_FRAME_SIZE`."""


# ---------------------------------------------------------------------------
# Codec.
# ---------------------------------------------------------------------------


def _read_exact(reader: BinaryIO, n: int) -> bytes:
    """Read exactly ``n`` bytes or raise :class:`FrameDecodeError`.

    Treats EOF as a decode error — half-read frames are never legal.
    """
    if n <= 0:
        return b""
    chunks: list = []
    remaining = n
    while remaining > 0:
        chunk = reader.read(remaining)
        if not chunk:
            raise FrameDecodeError(
                f"connection closed mid-frame (expected {n} bytes, "
                f"got {n - remaining})"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(reader: BinaryIO) -> Dict[str, Any]:
    """Read one frame from ``reader`` and return its parsed payload.

    Raises :class:`FrameTooLargeError` when the announced length
    exceeds :data:`MAX_FRAME_SIZE`. Raises :class:`FrameDecodeError`
    on truncation, non-JSON payloads, or non-object payloads.
    """
    header = _read_exact(reader, _LENGTH_HEADER_SIZE)
    (length,) = _LENGTH_HEADER.unpack(header)
    if length > MAX_FRAME_SIZE:
        raise FrameTooLargeError(
            f"frame length {length} exceeds MAX_FRAME_SIZE ({MAX_FRAME_SIZE})"
        )
    if length == 0:
        raise FrameDecodeError("empty frame (length=0)")
    body = _read_exact(reader, length)
    try:
        payload = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise FrameDecodeError(f"frame body is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FrameDecodeError(f"frame body is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise FrameDecodeError(
            f"frame body must be a JSON object; got {type(payload).__name__}"
        )
    if "type" not in payload:
        raise FrameDecodeError("frame payload missing required 'type' field")
    return payload


def write_frame(writer: BinaryIO, payload: Dict[str, Any]) -> None:
    """Encode ``payload`` and write it to ``writer``.

    Raises :class:`FrameTooLargeError` when the encoded JSON exceeds
    :data:`MAX_FRAME_SIZE`. ``payload`` must be JSON-serializable and
    must already carry a ``type`` field; this function does not
    validate the frame against any schema.
    """
    if "type" not in payload:
        raise GatewayError("frame payload must carry a 'type' field")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_SIZE:
        raise FrameTooLargeError(
            f"encoded frame size {len(body)} exceeds MAX_FRAME_SIZE "
            f"({MAX_FRAME_SIZE})"
        )
    writer.write(_LENGTH_HEADER.pack(len(body)))
    writer.write(body)
    writer.flush()


__all__ = [
    "GATEWAY_VERSION",
    "MAX_FRAME_SIZE",
    "FrameDecodeError",
    "FrameTooLargeError",
    "GatewayError",
    "read_frame",
    "write_frame",
]
