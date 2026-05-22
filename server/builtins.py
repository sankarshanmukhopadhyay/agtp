"""
Tier A registered built-in endpoints.

This module is one of two implementation mechanisms for Tier A
endpoints (the other being dispatcher-direct short-circuits like
the reserved DISCOVER roots). What lives here is registered into
the endpoint registry at startup, walks every dispatcher gate the
same way operator endpoints do, and shows up in the manifest like
any other endpoint — but the handler closure carries the
``__agtp_builtin__`` marker so
:func:`core.endpoint_tiers.classify_tier` knows the endpoint is
protocol-reserved, not operator-supplied.

AGTP exposes server metadata via AGTP methods at AGTP-native paths
rather than via HTTP-style well-known locations. Operator-authored
TOML can override a built-in by declaring an endpoint at the same
``(method, path)`` pair; in that case the built-in registration
silently skips on :class:`DuplicateEndpointError` so the operator's
choice wins. This is a deliberate escape hatch — in practice
operators rarely shadow built-ins.

See [`server/builtins.md`](builtins.md) for the operator-facing
catalogue and [`docs/endpoint-tiers.md`](../docs/endpoint-tiers.md)
for the full Tier A/B/C taxonomy.

Built-ins shipped today:

  * ``DISCOVER /methods`` — returns a lightweight ``method`` /
    ``path`` / ``description`` listing of every endpoint the
    server has registered. The full manifest (via target-less
    DISCOVER) carries the complete shape; this built-in is for
    clients that only want the inventory without the
    semantic-block / parameter / handler-binding overhead.
  * ``QUERY /proposals/{proposal_id}`` — agent polling surface for
    asynchronous PROPOSE evaluations (§7). Returns the current
    state of a proposal as 261 (pending), 263 (accepted), or 463
    (rejected). Only useful when the server has
    ``async_evaluation_enabled = true`` in its
    ``[policies.synthesis]`` block.

Pre-§6 servers shipped ``QUERY /methods`` returning the contents of
a ``methods.txt`` policy file. That file format has been retired
(see ``agtp-api §8``); the per-server method policy now lives in
``[policies.methods]`` of ``agtp-server.toml`` and surfaces in the
manifest's ``policies.methods`` block.

Adding a Tier A built-in
~~~~~~~~~~~~~~~~~~~~~~~~

1. Write a handler closure. Attach
   ``handler.__agtp_handler_kind__ = "registered_function"`` and
   ``handler.__agtp_builtin__ = "<short_label>"`` — the second
   marker is what
   :func:`core.endpoint_tiers.classify_tier` reads to distinguish
   Tier A from Tier B at lookup time.
2. Build an :class:`EndpointSpec` declaring the contract.
3. Add a ``_register(...)`` call in :func:`register_builtins`.
4. Update the Tier A inventory in
   :data:`core.endpoint_tiers.TIER_A_RESERVED_ENDPOINTS` if the
   new endpoint is also a protocol guarantee (most are).
5. Cross-reference the new built-in in
   [`server/builtins.md`](builtins.md) and
   [`docs/endpoint-tiers.md`](../docs/endpoint-tiers.md).
"""

from __future__ import annotations

import sys
from typing import Any, List

from agtp.handlers import EndpointContext, EndpointResponse
from core.endpoint import (
    EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
)


def _discover_methods_spec() -> EndpointSpec:
    """The :class:`EndpointSpec` registered for ``DISCOVER /methods``."""
    return EndpointSpec(
        name="DISCOVER",
        path="/methods",
        description=(
            "Lightweight inventory of registered (method, path) pairs. "
            "Same information as the manifest's ``endpoints`` array but "
            "without semantic block, parameters, or handler bindings."
        ),
        namespace="server-internal",
        semantic=SemanticBlock(
            intent=(
                "Enumerate the registered (method, path) endpoints the "
                "server exposes."
            ),
            actor="agent",
            outcome=(
                "An array of {method, path, description} entries "
                "covering every registered endpoint is returned."
            ),
            capability="discovery",
            confidence=0.95,
            impact="informational",
            is_idempotent=True,
        ),
        required_params=[],
        optional_params=[],
        output=[
            ParamSpec(
                name="methods",
                type="array",
                description=(
                    "Array of {method, path, description} entries — one "
                    "per registered endpoint."
                ),
            ),
        ],
        errors=[],
        # The handler binding is a placeholder; the closure is
        # registered directly via ``endpoint_registry.register(spec,
        # handler)``. The binding type still has to be one the
        # registry validator admits.
        handler=HandlerBinding(
            type="registered_function",
            function="server.builtins.discover_methods",
        ),
    )


def discover_methods(endpoint_registry: Any) -> "callable":
    """
    Build the ``DISCOVER /methods`` handler closure.

    The closure captures the live ``endpoint_registry`` so the
    response always reflects the current registration set —
    including endpoints registered after this built-in's own
    registration. Returns a compact ``{method, path, description}``
    array; agents that need the full endpoint contract use the
    manifest's ``endpoints`` array instead.
    """

    def handler(_ctx: EndpointContext) -> EndpointResponse:
        section = list(endpoint_registry.render_manifest_section())
        out: List[dict] = []
        for entry in section:
            out.append({
                "method": entry.get("method", ""),
                "path": entry.get("path", ""),
                "description": entry.get("description", "") or "",
            })
        return EndpointResponse(body={"methods": out})

    handler.__agtp_handler_kind__ = "registered_function"
    handler.__agtp_builtin__ = "discover_methods"
    return handler


def _query_proposal_spec() -> EndpointSpec:
    """The :class:`EndpointSpec` registered for ``QUERY /proposals``
    — the §7 async PROPOSE poll surface.

    The spec text describes this surface as
    ``QUERY /proposals/{proposal_id}``; the endpoint registry's
    exact-match lookup doesn't yet route path templates (deferred to
    a follow-up), so the v00 implementation accepts ``proposal_id``
    as a required body parameter on a literal-path endpoint. The
    polling_path field on a 261 response points callers at this
    surface; both spec and implementation agree on the wire shape
    of the response.
    """
    return EndpointSpec(
        name="QUERY",
        path="/proposals",
        description=(
            "Poll the state of an asynchronous PROPOSE evaluation. "
            "The body's proposal_id names the evaluation; the response "
            "is the same shape the original PROPOSE would have returned "
            "synchronously: 261 while pending, 263 on accept, 463 on "
            "reject."
        ),
        namespace="server-internal",
        semantic=SemanticBlock(
            intent=(
                "Retrieve the current state of an asynchronous PROPOSE "
                "evaluation by its proposal_id."
            ),
            actor="agent",
            outcome=(
                "Either a 261 in-progress response (evaluation ongoing), "
                "a 263 proposal-approved response (synthesis ready), or "
                "a 463 proposal-rejected response (refusal final)."
            ),
            capability="discovery",
            confidence=0.95,
            impact="informational",
            is_idempotent=True,
        ),
        required_params=[
            ParamSpec(
                name="proposal_id",
                type="string",
                description="The proposal identifier to look up.",
            ),
        ],
        optional_params=[],
        output=[
            ParamSpec(
                name="proposal_id",
                type="string",
                description="The proposal identifier echoed in the response.",
            ),
            ParamSpec(
                name="state",
                type="string",
                description=(
                    "One of ``pending`` / ``accepted`` / ``rejected``."
                ),
            ),
        ],
        errors=[],
        handler=HandlerBinding(
            type="registered_function",
            function="server.builtins.query_proposal",
        ),
    )


def query_proposal(proposal_store: Any) -> "callable":
    """Build the ``QUERY /proposals`` handler closure.

    The closure captures the server's :class:`ProposalStore` and
    resolves polls against its current state. The poll forwards the
    stored response body verbatim so the agent sees the same
    body shape the original PROPOSE would have returned
    synchronously.

    The handler returns ``wire.AGTPResponse`` directly so it can
    carry arbitrary status codes (261 / 263 / 463 / 404) without
    fitting the endpoint's success-shape output schema.
    """
    import json as _json
    from core import wire as _wire

    def _response(status: tuple, body: dict) -> Any:
        body_bytes = _json.dumps(body, indent=2).encode("utf-8")
        return _wire.AGTPResponse(
            status_code=status[0],
            status_text=status[1],
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body_bytes)),
            },
            body_bytes=body_bytes,
        )

    def handler(ctx: Any) -> Any:
        # EndpointContext stores the validated request body under
        # ``input``; the required-param gate already ensured
        # ``proposal_id`` is present.
        params = getattr(ctx, "input", None) or {}
        proposal_id = str(params.get("proposal_id") or "")
        if not proposal_id:
            return _response(
                (400, "Bad Request"),
                {"error": {
                    "code": "missing-required-param",
                    "explanation": "proposal_id is required",
                }},
            )
        record = proposal_store.lookup(proposal_id)
        if record is None:
            return _response(
                (404, "Not Found"),
                {"error": {
                    "code": "proposal-not-found",
                    "explanation": (
                        f"proposal {proposal_id!r} is not known to this "
                        f"server"
                    ),
                }},
            )
        if record.state == "pending":
            return _response(
                (261, "Negotiation In Progress"),
                {
                    "proposal_id": proposal_id,
                    "state": "pending",
                    "polling_path": "/proposals",
                    "evaluation_started_at": (
                        proposal_store.evaluation_started_at(proposal_id)
                    ),
                    "max_evaluation_duration": (
                        proposal_store.max_evaluation_duration_str()
                    ),
                },
            )
        # Resolved — accepted (263) or rejected (463). Forward the
        # stored body verbatim with the matching status code, plus
        # a ``state`` / ``proposal_id`` echo.
        body = dict(record.result_body or {})
        body["state"] = record.state
        body["proposal_id"] = proposal_id
        if record.result_status == 263:
            return _response((263, "Proposal Approved"), body)
        if record.result_status == 463:
            return _response((463, "Proposal Rejected"), body)
        return _response((200, "OK"), body)

    handler.__agtp_handler_kind__ = "registered_function"
    handler.__agtp_builtin__ = "query_proposal"
    return handler


def register_builtins(endpoint_registry: Any, *, proposal_store: Any = None) -> int:
    """
    Register every built-in endpoint on ``endpoint_registry``.
    Returns the count of successfully-registered built-ins.

    Operator-authored TOML that already registered a built-in's
    ``(method, path)`` pair takes precedence — the built-in
    registration silently skips on
    :class:`server.endpoint_registry.DuplicateEndpointError`.

    ``proposal_store`` (optional) wires the §7 async-PROPOSE poll.
    When omitted the ``QUERY /proposals/{proposal_id}`` endpoint is
    not registered; callers that don't care about async evaluation
    can leave it out.
    """
    from server.endpoint_registry import (
        DuplicateEndpointError, InvalidEndpointError,
    )

    registered = 0

    def _register(spec: EndpointSpec, handler: Any, label: str) -> None:
        nonlocal registered
        try:
            endpoint_registry.register(spec, handler)
            registered += 1
        except DuplicateEndpointError:
            # Operator override; respect it silently.
            pass
        except InvalidEndpointError as exc:
            print(
                f"[server] built-in endpoint {label} refused: {exc}",
                file=sys.stderr,
            )

    _register(
        _discover_methods_spec(),
        discover_methods(endpoint_registry),
        "(DISCOVER, /methods)",
    )

    if proposal_store is not None:
        _register(
            _query_proposal_spec(),
            query_proposal(proposal_store),
            "(QUERY, /proposals/{proposal_id})",
        )

    return registered


__all__ = [
    "discover_methods",
    "query_proposal",
    "register_builtins",
]
