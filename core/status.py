"""
AGTP status codes used by handlers and middleware.

The AGTP status code registry mixes standard HTTP codes with a small
number of AGTP-specific codes drawn from ranges unassigned in the
IANA HTTP Status Code Registry. The full table lives in the project
README; this module exports helpers for the codes that show up in
handler logic.

Active codes
~~~~~~~~~~~~

  * 200 OK / 202 Accepted / 204 No Content
  * 400 Bad Request
  * 401 Unauthorized
  * 403 Forbidden        (policy / permission refusal)
  * 404 Not Found
  * 408 Timeout          (TTL exceeded — AGTP-specific semantics)
  * 409 Conflict
  * 410 Gone             (Revoked / Deprecated agent)
  * 422 Unprocessable    (semantic refusal, including PROPOSE
                          negotiation refusal and counter-proposal)
  * 429 Rate Limited
  * 455 Scope Violation              (AGTP-specific)
  * 456 Budget Exceeded              (AGTP-specific)
  * 457 Zone Violation               (AGTP-specific)
  * 458 Counterparty Unverified      (AGTP-specific)
  * 459 Grammar Violation            (AGTP-specific; Method-Grammar
                                      header pathway, pre-dispatch
                                      lexical/reserved/stoplist refusal)
  * 500 Server Error
  * 503 Unavailable      (Suspended or temporarily down)
  * 550 Delegation Failure           (AGTP-specific)
  * 551 Authority Chain Broken       (AGTP-specific)

Reserved for AGTP expansion (do not return without a registry entry):

  * 460, 552, 553, 554, 555

Migration notes
~~~~~~~~~~~~~~~

Earlier AGTP drafts used codes that the current registry no longer
admits. Their semantics have been folded into the codes above:

  * 451 Scope Violation               -> 455 Scope Violation
  * 452 Method Not Permitted for Agent -> 403 Forbidden
                                          (error.code='method-not-permitted-for-agent')
  * 453 Zone Violation                -> 457 Zone Violation
  * 454 Grammar Violation             -> 459 Grammar Violation
                                          (Method-Grammar header pathway)
                                          The application-layer 422 +
                                          error.code='grammar-violation'
                                          form remains for in-handler
                                          grammar checks.
  * 460 Negotiation Refused           -> 422 Unprocessable
                                          (error.code='negotiation-refused')
  * 461 Counter-Proposal              -> 422 Unprocessable
                                          (body carries 'counter_proposal')
  * 462 Wildcards Refused             -> 403 Forbidden
                                          (error.code='wildcards-refused')

The helper function names below are preserved so call sites do not
churn; the wire-level status code each helper now produces is the
new value documented in its docstring.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core import wire


# -- (status_code, status_text) constants -----------------------------

# AGTP-specific.
SCOPE_VIOLATION         = (455, "Scope Violation")
BUDGET_EXCEEDED         = (456, "Budget Exceeded")
ZONE_VIOLATION          = (457, "Zone Violation")
COUNTERPARTY_UNVERIFIED = (458, "Counterparty Unverified")
GRAMMAR_VIOLATION       = (459, "Grammar Violation")
DELEGATION_FAILURE      = (550, "Delegation Failure")
AUTHORITY_CHAIN_BROKEN  = (551, "Authority Chain Broken")

# HTTP codes used as carriers for refusals that previously had
# AGTP-specific numbers. Exposed as constants so handlers can build
# responses through a single building block.
FORBIDDEN     = (403, "Forbidden")
UNPROCESSABLE = (422, "Unprocessable")


# -- Refusal reason strings used by negotiation_refused ----------------

REFUSAL_OUT_OF_SCOPE   = "out_of_scope"
REFUSAL_AMBIGUOUS      = "ambiguous"
REFUSAL_INSUFFICIENT   = "insufficient"
REFUSAL_POLICY_REFUSED = "policy_refused"
REFUSAL_NOT_IMPLEMENTED = "not_implemented"

ALL_REFUSAL_REASONS = {
    REFUSAL_OUT_OF_SCOPE,
    REFUSAL_AMBIGUOUS,
    REFUSAL_INSUFFICIENT,
    REFUSAL_POLICY_REFUSED,
    REFUSAL_NOT_IMPLEMENTED,
}


# -- Building block --------------------------------------------------


def _build(
    status: tuple, *, body: Dict[str, Any]
) -> wire.AGTPResponse:
    body_bytes = json.dumps(body, indent=2).encode("utf-8")
    return wire.AGTPResponse(
        status_code=status[0],
        status_text=status[1],
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body_bytes)),
        },
        body_bytes=body_bytes,
    )


# -- 403 Forbidden helpers ---------------------------------------------


def method_not_permitted_for_agent(
    method: str,
    agent_id: str,
    *,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Soft-deny / permission-refusal response. Wire status: **403**.

    The target agent's ``requires.methods`` does not include ``method``
    and ``wildcards`` is false. The body's ``error.code`` field carries
    the previous AGTP-specific tag so existing clients continue to
    branch correctly without depending on the obsolete 452 status.
    """
    if explanation is None:
        explanation = (
            f"This agent's permissions do not include {method}. "
            f"The principal has not authorized this method."
        )
    return _build(
        FORBIDDEN,
        body={
            "error": {
                "code": "method-not-permitted-for-agent",
                "method": method,
                "agent_id": agent_id,
                "explanation": explanation,
            }
        },
    )


# Backward-compat alias.
method_outside_need = method_not_permitted_for_agent


def wildcards_refused(
    agent_id: str,
    *,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Server policy ``wildcards_accepted=false`` rejects an agent that
    declares ``requires.wildcards: true`` invoking a non-embedded
    method. Wire status: **403**.
    """
    if explanation is None:
        explanation = (
            "Server policy.wildcards_accepted is false; "
            "agent declares wildcards: true."
        )
    return _build(
        FORBIDDEN,
        body={
            "error": {
                "code": "wildcards-refused",
                "agent_id": agent_id,
                "explanation": explanation,
            }
        },
    )


# -- 422 Unprocessable helpers (PROPOSE outcomes) ----------------------


def negotiation_refused(
    reason: str,
    explanation: str,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> wire.AGTPResponse:
    """
    PROPOSE refusal with structured reason. Wire status: **422**.
    """
    if reason not in ALL_REFUSAL_REASONS:
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
    return _build(UNPROCESSABLE, body=body)


def counter_proposal(spec: Dict[str, Any]) -> wire.AGTPResponse:
    """
    Server-issued counter-proposal naming an existing or near-existing
    method the server is willing to accept. Wire status: **422**.
    The body carries ``counter_proposal`` (the proposed MethodSpec)
    so clients can disambiguate from a plain refusal.
    """
    return _build(
        UNPROCESSABLE,
        body={"counter_proposal": spec},
    )


# -- 455 Scope Violation -----------------------------------------------


def scope_violation(
    method: str,
    missing_scopes: List[str],
    *,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Caller's scope set is missing what the method requires. Wire
    status: **455**.
    """
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


# -- 456 Budget Exceeded -----------------------------------------------


def budget_exceeded(
    method: str,
    *,
    declared_limit: Optional[Any] = None,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Method execution would exceed the Budget-Limit declared in the
    request. Wire status: **456**.
    """
    if explanation is None:
        explanation = (
            f"{method} would exceed the declared Budget-Limit"
            + (f" ({declared_limit})" if declared_limit is not None else "")
        )
    body = {
        "error": {
            "code": "budget-exceeded",
            "method": method,
            "explanation": explanation,
        }
    }
    if declared_limit is not None:
        body["error"]["declared_limit"] = declared_limit
    return _build(BUDGET_EXCEEDED, body=body)


# -- 457 Zone Violation ------------------------------------------------


def zone_violation(
    target_zone: str,
    *,
    request_zone: Optional[str] = None,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Request would route outside the AGTP-Zone-ID boundary. SEP-
    enforced. Wire status: **457**.
    """
    if explanation is None:
        explanation = (
            f"request would route outside zone {target_zone!r}"
            + (f" (request zone {request_zone!r})" if request_zone else "")
        )
    body = {
        "error": {
            "code": "zone-violation",
            "target_zone": target_zone,
            "explanation": explanation,
        }
    }
    if request_zone is not None:
        body["error"]["request_zone"] = request_zone
    return _build(ZONE_VIOLATION, body=body)


# -- 458 Counterparty Unverified ---------------------------------------


def counterparty_unverified(
    *,
    merchant_id: Optional[str] = None,
    reason: Optional[str] = None,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    PURCHASE counterparty failed merchant identity verification:
    Merchant-ID absent, Merchant-Manifest-Fingerprint mismatch, or
    merchant in non-Active lifecycle state. Wire status: **458**.
    """
    if explanation is None:
        explanation = (
            "merchant identity could not be verified"
            + (f" ({reason})" if reason else "")
        )
    body = {
        "error": {
            "code": "counterparty-unverified",
            "explanation": explanation,
        }
    }
    if merchant_id is not None:
        body["error"]["merchant_id"] = merchant_id
    if reason is not None:
        body["error"]["reason"] = reason
    return _build(COUNTERPARTY_UNVERIFIED, body=body)


# -- 459 Grammar Violation ---------------------------------------------


def grammar_violation(
    amg_code: str,
    message: str,
    *,
    method: Optional[str] = None,
    pass_name: Optional[str] = None,
    suggestion: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Wire-level rejection for the Method-Grammar header pathway.
    Returned when an unrecognized method name carrying
    ``Method-Grammar: AMG/1.0`` (or the deprecated ``AGIS/1.0``)
    fails AMG's name-targeted passes (lexical / reserved / stoplist).
    Wire status: **459**.

    The previous ``422 + error.code='grammar-violation'`` form is
    preserved for application-layer grammar refusals (e.g., a method
    is registered but the body fails AGIS validation). 459 is the
    dedicated *pre-dispatch* signal: the method name itself does not
    fit the AMG namespace, so the request never reaches a handler.
    """
    body: Dict[str, Any] = {
        "error": {
            "code": "grammar-violation",
            "amg_code": amg_code,
            "message": message,
        }
    }
    if method is not None:
        body["error"]["method"] = method
    if pass_name is not None:
        body["error"]["pass_name"] = pass_name
    if suggestion is not None:
        body["error"]["suggestion"] = suggestion
    return _build(GRAMMAR_VIOLATION, body=body)


# -- 550 Delegation Failure --------------------------------------------


def delegation_failure(
    sub_agent_id: str,
    *,
    underlying: Optional[str] = None,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    A delegated sub-agent failed to complete the requested action.
    Wire status: **550**.
    """
    if explanation is None:
        explanation = (
            f"sub-agent {sub_agent_id} did not complete the delegated action"
            + (f": {underlying}" if underlying else "")
        )
    body = {
        "error": {
            "code": "delegation-failure",
            "sub_agent_id": sub_agent_id,
            "explanation": explanation,
        }
    }
    if underlying is not None:
        body["error"]["underlying"] = underlying
    return _build(DELEGATION_FAILURE, body=body)


# -- 551 Authority Chain Broken ----------------------------------------


def authority_chain_broken(
    broken_link: str,
    *,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Delegation chain contains an unverifiable or broken identity
    link. Wire status: **551**.
    """
    if explanation is None:
        explanation = (
            f"the delegation chain has a broken link at {broken_link!r}"
        )
    return _build(
        AUTHORITY_CHAIN_BROKEN,
        body={
            "error": {
                "code": "authority-chain-broken",
                "broken_link": broken_link,
                "explanation": explanation,
            }
        },
    )


__all__ = [
    "ALL_REFUSAL_REASONS",
    "AUTHORITY_CHAIN_BROKEN",
    "BUDGET_EXCEEDED",
    "COUNTERPARTY_UNVERIFIED",
    "DELEGATION_FAILURE",
    "FORBIDDEN",
    "GRAMMAR_VIOLATION",
    "REFUSAL_AMBIGUOUS",
    "REFUSAL_INSUFFICIENT",
    "REFUSAL_NOT_IMPLEMENTED",
    "REFUSAL_OUT_OF_SCOPE",
    "REFUSAL_POLICY_REFUSED",
    "SCOPE_VIOLATION",
    "UNPROCESSABLE",
    "ZONE_VIOLATION",
    "authority_chain_broken",
    "budget_exceeded",
    "counter_proposal",
    "counterparty_unverified",
    "delegation_failure",
    "grammar_violation",
    "method_not_permitted_for_agent",
    "method_outside_need",
    "negotiation_refused",
    "scope_violation",
    "wildcards_refused",
    "zone_violation",
]
