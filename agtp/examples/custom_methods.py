"""
Example custom (AMG-flagged) methods.

This module shows how a deployment registers verbs beyond the AGTP
embedded twelve. Importing it runs `register_custom` at module-load
time, so the methods appear in REGISTRY and DISCOVER /methods reports
them in the `custom` bucket with `source="amg/1.0"`.

Nothing in core depends on this module. Servers opt in explicitly via
import or `--load-module agtp.examples.custom_methods`.
"""

from __future__ import annotations

from agtp import wire
from agtp.identity import AgentDocument
from agtp.methods import (
    REGISTRY,
    ServerState,
    error_response,
    json_response,
    parse_body,
    register_custom,
    require_params,
)


def handle_reconcile(
    request: wire.AGTPRequest,
    server_state: ServerState,
    agent_doc: AgentDocument,
) -> wire.AGTPResponse:
    spec = REGISTRY["RECONCILE"]
    try:
        params = parse_body(request)
    except ValueError as exc:
        return error_response(400, "Bad Request", "invalid-body", str(exc))

    err = require_params(spec, params)
    if err:
        return err

    return json_response(
        200,
        "OK",
        {
            "method": "RECONCILE",
            "namespace": spec.namespace,
            "agent_id": agent_doc.agent_id,
            "account_id": params["account_id"],
            "period": params["period"],
            "tolerance": params.get("tolerance", 0.01),
            "reconciliation_id": "rec-stub-0001",
            "status": "stub-reconciled",
            "discrepancies": [],
        },
        method_name="RECONCILE",
    )


def install() -> None:
    """
    Idempotent registration. Calling install() twice is a no-op (the
    second call is suppressed because the method is already in
    REGISTRY). Tests call this explicitly; servers can call it after
    importing the module.
    """
    if "RECONCILE" in REGISTRY:
        return
    register_custom(
        handle_reconcile,
        name="RECONCILE",
        namespace="acme-finance",
        category="transact",
        semantic_class="action-intent",
        idempotent=False,
        state_modifying=True,
        required_params=["account_id", "period"],
        optional_params=["tolerance"],
        error_codes=[400, 404, 405, 422, 451],
        description=(
            "Reconcile transactions for a given account and period."
        ),
    )


# Auto-install on import so the typical use case (server adds the
# import to its startup file) needs no extra step.
install()
