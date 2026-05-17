"""
mod_proxy — forward AGTP requests to an upstream agtpd.

Loaded by ``agtpd`` via ``--load-module mod_proxy``. The module's
``install(server_state)`` function installs a resolver for the
``proxy`` handler-binding type into ``server.handler_resolution``.

After installation, operators can declare endpoint TOMLs with::

    [endpoint.handler]
    type = "proxy"
    url  = "agtp://other.example.com"

Each invocation of that endpoint opens an outbound AGTP connection
to the upstream, forwards the request envelope (preserving Agent-ID,
Authority-Scope, Session-ID, Task-ID), and returns the upstream's
response to the original caller.

Use cases:
- **Federation.** A central agtpd routes a subset of methods to
  partner servers.
- **Edge termination.** Terminate TLS at an edge node, proxy
  plaintext to internal agtpd instances on a private network.
- **Sharding.** Distribute load across multiple workers behind a
  single agtpd's public address.

Differs from the existing ``external_service`` binding (which
proxies to HTTP upstreams) in that the upstream speaks AGTP.
"""

from __future__ import annotations

from typing import Any

from mod_proxy.handler import resolve_proxy


__all__ = ["install", "resolve_proxy"]


def install(server_state: Any) -> None:
    """Register mod_proxy's resolver against
    :mod:`server.handler_resolution`. Idempotent — safe to call
    multiple times."""
    from server import handler_resolution as _hr
    setattr(_hr, "resolve_proxy", resolve_proxy)
    # Mark the module installed so introspection / tests can see it.
    setattr(_hr, "_mod_proxy_installed", True)
