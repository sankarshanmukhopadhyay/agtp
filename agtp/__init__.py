"""
``agtp`` — public API surface for AGTP handler authors.

The Python handler-side library. Pairs with ``mod_python`` (the
runtime module that connects to ``agtpd`` over the gateway socket;
see [`docs/architecture/gateway-protocol.md`](../docs/architecture/gateway-protocol.md))
to serve AGTP traffic in Python.

Public surface::

    from agtp import (
        EndpointContext, EndpointResponse, EndpointError,
        endpoint, registry,
    )

Declare an endpoint::

    @endpoint(method="BOOK", path="/room", errors=["room_unavailable"])
    def book_room(ctx: EndpointContext):
        return EndpointResponse(body={"reservation_id": "..."})

Test it without spinning up the daemon::

    from agtp.testing import make_context, assert_ok

    def test_book_room():
        ctx = make_context(input={"room_type": "double", ...})
        response = assert_ok(book_room(ctx))
        assert "reservation_id" in response.body

The internal modules (``server.*``, ``client.*``, ``core.*``) are
not part of this public surface and may move between releases.
Stable contract lives behind the symbols re-exported here and in
``agtp.testing``.
"""

from __future__ import annotations

from agtp.handlers import (
    EndpointContext,
    EndpointError,
    EndpointResponse,
    HandlerResult,
)
from agtp.registry import (
    HandlerFn,
    HandlerRegistry,
    RegisteredHandler,
    endpoint,
    registry,
)


__all__ = [
    "EndpointContext",
    "EndpointError",
    "EndpointResponse",
    "HandlerFn",
    "HandlerRegistry",
    "HandlerResult",
    "RegisteredHandler",
    "endpoint",
    "registry",
]
