"""
Agent ID generation, validation, and URI parsing.

Reference: draft-hood-independent-agtp (v07 draft). The canonical Agent-ID
is a 256-bit cryptographic identifier rendered as 64 lowercase hexadecimal
characters. URIs are of the form:

    agtp://{agent-id}                  Form 1: canonical (authoritative)
    agtp://{agent-id}@{host}[:{port}]  Form 1a: ID with explicit host

Form 1 requires registry lookup to discover the serving host. Form 1a
embeds the host directly, allowing resolution before a registry exists.
Both forms resolve to the same canonical Agent-ID.
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

# Compiled URI pattern. Matches:
#   agtp://abc123...                       (no host, requires registry)
#   agtp://abc123...@example.com           (host, default port)
#   agtp://abc123...@example.com:4480      (host and port)
#   agtp://abc123...?format=agent.json     (with query)
URI_PATTERN = re.compile(
    r"^agtp://"
    r"(?P<agent_id>[0-9a-f]{64})"
    r"(?:@(?P<host>[a-zA-Z0-9.\-]+)(?::(?P<port>\d+))?)?"
    r"(?:\?(?P<query>.*))?$"
)


class AgentIDError(ValueError):
    """Raised when an Agent ID or URI fails validation."""


@dataclass
class ParsedURI:
    """Result of parsing an `agtp://` URI."""

    agent_id: str
    host: Optional[str] = None
    port: Optional[int] = None
    query: Optional[str] = None

    @property
    def has_explicit_host(self) -> bool:
        """True if the URI is Form 1a (host embedded)."""
        return self.host is not None

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


def parse_uri(uri: str) -> ParsedURI:
    """
    Parse an `agtp://` URI into its components.

    Raises AgentIDError on malformed input.
    """
    if not isinstance(uri, str):
        raise AgentIDError(f"URI must be a string, got {type(uri).__name__}")

    match = URI_PATTERN.match(uri.strip())
    if not match:
        raise AgentIDError(f"not a valid AGTP URI: {uri!r}")

    agent_id = match.group("agent_id")
    host = match.group("host")
    port_str = match.group("port")
    query = match.group("query")

    port: Optional[int] = None
    if port_str is not None:
        try:
            port = int(port_str)
        except ValueError as exc:
            raise AgentIDError(f"invalid port number: {port_str!r}") from exc
        if not (1 <= port <= 65535):
            raise AgentIDError(f"port out of range (1-65535): {port}")

    return ParsedURI(agent_id=agent_id, host=host, port=port, query=query)


def format_uri(
    agent_id: str,
    host: Optional[str] = None,
    port: Optional[int] = None,
    format_: Optional[str] = None,
) -> str:
    """
    Format components back into a canonical `agtp://` URI string.
    """
    validate_agent_id(agent_id)

    uri = f"agtp://{agent_id}"
    if host:
        uri += f"@{host}"
        if port is not None and port != DEFAULT_AGTP_PORT:
            uri += f":{port}"
    if format_:
        uri += f"?format={format_}"
    return uri
