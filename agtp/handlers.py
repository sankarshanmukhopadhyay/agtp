"""
Public handler API.

This is the surface a handler author imports from. Three small
dataclasses, one signature::

    from agtp.handlers import EndpointContext, EndpointResponse, EndpointError

    def book_room(ctx: EndpointContext):
        if ctx.input["room_type"] not in AVAILABLE:
            return EndpointError(
                code="room_unavailable",
                message="The requested room type is not available.",
                details={"room_type": ctx.input["room_type"]},
            )
        reservation_id = create_reservation(...)
        return EndpointResponse(body={"reservation_id": reservation_id})

The server resolves the handler, validates the incoming body
against the endpoint's input schema, builds a
:class:`EndpointContext`, calls the handler, and translates the
returned :class:`EndpointResponse` or :class:`EndpointError` into
the right AGTP wire response.

Handler authors do **not** depend on AGTPRequest / AGTPResponse —
those are the wire layer. This module is the abstraction the
handler stays inside.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Union, runtime_checkable


# ---------------------------------------------------------------------------
# EndpointContext.
# ---------------------------------------------------------------------------


@dataclass
class EndpointContext:
    """
    Per-request context handed to the handler.

    Fields:

      * ``input``         the validated request body. The dispatcher
                          ran the body through the endpoint's input
                          schema before calling the handler, so every
                          required field is present and every value
                          satisfies its declared type / format / enum.
      * ``agent_id``      the invoking agent's identity (the ``agent_id``
                          extracted from the ``Agent-ID`` header — or
                          legacy ``Target-Agent`` — or the URI). May
                          be empty for server-level probes.
      * ``principal_id``  identifier of the human or entity the agent
                          acts on behalf of, lifted from the resolved
                          Agent Document's ``principal_id`` field.
                          Empty when the agent does not declare a
                          principal or when the request is a
                          server-level probe. Surfaced for handlers
                          that need to log, audit, or branch on who
                          the agent represents.
      * ``agent_scopes``  the scopes the calling agent has declared.
                          The dispatcher's authority gate already
                          checked these against the endpoint's
                          ``required_scopes``; the list is surfaced to
                          the handler in case finer-grained checks are
                          needed.
      * ``authority_scope`` the §10 ``Authority-Scope`` header value
                          (claimed scopes for this specific request).
                          Empty list when the header is absent. The
                          dispatcher's claim-validation gate already
                          verified every entry against the agent's
                          declared scopes; handlers may consult it
                          for finer-grained authority decisions.
      * ``session_id``    the §10 ``Session-ID`` header — an opaque
                          operational grouping identifier. The
                          server doesn't interpret it; it's passed
                          through for handler-level session
                          tracking. ``None`` when absent.
      * ``task_id``       the §10 ``Task-ID`` header — identifies a
                          specific task across multiple requests for
                          tracing and audit. The server echoes it in
                          the response automatically; handlers can
                          read it for log correlation. ``None`` when
                          absent.
      * ``server_state``  opaque reference to the server's runtime
                          state (registry, runtime, etc.). Reserved
                          for advanced handlers; most don't need it.
      * ``request_id``    correlation identifier the handler can log
                          alongside its actions; matches the
                          ``Request-Id`` response header the server
                          emits.
      * ``method``        the AGTP verb the request carried.
      * ``path``          the URI path the request targeted.
      * ``headers``       the raw request headers, lowercased.
                          Reserved for handlers that need access to
                          values like ``Idempotency-Key`` or
                          ``Trace-Parent``.
    """

    input: Dict[str, Any]
    agent_id: str = ""
    principal_id: str = ""
    agent_scopes: List[str] = field(default_factory=list)
    authority_scope: List[str] = field(default_factory=list)
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    server_state: Optional[Any] = None
    request_id: str = ""
    method: str = ""
    path: str = "/"
    headers: Dict[str, str] = field(default_factory=dict)
    # Phase B mTLS trust signals. True when the daemon verified an
    # Agent Certificate during the TLS handshake AND the cert-derived
    # Agent-ID matches the request's agent_id. False for header-only
    # identity (Agent-ID header without mTLS) and for anonymous
    # requests. ``agent_cert_fingerprint`` is the SHA-256 of the cert
    # DER bytes, hex-encoded; null when not verified via cert.
    agent_verified: bool = False
    agent_cert_fingerprint: Optional[str] = None
    # Phase C: handler-side access to the daemon's gateway capabilities.
    # When running inside a runtime module that negotiated the
    # ``sign_request`` and/or ``outbound_call`` capabilities, this
    # field holds a :class:`DaemonClient` implementation. Handlers
    # call ``ctx.daemon.sign(bytes)`` and ``ctx.daemon.fetch(...)`` to
    # ask the daemon to sign or proxy on their behalf. ``None`` for
    # in-daemon dispatch (no module involved) and for runtime modules
    # that didn't claim the capability.
    daemon: Optional[Any] = None  # DaemonClient implementation


# ---------------------------------------------------------------------------
# EndpointResponse.
# ---------------------------------------------------------------------------


@dataclass
class EndpointResponse:
    """
    Returned by a handler on success.

    Fields:

      * ``body``    the response payload. The dispatcher validates
                    it against the endpoint's output schema before
                    sending it to the wire (so handler bugs surface
                    immediately during development).
      * ``status``  HTTP / AGTP status code. Defaults to ``200``.
                    Handlers may return ``201``, ``202``, or any
                    success-shaped code their endpoint contract
                    documents.
      * ``headers`` optional response headers the dispatcher should
                    include alongside ``Content-Type`` and
                    ``Content-Length``.
      * ``attribution_extra`` optional dict that rides in the JWS
                    payload of the response's Attribution-Record as a
                    top-level ``extra`` block. Handlers populate this
                    with governance-runtime metadata (``action_id``,
                    ``evaluation_id``, ``decision_id``,
                    ``intent_assertion_jti``, etc.) the daemon
                    doesn't itself produce. The daemon never invents
                    these fields; they're strictly handler-supplied
                    and pass through verbatim. ``None`` (the default)
                    omits the ``extra`` block entirely.
    """

    body: Dict[str, Any]
    status: int = 200
    headers: Optional[Dict[str, str]] = None
    attribution_extra: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# EndpointError.
# ---------------------------------------------------------------------------


@dataclass
class EndpointError:
    """
    Returned by a handler when an expected error condition occurs.

    The ``code`` must be one of the names declared in the endpoint's
    ``errors`` list (e.g. ``"room_unavailable"``). The dispatcher
    translates the error into a 422 response with a structured body::

        {
          "error": {
            "code": "room_unavailable",
            "message": "The requested room type is not available.",
            "details": {"room_type": "suite"}
          }
        }

    Use :class:`EndpointError` for predictable failure modes the
    contract describes. Use ``raise`` for unexpected exceptions —
    the dispatcher converts those to 500.

    Fields:

      * ``code``     the error name; must be in the endpoint's
                     ``errors`` list.
      * ``message``  operator / agent facing prose.
      * ``details``  optional structured detail. JSON-serializable.
    """

    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


#: The handler signature. Phase 2 normalizes every handler — whether
#: registered_function, composition, or external_service — to take a
#: single :class:`EndpointContext` and return either a response or
#: an error.
HandlerResult = Union[EndpointResponse, EndpointError]


# ---------------------------------------------------------------------------
# DaemonClient — Phase C gateway capabilities exposed to handlers.
# ---------------------------------------------------------------------------


@dataclass
class OutboundResponse:
    """Response from a daemon-mediated outbound AGTP call.

    Returned by :meth:`DaemonClient.fetch` when the upstream answers
    with a 2xx. Upstream errors are raised as exceptions; handlers
    catch them or let them propagate as ``handler_exception``.
    """

    status: int
    headers: Dict[str, str]
    body: Dict[str, Any]


class DaemonError(Exception):
    """Raised by :class:`DaemonClient` methods on protocol or upstream failure.

    The ``code`` attribute carries one of the structured codes from
    the gateway spec's ``sign_error`` / ``outbound_error`` frames
    (e.g. ``signing_unavailable``, ``upstream_unreachable``,
    ``upstream_malformed``).
    """

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


@runtime_checkable
class DaemonClient(Protocol):
    """Handler-side view of the daemon's gateway-protocol capabilities.

    When a handler runs inside a runtime module (``mod_python``,
    ``mod_php``, …), the module sets ``EndpointContext.daemon`` to
    an instance implementing this protocol. Handlers use it to
    request Ed25519 signatures and outbound AGTP calls from the
    daemon, without managing keys, TLS state, or upstream
    connection pools themselves.

    The protocol is intentionally minimal. Methods may raise
    :class:`DaemonError` when the daemon refuses a request (capability
    not advertised, signing key not loaded, upstream unreachable).

    Each method consumes a corresponding gateway capability:

      * :meth:`sign` consumes the ``sign_request`` capability
      * :meth:`fetch` consumes the ``outbound_call`` capability

    Modules that don't claim the capability in their ``hello`` MUST
    pass a DaemonClient implementation that raises ``DaemonError``
    with code ``capability_not_claimed`` rather than silently failing.
    """

    def sign(self, data: bytes) -> bytes:
        """Ask the daemon to Ed25519-sign ``data``. Returns the 64-byte
        signature. Raises :class:`DaemonError` when signing is
        unavailable."""
        ...

    def fetch(
        self,
        uri: str,
        method: str,
        path: str = "/",
        *,
        body: Optional[Union[Dict[str, Any], str, bytes]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> OutboundResponse:
        """Ask the daemon to make an outbound AGTP request. Returns
        the upstream response. Raises :class:`DaemonError` on upstream
        failure or malformed response."""
        ...


__all__ = [
    "DaemonClient",
    "DaemonError",
    "EndpointContext",
    "EndpointError",
    "EndpointResponse",
    "HandlerResult",
    "OutboundResponse",
]
