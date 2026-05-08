"""
AGTP wire format (v1.0).

HTTP-style framing:

    AGTP/1.0 METHOD\r\n
    Header-Name: value\r\n
    \r\n
    {body bytes}

This module supports raw byte bodies (so JSON, HTML, YAML, or any other
content type rides over the same framing). Content-Length frames the
body when present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


AGTP_VERSION = "AGTP/1.0"


class WireFormatError(Exception):
    """Raised when an AGTP message cannot be parsed."""


@dataclass
class AGTPRequest:
    method: str
    headers: Dict[str, str] = field(default_factory=dict)
    body_bytes: bytes = b""

    def serialize(self) -> bytes:
        headers = dict(self.headers)
        if self.body_bytes:
            headers.setdefault("Content-Length", str(len(self.body_bytes)))
        lines = [f"{AGTP_VERSION} {self.method}"]
        for k, v in headers.items():
            lines.append(f"{k}: {v}")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
        return head + self.body_bytes


@dataclass
class AGTPResponse:
    status_code: int
    status_text: str
    headers: Dict[str, str] = field(default_factory=dict)
    body_bytes: bytes = b""

    def serialize(self) -> bytes:
        headers = dict(self.headers)
        if self.body_bytes:
            headers.setdefault("Content-Length", str(len(self.body_bytes)))
        lines = [f"{AGTP_VERSION} {self.status_code} {self.status_text}"]
        for k, v in headers.items():
            lines.append(f"{k}: {v}")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
        return head + self.body_bytes


def _read_until_blank_line(reader) -> bytes:
    chunks = []
    while True:
        line = reader.readline()
        if not line:
            raise WireFormatError("connection closed before headers complete")
        if line in (b"\r\n", b"\n"):
            break
        chunks.append(line)
    return b"".join(chunks)


def _parse_headers(raw: bytes) -> tuple[str, Dict[str, str]]:
    text = raw.decode("utf-8", errors="replace")
    lines = [line.rstrip("\r") for line in text.split("\n") if line.strip()]
    if not lines:
        raise WireFormatError("no header lines present")
    start_line = lines[0]
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            raise WireFormatError(f"malformed header line: {line!r}")
        name, _, value = line.partition(":")
        headers[name.strip()] = value.strip()
    return start_line, headers


def _read_body(reader, headers: Dict[str, str]) -> bytes:
    length_str = None
    for k, v in headers.items():
        if k.lower() == "content-length":
            length_str = v
            break
    if not length_str:
        return b""
    try:
        length = int(length_str)
    except ValueError as exc:
        raise WireFormatError(f"invalid Content-Length: {length_str!r}") from exc
    if length == 0:
        return b""
    raw = reader.read(length)
    if len(raw) != length:
        raise WireFormatError(
            f"body truncated: expected {length} bytes, got {len(raw)}"
        )
    return raw


def parse_request(reader) -> AGTPRequest:
    head = _read_until_blank_line(reader)
    start_line, headers = _parse_headers(head)
    parts = start_line.split()
    if len(parts) != 2:
        raise WireFormatError(f"malformed request start line: {start_line!r}")
    version, method = parts
    if version != AGTP_VERSION:
        raise WireFormatError(f"unsupported AGTP version: {version!r}")
    body = _read_body(reader, headers)
    return AGTPRequest(method=method, headers=headers, body_bytes=body)


def parse_response(reader) -> AGTPResponse:
    head = _read_until_blank_line(reader)
    start_line, headers = _parse_headers(head)
    parts = start_line.split(maxsplit=2)
    if len(parts) < 2:
        raise WireFormatError(f"malformed response start line: {start_line!r}")
    version = parts[0]
    if version != AGTP_VERSION:
        raise WireFormatError(f"unsupported AGTP version: {version!r}")
    try:
        status_code = int(parts[1])
    except ValueError as exc:
        raise WireFormatError(f"non-numeric status code: {parts[1]!r}") from exc
    status_text = parts[2] if len(parts) > 2 else ""
    body = _read_body(reader, headers)
    return AGTPResponse(
        status_code=status_code,
        status_text=status_text,
        headers=headers,
        body_bytes=body,
    )


def header(message, name: str, default: str = "") -> str:
    """Case-insensitive header lookup on AGTPRequest or AGTPResponse."""
    lower = name.lower()
    for k, v in message.headers.items():
        if k.lower() == lower:
            return v
    return default
