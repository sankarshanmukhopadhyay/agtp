"""
Agent ID generation, validation, and URI parsing.

Reference: ``agtp §11``. The canonical Agent-ID is a 256-bit
cryptographic identifier rendered as 64 lowercase hexadecimal
characters. URIs come in six forms split across three addressing
strategies:

**Identity-anchored** (Forms 1 / 1a) — the URI carries the canonical
hex Agent-ID directly::

    agtp://{agent-id}                  Form 1:  canonical identity
    agtp://{agent-id}@{host}           Form 1a: identity + explicit host

**Server-level** (Forms 2 / 2a) — the URI addresses a server, not an
agent. Used for DISCOVER (manifest fetch). Forms 2 and 2a are
structurally identical; the distinction is a deployment convention::

    agtp://{host}                      Form 2:  server-level discovery
    agtp://{domain}                    Form 2a: organization-level

**Domain-anchored agent** (Forms 3 / 4) — the URI identifies an agent
by local name at a domain. The AGTP server at the domain looks up
the name against its ``hosted_agents`` manifest and routes
accordingly. Form 4 is a deployment convention using a dedicated
``agtp.`` subdomain::

    agtp://{domain}/agents/{name}      Form 3:  domain-anchored agent
    agtp://agtp.{domain}/agents/{name} Form 4:  subdomain-anchored agent

The canonical wire form omits port; the default port is 4480 and is
not specified in the URI (mirroring the convention HTTP uses for
443 / 80). The parser **accepts** ``:port`` as a non-canonical
convenience for dev / test / ephemeral hosting; ``format_uri``
never emits it.

The parser distinguishes Forms 1/1a from the others by inspecting
the leading authority. 64 lowercase hex characters means an
Agent-ID; anything else is a hostname. Domain-anchored agents
(Forms 3 / 4) are detected by the ``/agents/{name}`` path suffix on
a host-authority URI.

Sub-paths under ``/agents/{name}/...`` are reserved for future
revisions and rejected in v00. The Future Work for §11 notes this
may be better served by an explicit API surface rather than URI
nesting.
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

# Agent handle (Form 3 / 4 local name). Liberal — operators choose
# the naming convention. First char must be alphanumeric to avoid
# weirdness with leading punctuation.
_HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")

# Forms 1 / 1a: starts with a 64-char hex agent id.
_AGENT_URI_PATTERN = re.compile(
    r"^agtp://"
    r"(?P<agent_id>[0-9a-f]{64})"
    r"(?:@(?P<host>[a-zA-Z0-9.\-]+)(?::(?P<port>\d+))?)?"
    r"(?:\?(?P<query>.*))?$"
)

# Forms 3 / 4: domain-anchored agent. The host carries the domain
# (Form 3) or the ``agtp.`` subdomain convention (Form 4); the path
# is exactly ``/agents/{name}``.
_DOMAIN_AGENT_URI_PATTERN = re.compile(
    r"^agtp://"
    r"(?P<host>[A-Za-z0-9.\-]+)"
    r"(?::(?P<port>\d+))?"
    r"/agents/(?P<handle>[A-Za-z0-9][A-Za-z0-9._\-]*)"
    r"$"
)

# Forms 2 / 2a: server-level. No agent identifier. Query strings
# are not currently admitted on server URIs; if that changes the
# design note will say so.
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
    """Result of parsing an ``agtp://`` URI (per §11).

    Field semantics by form:

      * **Form 1**:  ``agent_id`` set, ``host`` / ``agent_handle``
                     None.
      * **Form 1a**: ``agent_id`` set, ``host`` set.
      * **Form 2 / 2a**: ``host`` set, ``agent_id`` / ``agent_handle``
                         None.
      * **Form 3**:  ``host`` set, ``agent_handle`` set
                     (``host`` does NOT start with ``agtp.``).
      * **Form 4**:  ``host`` set, ``agent_handle`` set
                     (``host`` starts with ``agtp.``).

    ``port`` is non-canonical — the parser accepts it for dev / test
    convenience but :func:`format_uri` never emits it. Canonical
    production URIs omit port (the default 4480 is implicit, matching
    the convention HTTP uses for 443 / 80).
    """

    agent_id: Optional[str] = None
    agent_handle: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    query: Optional[str] = None

    @property
    def has_explicit_host(self) -> bool:
        """True when the URI carries a host component (Forms 1a, 2,
        2a, 3, 4)."""
        return self.host is not None

    @property
    def is_server_level(self) -> bool:
        """True for Forms 2 / 2a — server addressed directly, no
        agent identifier in the URI."""
        return self.agent_id is None and self.agent_handle is None

    @property
    def is_domain_anchored(self) -> bool:
        """True for Forms 3 / 4 — agent identified by local name at a
        domain, requiring server-side resolution via the manifest's
        ``hosted_agents`` block."""
        return self.agent_handle is not None

    @property
    def form(self) -> str:
        """Return the URI form per ``agtp §11``: one of
        ``"1"`` / ``"1a"`` / ``"2"`` / ``"3"`` / ``"4"``.

        Forms 2 and 2a are structurally identical (both
        ``agtp://[host]``); the parser returns ``"2"`` for both. The
        "Form 2a" label is a spec-level deployment convention for
        bare-domain server URIs and is not surfaced by the parser.
        """
        if self.agent_id is not None:
            return "1a" if self.host is not None else "1"
        if self.agent_handle is not None:
            # Form 3 vs Form 4: distinguished by ``agtp.`` host prefix.
            return "4" if (self.host or "").startswith("agtp.") else "3"
        return "2"

    @property
    def effective_port(self) -> int:
        """Port to connect to, defaulting to 4480 when not specified.

        Production URIs omit port; the parser tolerates explicit
        ports for dev / test fixtures but the canonical form does
        not include them.
        """
        return self.port if self.port is not None else DEFAULT_AGTP_PORT

    def format_param(self) -> Optional[str]:
        """Extract ``format`` from the query string if present."""
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
    Parse an ``agtp://`` URI into its components.

    Resolution order (first match wins):

      1. **Forms 1 / 1a** — leading authority is a 64-char hex
         Agent-ID, optionally followed by ``@host``.
      2. **Forms 3 / 4** — host authority, path ``/agents/{name}``.
         Form 4 is the subdomain-convention variant (host prefixed
         with ``agtp.``); the parser doesn't structurally
         differentiate from Form 3 except via the
         :attr:`ParsedURI.form` property's host-prefix check.
      3. **Forms 2 / 2a** — bare host authority, no path or path
         is ``/``. Forms 2 and 2a are structurally identical (the
         "2a" label is a deployment convention for bare-domain
         server URIs); the parser returns ``form == "2"`` for both.

    Raises :class:`AgentIDError` on malformed input — including
    ``/agents/{name}/...`` sub-paths (Q4: reserved for future
    revisions per §11).
    """
    if not isinstance(uri, str):
        raise AgentIDError(f"URI must be a string, got {type(uri).__name__}")

    text = uri.strip()

    # Form 1 / 1a (hex authority).
    agent_match = _AGENT_URI_PATTERN.match(text)
    if agent_match:
        return ParsedURI(
            agent_id=agent_match.group("agent_id"),
            host=agent_match.group("host"),
            port=_parse_port(agent_match.group("port")),
            query=agent_match.group("query"),
        )

    # Forms 3 / 4 (domain-anchored agent).
    domain_match = _DOMAIN_AGENT_URI_PATTERN.match(text)
    if domain_match:
        host = domain_match.group("host")
        if not _HOSTNAME_PATTERN.match(host):
            raise AgentIDError(f"not a valid hostname: {host!r}")
        handle = domain_match.group("handle")
        if not _HANDLE_PATTERN.match(handle):
            raise AgentIDError(
                f"not a valid agent handle: {handle!r}"
            )
        return ParsedURI(
            agent_id=None,
            agent_handle=handle,
            host=host,
            port=_parse_port(domain_match.group("port")),
            query=None,
        )

    # Form 2 / 2a (server-level).
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

    # §11 Q4: ``/agents/{name}/...`` sub-paths are reserved for
    # future revisions. Detect the pattern explicitly so the error
    # message points the operator at the right concern.
    sub_path = re.match(
        r"^agtp://[A-Za-z0-9.\-]+(?::\d+)?/agents/[^/]+/.+$", text,
    )
    if sub_path is not None:
        raise AgentIDError(
            f"sub-paths under /agents/{{name}}/... are reserved for "
            f"future AGTP revisions; v00 supports only "
            f"/agents/{{name}} exactly. Got: {uri!r}"
        )

    raise AgentIDError(f"not a valid AGTP URI: {uri!r}")


def format_uri(
    agent_id: Optional[str] = None,
    agent_handle: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    format_: Optional[str] = None,
) -> str:
    """
    Format components back into a canonical ``agtp://`` URI string.

    Form selection rule:

      * ``agent_id`` only             → Form 1
      * ``agent_id`` + ``host``       → Form 1a
      * ``agent_handle`` + ``host``   → Form 3 (or Form 4 when
                                        ``host`` starts with ``agtp.``)
      * ``host`` only                  → Form 2 / 2a

    ``port`` is accepted for back-compat callers that still thread it
    through but is **never emitted** in canonical output (per §11).
    Production URIs omit port; the default 4480 is implicit. Callers
    that need to connect to a non-default port use a separate
    ``port=`` parameter on the transport layer, not the URI.

    Raises :class:`AgentIDError` when neither ``agent_id``,
    ``agent_handle``, nor ``host`` is supplied, or when an
    ``agent_handle`` is supplied without a ``host`` (Forms 3 / 4
    require both).
    """
    # ``port`` parameter retained for caller signature compatibility
    # but ignored in canonical output. Callers that need port-aware
    # behavior should pass it to the transport directly.
    del port  # canonical URIs omit port (§11)

    if agent_id is None and agent_handle is None and host is None:
        raise AgentIDError(
            "format_uri requires agent_id, agent_handle, or host"
        )
    if agent_handle is not None and host is None:
        raise AgentIDError(
            "agent_handle (Forms 3 / 4) requires a host component"
        )

    if agent_id is not None:
        validate_agent_id(agent_id)
        uri = f"agtp://{agent_id}"
        if host:
            uri += f"@{host}"
        if format_:
            uri += f"?format={format_}"
        return uri

    if agent_handle is not None:
        if not _HOSTNAME_PATTERN.match(host):
            raise AgentIDError(f"not a valid hostname: {host!r}")
        if not _HANDLE_PATTERN.match(agent_handle):
            raise AgentIDError(
                f"not a valid agent handle: {agent_handle!r}"
            )
        return f"agtp://{host}/agents/{agent_handle}"

    # Form 2 / 2a: agtp://host
    if not _HOSTNAME_PATTERN.match(host):
        raise AgentIDError(f"not a valid hostname: {host!r}")
    return f"agtp://{host}"
