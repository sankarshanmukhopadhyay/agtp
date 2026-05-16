"""
Sample handlers demonstrating the M3 step (b) ``@endpoint`` decorator.

Run alongside ``agtpd --gateway-socket`` to see end-to-end gateway
dispatch in action::

    # terminal 1
    python -m server --gateway-socket /tmp/agtpd.sock \\
        --agents-dir server/agents --endpoints-dir endpoints

    # terminal 2
    python -m mod_python \\
        --gateway-socket /tmp/agtpd.sock \\
        --load-module samples.gateway_demo

When agtpd's TOML endpoints reference these functions by dotted path
(``samples.gateway_demo.echo``, ``samples.gateway_demo.book_room``),
the daemon sends the references in the ``register`` frame and this
module resolves them against ``agtp.registry``.

These handlers are intentionally tiny — the point is to exercise the
gateway round-trip, not to demonstrate AGTP modeling.
"""

from __future__ import annotations

from agtp import EndpointContext, EndpointError, EndpointResponse, endpoint


@endpoint(method="QUERY", path="/echo")
def echo(ctx: EndpointContext) -> EndpointResponse:
    """Return the input ``value`` unchanged. The simplest possible handler."""
    return EndpointResponse(body={"echo": ctx.input.get("value", "")})


@endpoint(
    method="BOOK",
    path="/room",
    errors=["room_unavailable"],
)
def book_room(ctx: EndpointContext):
    """Toy room-booking handler. Refuses the presidential suite."""
    room_type = ctx.input.get("room_type", "double")
    if room_type == "presidential_suite":
        return EndpointError(
            code="room_unavailable",
            message="The presidential suite is not available.",
            details={"room_type": room_type},
        )
    return EndpointResponse(body={
        "reservation_id": f"res-{ctx.input.get('guest', 'anon')}-{room_type}",
        "agent": ctx.agent_id,
    })
