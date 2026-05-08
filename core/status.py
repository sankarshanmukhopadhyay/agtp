"""
AGTP status codes used by handlers and middleware.

The Interaction Model design note (sections 4 and 6) introduces three
new client-refusal codes alongside the existing 451 Scope Violation:

    451 Scope Violation                       (existing)
    452 Method Not Permitted for Agent       (soft-deny / permission)
    460 Negotiation Refused                   (PROPOSE)
    461 Counter-Proposal                      (PROPOSE)
    462 Wildcards Refused                     (server policy)

Each code has a canonical (status, reason-phrase) tuple plus a
helper that emits the standard JSON body shape. Building responses
through these helpers keeps wire output consistent across handlers.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core import wire


# -- (status_code, status_text) constants -----------------------------

SCOPE_VIOLATION = (451, "Scope Violation")
# 452 reframed: agents are users, not APIs. Their requires.methods
# expresses what the principal has authorized them to invoke; absence
# is a refusal of permission, not a missing capability on the agent.
METHOD_NOT_PERMITTED_FOR_AGENT = (452, "Method Not Permitted for Agent")
# Backward-compat alias kept so external callers that imported the
# previous constant name continue to compile. New code should prefer
# METHOD_NOT_PERMITTED_FOR_AGENT.
METHOD_OUTSIDE_NEED = METHOD_NOT_PERMITTED_FOR_AGENT
NEGOTIATION_REFUSED = (460, "Negotiation Refused")
COUNTER_PROPOSAL = (461, "Counter-Proposal")
WILDCARDS_REFUSED = (462, "Wildcards Refused")


# -- Refusal reason strings used by 460 -------------------------------

REFUSAL_OUT_OF_SCOPE = "out_of_scope"
REFUSAL_AMBIGUOUS = "ambiguous"
REFUSAL_INSUFFICIENT = "insufficient"
REFUSAL_POLICY_REFUSED = "policy_refused"
REFUSAL_NOT_IMPLEMENTED = "not_implemented"

ALL_REFUSAL_REASONS = {
    REFUSAL_OUT_OF_SCOPE,
    REFUSAL_AMBIGUOUS,
    REFUSAL_INSUFFICIENT,
    REFUSAL_POLICY_REFUSED,
    REFUSAL_NOT_IMPLEMENTED,
}


def _wrap(payload: Dict[str, Any]) -> wire.AGTPResponse:
    body = json.dumps(payload, indent=2).encode("utf-8")
    return wire.AGTPResponse(
        status_code=payload["__status"][0],
        status_text=payload["__status"][1],
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
        body_bytes=body,
    )


def _build(
    status: tuple, *, body: Dict[str, Any]
) -> wire.AGTPResponse:
    payload = dict(body)
    payload["__status"] = status
    raw = _wrap(payload).body_bytes
    # Strip the synthetic key from the actual on-wire body. We use it
    # only as a vehicle for passing the (code, text) tuple into _wrap.
    parsed = json.loads(raw.decode("utf-8"))
    parsed.pop("__status", None)
    body_bytes = json.dumps(parsed, indent=2).encode("utf-8")
    return wire.AGTPResponse(
        status_code=status[0],
        status_text=status[1],
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body_bytes)),
        },
        body_bytes=body_bytes,
    )


# -- 452 Method Not Permitted for Agent -------------------------------


def method_not_permitted_for_agent(
    method: str,
    agent_id: str,
    *,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Soft-deny / permission-refusal response.

    The target agent's ``requires.methods`` does not include ``method``
    and ``wildcards`` is false. The body framing emphasizes that the
    principal has not authorized this method, not that the agent
    "lacks" anything; agents are users, not APIs.
    """
    if explanation is None:
        explanation = (
            f"This agent's permissions do not include {method}. "
            f"The principal has not authorized this method."
        )
    return _build(
        METHOD_NOT_PERMITTED_FOR_AGENT,
        body={
            "error": {
                "code": "method-not-permitted-for-agent",
                "method": method,
                "agent_id": agent_id,
                "explanation": explanation,
            }
        },
    )


# Backward-compat alias. Callers using the old name still work; the
# wire shape is identical.
method_outside_need = method_not_permitted_for_agent


# -- 462 Wildcards Refused --------------------------------------------


def wildcards_refused(
    agent_id: str,
    *,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Server policy ``wildcards_accepted=false`` rejects an agent that
    declares ``requires.wildcards: true`` invoking a non-embedded
    method.
    """
    if explanation is None:
        explanation = (
            "Server policy.wildcards_accepted is false; "
            "agent declares wildcards: true."
        )
    return _build(
        WILDCARDS_REFUSED,
        body={
            "error": {
                "code": "wildcards-refused",
                "agent_id": agent_id,
                "explanation": explanation,
            }
        },
    )


# -- 460 Negotiation Refused -------------------------------------------


def negotiation_refused(
    reason: str,
    explanation: str,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> wire.AGTPResponse:
    if reason not in ALL_REFUSAL_REASONS:
        # Defensive: callers should use the constants.
        raise ValueError(f"unknown negotiation refusal reason: {reason!r}")
    body = {
        "error": {
            "code": "negotiation-refused",
            "reason": reason,
            "explanation": explanation,
        }
    }
    if extra:
        body["error"].update(extra)
    return _build(NEGOTIATION_REFUSED, body=body)


# -- 461 Counter-Proposal ---------------------------------------------


def counter_proposal(spec: Dict[str, Any]) -> wire.AGTPResponse:
    """
    Return a counter-proposal naming an existing or near-existing
    method the server is willing to accept. ``spec`` is a MethodSpec
    serialized to a dict (see ``agtp.methods.spec_to_dict``).
    """
    return _build(
        COUNTER_PROPOSAL,
        body={"counter_proposal": spec},
    )


# -- 451 Scope Violation (existing semantics; helper for symmetry) ---


def scope_violation(
    method: str,
    missing_scopes: List[str],
    *,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    if explanation is None:
        explanation = (
            f"{method} requires scope(s) the caller has not presented: "
            f"{', '.join(missing_scopes)}"
        )
    return _build(
        SCOPE_VIOLATION,
        body={
            "error": {
                "code": "scope-violation",
                "method": method,
                "missing_scopes": list(missing_scopes),
                "explanation": explanation,
            }
        },
    )


__all__ = [
    "ALL_REFUSAL_REASONS",
    "COUNTER_PROPOSAL",
    "METHOD_NOT_PERMITTED_FOR_AGENT",
    "METHOD_OUTSIDE_NEED",
    "NEGOTIATION_REFUSED",
    "REFUSAL_AMBIGUOUS",
    "REFUSAL_INSUFFICIENT",
    "REFUSAL_NOT_IMPLEMENTED",
    "REFUSAL_OUT_OF_SCOPE",
    "REFUSAL_POLICY_REFUSED",
    "SCOPE_VIOLATION",
    "WILDCARDS_REFUSED",
    "counter_proposal",
    "method_not_permitted_for_agent",
    "method_outside_need",
    "negotiation_refused",
    "scope_violation",
    "wildcards_refused",
]
