"""
Agent ID generation, validation, and URI parsing.

Reference: draft-hood-independent-agtp (v07 draft) plus the Interaction
Model design note. The canonical Agent-ID is a 256-bit cryptographic
identifier rendered as 64 lowercase hexadecimal characters. URIs come
in three forms:

    agtp://{agent-id}                  Form 1: canonical (registry)
    agtp://{agent-id}@{host}[:{port}]  Form 1a: ID with explicit host
    agtp://{host}[:{port}]             Form 2:  server-level (no agent)

Form 1 requires registry lookup to discover the serving host. Form 1a
embeds the host directly. Form 2 addresses the server itself, which is
the surface used by DISCOVER (no Target-Agent) to retrieve the Server
Manifest.

The parser distinguishes Form 1/1a from Form 2 by inspecting the first
component after `agtp://`. Sixty-four lowercase hex characters means
agent ID; anything else is treated as a hostname.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Optional


AGENT_ID_BYTES = 32              # 256 bits
AGENT_ID_HEX_LENGTH = 64         # 32 bytes * 2 hex chars
AGENT_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_AGTP_PORT = 4480

# Hostnames per RFC 1123: alphanumeric, dots, hyphens. No underscores.
# Each label can be up to 63 chars; the regex stays liberal and lets
# higher-level checks enforce label length when it matters.
_HOSTNAME_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9\-]{0,61}[A-Za-z0-9])"
    r"(?:\.(?:[A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9\-]{0,61}[A-Za-z0-9]))*$"
)

# Form 1 / 1a: starts with a 64-char hex agent id.
_AGENT_URI_PATTERN = re.compile(
    r"^agtp://"
    r"(?P<agent_id>[0-9a-f]{64})"
    r"(?:@(?P<host>[a-zA-Z0-9.\-]+)(?::(?P<port>\d+))?)?"
    r"(?:\?(?P<query>.*))?$"
)

# Form 2: server-level, no agent ID. Query strings are not currently
# admitted on server URIs; if that changes the design note will say so.
_SERVER_URI_PATTERN = re.compile(
    r"^agtp://"
    r"(?P<host>[A-Za-z0-9.\-]+)"
    r"(?::(?P<port>\d+))?$"
)

# Public alias kept for backward compatibility with code that imported
# URI_PATTERN directly.
URI_PATTERN = _AGENT_URI_PATTERN


class AgentIDError(ValueError):
    """Raised when an Agent ID or URI fails validation."""


@dataclass
class ParsedURI:
    """Result of parsing an `agtp://` URI.

    Form 1:    agent_id set, host=None, port=None
    Form 1a:   agent_id set, host set
    Form 2:    agent_id=None, host set
    """

    agent_id: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    query: Optional[str] = None

    @property
    def has_explicit_host(self) -> bool:
        """True when the URI carries a host component (Forms 1a and 2)."""
        return self.host is not None

    @property
    def is_server_level(self) -> bool:
        """True for Form 2 URIs (no agent ID, host directly addressed)."""
        return self.agent_id is None

    @property
    def effective_port(self) -> int:
        """Port to connect to, defaulting to 4480 when not specified."""
        return self.port if self.port is not None else DEFAULT_AGTP_PORT

    def format_param(self) -> Optional[str]:
        """Extract `format` from the query string if present."""
        if not self.query:
            return None
        for pair in self.query.split("&"):
            if pair.startswith("format="):
                return pair[len("format=") :]
        return None


def generate_agent_id() -> str:
    """
    Generate a fresh random 256-bit Agent ID, returned as 64-char lowercase hex.

    In production AGTP, Agent IDs are derived from Birth Certificate hashes
    at ACTIVATE time (see v06 §5.1). For v1 we generate them randomly because
    the Birth Certificate machinery doesn't exist yet. The cryptographic
    properties are equivalent at the wire level; the difference is purely
    in provenance.
    """
    return secrets.token_hex(AGENT_ID_BYTES)


def validate_agent_id(agent_id: str) -> None:
    """
    Validate that a string is a well-formed Agent ID.

    Raises AgentIDError on failure. Returns None on success.
    """
    if not isinstance(agent_id, str):
        raise AgentIDError(f"agent ID must be a string, got {type(agent_id).__name__}")
    if len(agent_id) != AGENT_ID_HEX_LENGTH:
        raise AgentIDError(
            f"agent ID must be {AGENT_ID_HEX_LENGTH} characters, "
            f"got {len(agent_id)}"
        )
    if not AGENT_ID_PATTERN.match(agent_id):
        raise AgentIDError(
            "agent ID must contain only lowercase hexadecimal characters"
        )


def _parse_port(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    try:
        port = int(text)
    except ValueError as exc:
        raise AgentIDError(f"invalid port number: {text!r}") from exc
    if not (1 <= port <= 65535):
        raise AgentIDError(f"port out of range (1-65535): {port}")
    return port


def parse_uri(uri: str) -> ParsedURI:
    """
    Parse an `agtp://` URI into its components.

    Tries Form 1 / 1a first (those start with 64 hex chars). Falls back
    to Form 2 (server-level) when the leading component is a hostname.
    Raises AgentIDError on malformed input.
    """
    if not isinstance(uri, str):
        raise AgentIDError(f"URI must be a string, got {type(uri).__name__}")

    text = uri.strip()

    agent_match = _AGENT_URI_PATTERN.match(text)
    if agent_match:
        return ParsedURI(
            agent_id=agent_match.group("agent_id"),
            host=agent_match.group("host"),
            port=_parse_port(agent_match.group("port")),
            query=agent_match.group("query"),
        )

    server_match = _SERVER_URI_PATTERN.match(text)
    if server_match:
        host = server_match.group("host")
        if not _HOSTNAME_PATTERN.match(host):
            raise AgentIDError(f"not a valid hostname: {host!r}")
        return ParsedURI(
            agent_id=None,
            host=host,
            port=_parse_port(server_match.group("port")),
            query=None,
        )

    raise AgentIDError(f"not a valid AGTP URI: {uri!r}")


def format_uri(
    agent_id: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    format_: Optional[str] = None,
) -> str:
    """
    Format components back into a canonical `agtp://` URI string.

    Either `agent_id` or `host` must be supplied. When both are
    supplied the result is Form 1a; agent-only is Form 1; host-only is
    Form 2.
    """
    if agent_id is None and host is None:
        raise AgentIDError("format_uri requires at least agent_id or host")

    if agent_id is not None:
        validate_agent_id(agent_id)
        uri = f"agtp://{agent_id}"
        if host:
            uri += f"@{host}"
            if port is not None and port != DEFAULT_AGTP_PORT:
                uri += f":{port}"
        if format_:
            uri += f"?format={format_}"
        return uri

    # Form 2: agtp://host[:port]
    if not _HOSTNAME_PATTERN.match(host):
        raise AgentIDError(f"not a valid hostname: {host!r}")
    uri = f"agtp://{host}"
    if port is not None and port != DEFAULT_AGTP_PORT:
        uri += f":{port}"
    return uri
