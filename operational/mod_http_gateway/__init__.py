"""
mod_http_gateway — REST → AGTP translation sidecar (RCNS-5).

Loaded by ``agtpd`` via ``--load-module mod_http_gateway``. The
module's ``install(server_state)`` function starts a parallel HTTP
listener on a configurable port that:

  1. Accepts plain HTTP requests (the same shape ordinary web
     clients send).
  2. Translates the HTTP method to an AGTP verb via the daemon's
     ``[policies.methods.aliases]`` table (e.g. ``GET`` → ``FETCH``
     by default).
  3. Builds an :class:`core.wire.AGTPRequest` and dispatches it
     through the daemon's regular :func:`server.methods.dispatch`
     path, including all policy gates, the audit chain, and any
     hooks the daemon has registered.
  4. Serializes the daemon's :class:`core.wire.AGTPResponse` back
     to the HTTP client.

The daemon's AGTP wire stays untouched. This is purely a
side-listener that translates between two wire shapes; everything
else is shared with the AGTP listener.

Configuration via environment variables (defaults shown):

  * ``AGTP_HTTP_GATEWAY_ENABLED`` — set to ``"0"`` to load the
    module without starting the listener (1).
  * ``AGTP_HTTP_GATEWAY_HOST`` — bind host (127.0.0.1; loopback by
    default for safety).
  * ``AGTP_HTTP_GATEWAY_PORT`` — TCP port (8080).
  * ``AGTP_HTTP_GATEWAY_AGENT_ID`` — optional pinned Agent-ID when
    the HTTP client doesn't carry one. Without this, requests must
    set the ``X-Agent-Id`` header or get 401.

The gateway never triggers RCNS. REST callers' ``Allow-RCNS``
headers (if any) are stripped before the request enters dispatch.
A REST call to an unregistered ``(method, path)`` returns 404 —
RCNS is AGTP-native by design.

See :file:`README.md` for the verb-translation table, header
mapping, and limitations.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from .gateway import HttpGatewayServer


__all__ = ["HttpGatewayServer", "install"]


def install(server_state: Any) -> None:
    """Boot hook: start the HTTP listener on a background thread.

    Called by agtpd after ``--load-module mod_http_gateway``. The
    gateway thread is daemonized so the AGTP listener's exit cleanly
    tears it down.
    """
    if os.environ.get("AGTP_HTTP_GATEWAY_ENABLED", "1") == "0":
        return
    host = os.environ.get("AGTP_HTTP_GATEWAY_HOST") or "127.0.0.1"
    port = int(os.environ.get("AGTP_HTTP_GATEWAY_PORT") or "8080")
    pinned_agent = os.environ.get("AGTP_HTTP_GATEWAY_AGENT_ID") or ""
    gateway = HttpGatewayServer(
        host=host, port=port,
        server_state=server_state,
        pinned_agent_id=pinned_agent,
    )
    thread = threading.Thread(
        target=gateway.serve_forever,
        name="agtp-http-gateway",
        daemon=True,
    )
    thread.start()
    # Stash on the server_state so future tooling (or test harnesses)
    # can shut the gateway down cleanly.
    server_state.http_gateway = gateway
    import sys as _sys
    print(
        f"[server] mod_http_gateway listening on http://{host}:{port}",
        file=_sys.stderr,
    )
