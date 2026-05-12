"""
AGTP wire format (v1.0).

Framing:

    AGTP/1.0 METHOD [PATH[?QUERY]]\r\n
    Header-Name: value\r\n
    \r\n
    {body bytes}

The optional ``PATH`` token on the request line carries the URI path
the request targets. Servers that bind methods to specific paths
(via ``server.endpoint_registry.EndpointRegistry``) use it to route
the request; servers that don't, or callers that don't supply one,
default to ``/``. The two-token shape (``AGTP/1.0 METHOD``) remains
valid for callers that don't carry a path.

The path token **MAY** carry a query string after a ``?``. Query
parameters parse into a ``query`` dict on the parsed request; the
dispatcher merges them into the request input alongside body
parameters before schema validation (body wins on key conflicts).

URI fragments (the ``#fragment`` part of a URI) are client-side-only
in URI conventions; AGTP rejects any request whose path token
contains a ``#`` with :class:`WireFormatError`. AGTP is its own
protocol, not HTTP — there is no "browser bar" use case to
accommodate.

This module supports raw byte bodies (so JSON, HTML, YAML, or any other
content type rides over the same framing). Content-Length frames the
body when present.
"""

from __future__ import annotations

import urllib.parse
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
    #: URI path the request targets. Defaults to ``/`` for back-compat
    #: with two-token clients that don't carry one. Servers consult
    #: this when matching against
    #: :class:`server.endpoint_registry.EndpointRegistry`. Always the
    #: bare path (no ``?query`` suffix); query parameters live in
    #: :attr:`query`.
    path: str = "/"
    #: Query parameters parsed from the request line's ``?`` suffix.
    #: Each value is a string (the wire format does not distinguish
    #: typed values from string-encoded ones; the input schema's
    #: ``type`` declaration drives any coercion downstream). Repeated
    #: keys collapse to the last value — the runtime contract is that
    #: query strings carry simple ``key=value`` pairs, not multi-valued
    #: forms; richer shapes ride in the body.
    query: Dict[str, str] = field(default_factory=dict)

    def serialize(self) -> bytes:
        headers = dict(self.headers)
        if self.body_bytes:
            headers.setdefault("Content-Length", str(len(self.body_bytes)))
        # Re-encode the path + query as a single token. We use
        # ``quote_plus`` only for the values so paths and structural
        # ``=`` / ``&`` separators stay readable in transcripts.
        path_token = self.path or "/"
        if self.query:
            qstr = "&".join(
                f"{urllib.parse.quote(str(k), safe='')}="
                f"{urllib.parse.quote(str(v), safe='')}"
                for k, v in self.query.items()
            )
            path_token = f"{path_token}?{qstr}"
        # Two-token form (no path, no query) is wire-equivalent to a
        # path of ``/`` and is what every pre-Phase-2 client sent.
        # Emit the path token only when it differs from the default
        # so existing transcripts and tests stay byte-identical.
        if path_token != "/":
            start = f"{AGTP_VERSION} {self.method} {path_token}"
        else:
            start = f"{AGTP_VERSION} {self.method}"
        lines = [start]
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
    # Two-token form (back-compat): ``AGTP/1.0 METHOD``. Three-token
    # form (Phase-2+): ``AGTP/1.0 METHOD PATH[?QUERY]``. Anything
    # else is malformed.
    if len(parts) == 2:
        version, method = parts
        path_token = "/"
    elif len(parts) == 3:
        version, method, path_token = parts
    else:
        raise WireFormatError(f"malformed request start line: {start_line!r}")
    if version != AGTP_VERSION:
        raise WireFormatError(f"unsupported AGTP version: {version!r}")

    # Reject fragments at the wire layer. URI fragments are
    # client-side-only by URI convention; AGTP traffic carries no
    # browser-bar use case so a ``#`` in the request line is always
    # malformed.
    if "#" in path_token:
        raise WireFormatError(
            f"request path contains a fragment ('#'); fragments are "
            f"client-side-only and not permitted on AGTP requests: "
            f"{path_token!r}"
        )

    # Split the path token at the first ``?`` so the dispatcher
    # matches against the bare path and merges the query into the
    # request input.
    if "?" in path_token:
        path, _, query_str = path_token.partition("?")
    else:
        path, query_str = path_token, ""
    if not path:
        path = "/"
    query = _parse_query_string(query_str)

    body = _read_body(reader, headers)
    return AGTPRequest(
        method=method, headers=headers, body_bytes=body,
        path=path, query=query,
    )


def _parse_query_string(query_str: str) -> Dict[str, str]:
    """Parse ``a=1&b=2`` into a string-valued dict. Repeated keys
    collapse to the last value (per the documented runtime contract).
    Empty input returns an empty dict."""
    if not query_str:
        return {}
    out: Dict[str, str] = {}
    for pair in query_str.split("&"):
        if not pair:
            continue
        k, sep, v = pair.partition("=")
        key = urllib.parse.unquote_plus(k)
        if not key:
            continue
        out[key] = urllib.parse.unquote_plus(v) if sep else ""
    return out


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


def read_agent_id(message, default: str = "") -> str:
    """Read the invoking agent's identity from an AGTPRequest.

    Per ``agtp §10``, the canonical header name is ``Agent-ID``. The
    pre-§10 implementation used ``Target-Agent`` (which was misnamed
    — it identifies the source agent, not the target). This helper
    reads ``Agent-ID`` first; falls back to ``Target-Agent`` with a
    deprecation warning when the legacy name is the only one present.

    Returns the agent id string, or ``default`` when neither header
    is set (server-level operations such as target-less DISCOVER).
    """
    new_value = header(message, "Agent-ID")
    if new_value:
        return new_value
    legacy_value = header(message, "Target-Agent")
    if legacy_value:
        import sys as _sys
        import warnings as _warnings
        _warnings.warn(
            "Target-Agent header is deprecated; use Agent-ID instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        print(
            "[server] Warning: Target-Agent header is deprecated; "
            "use Agent-ID instead.",
            file=_sys.stderr,
        )
        return legacy_value
    return default
