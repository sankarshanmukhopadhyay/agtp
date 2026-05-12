"""
Sample handler implementations referenced by the documentation
TOML files in ``endpoints/``.

Each function below shows one shape an endpoint handler can take:

  * ``query_catalog`` returns a successful response with a
    structured body. The dispatcher validates the body against the
    endpoint's output schema.
  * ``book_room`` shows the EndpointError pattern: if the request
    asks for a room type the inventory doesn't carry, the handler
    returns a typed error with details. The dispatcher translates
    that into a 422 with a structured body.

Handlers stay above the wire layer — they accept a single
:class:`agtp.handlers.EndpointContext` and return a response or
error dataclass. The signature is uniform regardless of which
binding kind eventually points at the function.
"""

from __future__ import annotations

from typing import Dict, List

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse


# ---------------------------------------------------------------------------
# QUERY /catalog
# ---------------------------------------------------------------------------


_SAMPLE_PRODUCTS: List[Dict[str, object]] = [
    {"sku": "A1", "name": "Widget", "price": 9.99},
    {"sku": "A2", "name": "Gadget", "price": 19.99},
    {"sku": "A3", "name": "Sprocket", "price": 4.50},
]


def query_catalog(ctx: EndpointContext) -> EndpointResponse:
    """
    Bound to ``QUERY /catalog`` in
    ``endpoints/query_catalog.toml``. Returns the static sample
    catalog, optionally filtered by the ``category`` parameter.
    """
    category = ctx.input.get("category")
    items = _SAMPLE_PRODUCTS
    if category:
        items = [
            p for p in items
            if str(p.get("name", "")).lower().startswith(str(category).lower())
        ]
    return EndpointResponse(
        body={
            "items": list(items),
            # The output schema declares ``items`` as required, so
            # adding extra keys here is fine — the validator allows
            # additional output properties for forward compat.
            "count": len(items),
        }
    )


# ---------------------------------------------------------------------------
# BOOK /room
# ---------------------------------------------------------------------------


_AVAILABLE_ROOM_TYPES = {"single", "double"}  # 'suite' intentionally absent


def book_room(ctx: EndpointContext) -> EndpointResponse | EndpointError:
    """
    Bound to ``BOOK /room`` in ``endpoints/book_room.toml``. Returns
    a synthetic reservation_id for available rooms; surfaces a typed
    ``room_unavailable`` error for the room type the demo inventory
    doesn't carry.
    """
    room_type = str(ctx.input.get("room_type", ""))
    if room_type not in _AVAILABLE_ROOM_TYPES:
        return EndpointError(
            code="room_unavailable",
            message=(
                f"the {room_type!r} room type is not available at this "
                f"property"
            ),
            details={"requested": room_type,
                     "available": sorted(_AVAILABLE_ROOM_TYPES)},
        )

    # Synthetic confirmation. Real handlers would write to a
    # reservation system here. The output schema enforces
    # reservation_id / confirmation_email / total_cost being present.
    reservation_id = f"res-{ctx.input['guest_id'][:8]}-{room_type}"
    return EndpointResponse(
        body={
            "reservation_id": reservation_id,
            "confirmation_email": "guest@example.com",
            "total_cost": 199.00,
        }
    )
