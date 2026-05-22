"""
HTTP → AGTP translation server.

Implements the parallel HTTP listener that mod_http_gateway boots
when loaded. Translation is mechanical:

  HTTP request (GET /products HTTP/1.1)
    + Agent-ID identification (X-Agent-Id header or pinned)
    → AGTPRequest(method=resolve_alias(GET) or GET, path=/products, ...)
    → server.methods.dispatch(...)
    → AGTPResponse
    → HTTP response

The implementation uses the stdlib ``http.server`` so the module
ships with the daemon without adding dependencies. Threading is
on by default so a slow handler doesn't block other REST clients.

This module deliberately doesn't try to be a general-purpose HTTP
server. The transformations it performs:

  * Reads the request body up to the Content-Length.
  * Lowercases incoming headers, normalizes ``X-Agent-Id`` /
    ``X-Synthesis-Id`` to the daemon's canonical case.
  * Strips ``Allow-RCNS`` — REST callers cannot trigger RCNS
    negotiations (by design; the protocol is AGTP-native).
  * Resolves the HTTP verb via the daemon's
    ``[policies.methods.aliases]`` table; unknown verbs propagate
    to the daemon which returns 459.

For richer translation (per-route mappings, content negotiation,
auth chaining) operators fork this module or front it with a
proper reverse proxy.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Optional

from core import wire

if TYPE_CHECKING:
    from core.identity import AgentDocument


class HttpGatewayServer:
    """The parallel HTTP listener.

    Constructed by :func:`mod_http_gateway.install` and started on
    a daemon thread. The server's ``serve_forever`` accepts requests
    indefinitely until ``shutdown`` is called (typically at daemon
    exit).
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        server_state: Any,
        pinned_agent_id: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.server_state = server_state
        self.pinned_agent_id = pinned_agent_id
        handler = _make_handler(server_state, pinned_agent_id)
        self._httpd = ThreadingHTTPServer((host, port), handler)

    @property
    def server_address(self) -> tuple:
        return self._httpd.server_address

    def serve_forever(self) -> None:
        self._serving = True
        try:
            self._httpd.serve_forever()
        except OSError:
            # Bind / accept failures during shutdown are normal.
            pass
        finally:
            self._serving = False

    def shutdown(self) -> None:
        """Stop the listener and release the bound port.

        Safe to call before :meth:`serve_forever` ever ran — the
        socket-only release path (``server_close``) runs unconditionally;
        the loop-shutdown only fires when the serve loop is active to
        avoid the stdlib's blocking-wait deadlock that triggers when
        you ask it to stop something that never started.
        """
        if getattr(self, "_serving", False):
            self._httpd.shutdown()
        try:
            self._httpd.server_close()
        except OSError:
            pass


def _make_handler(server_state: Any, pinned_agent_id: str) -> type:
    """Build the BaseHTTPRequestHandler subclass that closes over the
    daemon's ``server_state``. Using a closure keeps the http.server
    API (handler class, not instance) compatible with our need to
    inject context."""

    class _Handler(BaseHTTPRequestHandler):
        # Suppress the default stderr access log; the daemon already
        # owns logging via its audit and signing layers.
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _serve_request(self, http_method: str) -> None:
            try:
                self._translate_and_dispatch(http_method)
            except Exception as exc:  # noqa: BLE001
                # Last-ditch error envelope so a buggy handler never
                # crashes the HTTP server thread.
                body = json.dumps({
                    "error": {
                        "code": "gateway-internal-error",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                }).encode("utf-8")
                self.send_response(500, "Internal Server Error")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def _translate_and_dispatch(self, http_method: str) -> None:
            # Read the body (if any).
            length = int(self.headers.get("Content-Length") or "0")
            body_bytes = self.rfile.read(length) if length > 0 else b""

            # Resolve Agent-ID — header first, then pinned fallback.
            agent_id_raw = (
                self.headers.get("X-Agent-Id")
                or self.headers.get("Agent-ID")
                or pinned_agent_id
            )
            if not agent_id_raw:
                self._reply_json(
                    401, "Unauthorized",
                    {
                        "error": {
                            "code": "missing-agent-id",
                            "detail": (
                                "REST gateway requires an X-Agent-Id "
                                "header or AGTP_HTTP_GATEWAY_AGENT_ID "
                                "operator-pinned value"
                            ),
                        }
                    },
                )
                return

            # Build the AGTP request. Headers carry over with the
            # daemon's canonical names; Allow-RCNS is stripped so
            # REST callers can never trigger negotiation.
            agtp_headers: dict = {
                "Agent-ID": agent_id_raw,
                "Content-Length": str(length),
            }
            # Translate a handful of common HTTP headers to their
            # AGTP equivalents.
            forwards = {
                "x-request-id": "Request-ID",
                "x-task-id": "Task-ID",
                "x-session-id": "Session-ID",
                "x-synthesis-id": "Synthesis-Id",
                "rcns-idempotency-key": "RCNS-Idempotency-Key",
                "content-type": "Content-Type",
            }
            for hk, ak in forwards.items():
                val = self.headers.get(hk)
                if val:
                    agtp_headers[ak] = val
            # Explicitly drop Allow-RCNS — REST callers can't fire
            # RCNS regardless of what they send. This is documented
            # in operational/mod_http_gateway/README.md.
            agtp_headers.pop("Allow-RCNS", None)

            request = wire.AGTPRequest(
                method=http_method.upper(),
                path=self.path,
                headers=agtp_headers,
                body_bytes=body_bytes,
            )

            # Resolve the agent. Cleanly fail on unknown ids rather
            # than letting the daemon's _select_target produce a
            # different error shape.
            agent_doc = _resolve_agent_doc(server_state, agent_id_raw)
            if agent_doc is None:
                self._reply_json(
                    404, "Not Found",
                    {
                        "error": {
                            "code": "agent-not-found",
                            "detail": (
                                f"no agent with id {agent_id_raw!r} on "
                                f"this server"
                            ),
                            "agent_id": agent_id_raw,
                        }
                    },
                )
                return

            # Dispatch through the daemon's regular path.
            from server.main import _finalize_response
            from server.methods import dispatch
            config = getattr(server_state, "config", None)
            agtp_response = dispatch(
                request, server_state, agent_doc, config=config,
            )

            # Run the same response-finalization the AGTP wire
            # listener runs so REST traffic produces the same
            # Attribution-Records, Server-ID stamps, header echoes,
            # and audit chain entries that AGTP-native traffic does.
            # Without this REST callers would silently bypass §10
            # response-policy enforcement.
            attribution_extra = getattr(agtp_response, "_attribution_extra", None)
            if attribution_extra is not None:
                try:
                    delattr(agtp_response, "_attribution_extra")
                except AttributeError:
                    pass
            # RCNS-5: surface the original HTTP verb on the
            # Attribution-Record. The alias resolution gate already
            # stashed _aliased_from on the request; merge into the
            # attribution_extra so the JWS payload carries
            # requested_method.
            aliased_from = getattr(request, "_aliased_from", None)
            if aliased_from:
                if attribution_extra is None:
                    attribution_extra = {}
                attribution_extra.setdefault("requested_method", aliased_from)
            _finalize_response(
                agtp_response, request, config,
                attribution_extra=attribution_extra,
                owner_id=getattr(agent_doc, "owner_id", "") or "",
                principal_id=getattr(agent_doc, "principal_id", "") or "",
                trust_tier=getattr(agent_doc, "trust_tier", None),
                trust_warning=getattr(agent_doc, "trust_warning", "") or "",
                verification_path=(
                    getattr(agent_doc, "verification_path", "") or ""
                ),
            )

            # Serialize the AGTP response back as HTTP.
            self.send_response(
                agtp_response.status_code,
                agtp_response.status_text or "OK",
            )
            for k, v in (agtp_response.headers or {}).items():
                self.send_header(k, str(v))
            self.end_headers()
            if agtp_response.body_bytes:
                self.wfile.write(agtp_response.body_bytes)

        def _reply_json(self, status: int, text: str, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status, text)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        # HTTP verbs we serve. Each just delegates to the shared
        # translator. The stdlib's BaseHTTPRequestHandler dispatches
        # by method name (do_GET, do_POST, etc.) — we map them all
        # to a single helper that hands the verb to the daemon.
        def do_GET(self) -> None:      # noqa: N802 (stdlib convention)
            self._serve_request("GET")
        def do_POST(self) -> None:     # noqa: N802
            self._serve_request("POST")
        def do_PUT(self) -> None:      # noqa: N802
            self._serve_request("PUT")
        def do_DELETE(self) -> None:   # noqa: N802
            self._serve_request("DELETE")
        def do_PATCH(self) -> None:    # noqa: N802
            self._serve_request("PATCH")
        def do_HEAD(self) -> None:     # noqa: N802
            self._serve_request("HEAD")

    return _Handler


def _resolve_agent_doc(server_state: Any, agent_id: str) -> Optional["AgentDocument"]:
    """Look up the agent document via the server state's standard
    accessors. The ``AgentRegistry`` exposes ``lookup`` for both
    agent_id and short-name resolution; we accept the wire-supplied
    id verbatim."""
    lookup = getattr(server_state, "lookup", None)
    if lookup is None:
        return None
    return lookup(agent_id.strip().lower())
