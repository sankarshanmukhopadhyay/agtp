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
  * 261 Negotiation In Progress      (AGTP-specific; async PROPOSE)
  * 262 Authorization Required       (AGTP-specific; unified auth
                                      gate — scope, wildcards,
                                      credentials, anonymous access)
  * 263 Proposal Approved            (AGTP-specific; PROPOSE accept)
  * 400 Bad Request
  * 401 Unauthorized
  * 403 Forbidden        (policy / permission refusal — agent-doc
                          ``method-not-permitted-for-agent`` only)
  * 404 Not Found
  * 408 Timeout          (TTL exceeded — AGTP-specific semantics)
  * 409 Conflict
  * 410 Gone             (Revoked / Deprecated agent)
  * 422 Unprocessable    (semantic refusal; no longer used for PROPOSE
                          outcomes — see 263 / 463)
  * 429 Rate Limited
  * 455 Scope Violation              (AGTP-specific; reserved for
                                      *non-authority* scope refusals
                                      such as budget, rate-limit,
                                      quota. Authority issues use
                                      262.)
  * 456 Budget Exceeded              (AGTP-specific)
  * 457 Zone Violation               (AGTP-specific)
  * 458 Counterparty Unverified      (AGTP-specific)
  * 459 Method Violation             (AGTP-specific)
  * 460 Endpoint Violation           (AGTP-specific)
  * 461 RCNS Contract Available      (AGTP-specific; RCNS-3, confirm-
                                      first preview of a negotiated
                                      contract — agent re-issues with
                                      ``Synthesis-Id`` to execute)
  * 463 Proposal Rejected            (AGTP-specific; PROPOSE refuse)
  * 464 RCNS No Contract             (AGTP-specific; RCNS-3, the
                                      daemon attempted negotiation
                                      on the caller's behalf and
                                      could not deliver — distinct
                                      from 463, which is reserved
                                      for explicit PROPOSE refusals)
  * 500 Server Error
  * 503 Unavailable      (Suspended or temporarily down)
  * 550 Delegation Failure           (AGTP-specific)
  * 551 Authority Chain Broken       (AGTP-specific)

Reserved for AGTP expansion (do not return without a registry entry):

  * 552, 553, 554, 555

Migration notes
~~~~~~~~~~~~~~~

Earlier AGTP drafts used codes that the current registry no longer
admits. Their semantics have been folded into the codes above:

  * 451 Scope Violation               -> 455 Scope Violation
  * 452 Method Not Permitted for Agent -> 403 Forbidden
                                          (error.code='method-not-permitted-for-agent')
  * 453 Zone Violation                -> 457 Zone Violation
  * 454 Grammar Violation             -> 459 Method Violation
  * 461 Counter-Proposal              -> 463 Proposal Rejected
                                          (body carries 'counter_proposal'
                                          under error.counter_proposal)
  * 462 Wildcards Refused             -> 262 Authorization Required
                                          (error.type='wildcards-required')

§7 PROPOSE outcomes
~~~~~~~~~~~~~~~~~~~

Per ``agtp-api §7`` PROPOSE has its own status-code surface:

  * **263 Proposal Approved**     — synthesis accepted; body carries
                                    synthesis_id + endpoint + expires_at.
  * **463 Proposal Rejected**     — server refuses; body carries
                                    structured reason and optional
                                    counter_proposal.
  * **261 Negotiation In Progress** — proposal needs async evaluation;
                                       body carries proposal_id +
                                       polling instructions.
  * **400 Bad Request**           — body malformed (missing fields,
                                    invalid JSON, malformed schemas).
  * **262 Authorization Required** — agent's authority insufficient
                                      for the proposed action.

The legacy ``negotiation_refused`` helper (422) is retired; new code
should call ``proposal_rejected`` directly.

The helper function names below are preserved so call sites do not
churn; the wire-level status code each helper now produces is the
new value documented in its docstring.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core import wire


# -- (status_code, status_text) constants -----------------------------

# AGTP-specific (PROPOSE / negotiation surface, §7).
NEGOTIATION_IN_PROGRESS     = (261, "Negotiation In Progress")
AUTHORIZATION_REQUIRED      = (262, "Authorization Required")
PROPOSAL_APPROVED           = (263, "Proposal Approved")
PROPOSAL_REJECTED           = (463, "Proposal Rejected")

# AGTP-specific (other).
SCOPE_VIOLATION             = (455, "Scope Violation")
BUDGET_EXCEEDED             = (456, "Budget Exceeded")
ZONE_VIOLATION              = (457, "Zone Violation")
COUNTERPARTY_UNVERIFIED     = (458, "Counterparty Unverified")
METHOD_GRAMMAR_VIOLATION    = (459, "Method Violation")
ENDPOINT_GRAMMAR_VIOLATION  = (460, "Endpoint Violation")
DELEGATION_FAILURE          = (550, "Delegation Failure")
AUTHORITY_CHAIN_BROKEN      = (551, "Authority Chain Broken")

# AGTP-specific (RCNS — Runtime Contract Negotiation Substrate, RCNS-3).
# Wire reservations land in RCNS-1 so the rest of the build can target
# stable codes; the dispatcher gate that actually returns them ships
# in RCNS-3.
RCNS_CONTRACT_AVAILABLE     = (461, "RCNS Contract Available")
RCNS_NO_CONTRACT            = (464, "RCNS No Contract")

# Backward-compat alias. The previous 459 helper keyed off
# ``GRAMMAR_VIOLATION``; keep the constant exported under both names
# so call sites that imported the old form keep working through the
# transition.
GRAMMAR_VIOLATION = METHOD_GRAMMAR_VIOLATION

# HTTP codes used as carriers for refusals that previously had
# AGTP-specific numbers. Exposed as constants so handlers can build
# responses through a single building block.
BAD_REQUEST        = (400, "Bad Request")
FORBIDDEN          = (403, "Forbidden")
NOT_FOUND          = (404, "Not Found")
METHOD_NOT_ALLOWED = (405, "Method Not Allowed")
UNPROCESSABLE      = (422, "Unprocessable")


# -- §7 PROPOSE reason / issue / auth-type vocabularies ----------------

#: Reason codes carried on a 463 Proposal Rejected response.
PROPOSAL_REASON_OUT_OF_SCOPE          = "out-of-scope"
PROPOSAL_REASON_POLICY_REFUSED        = "policy-refused"
PROPOSAL_REASON_COMPOSITION_IMPOSSIBLE = "composition-impossible"
PROPOSAL_REASON_AMBIGUOUS             = "ambiguous"

ALL_PROPOSAL_REJECT_REASONS = {
    PROPOSAL_REASON_OUT_OF_SCOPE,
    PROPOSAL_REASON_POLICY_REFUSED,
    PROPOSAL_REASON_COMPOSITION_IMPOSSIBLE,
    PROPOSAL_REASON_AMBIGUOUS,
}

#: Issue codes carried on a 400 Bad Request response for PROPOSE.
BAD_REQUEST_ISSUE_INVALID_JSON           = "invalid-json"
BAD_REQUEST_ISSUE_MISSING_REQUIRED_FIELD = "missing-required-field"
BAD_REQUEST_ISSUE_MALFORMED_SEMANTIC     = "malformed-semantic-block"
BAD_REQUEST_ISSUE_MALFORMED_SCHEMA       = "malformed-schema"

ALL_BAD_REQUEST_ISSUES = {
    BAD_REQUEST_ISSUE_INVALID_JSON,
    BAD_REQUEST_ISSUE_MISSING_REQUIRED_FIELD,
    BAD_REQUEST_ISSUE_MALFORMED_SEMANTIC,
    BAD_REQUEST_ISSUE_MALFORMED_SCHEMA,
}

#: Reason codes carried on a 464 RCNS No Contract response. Distinct
#: from :data:`ALL_PROPOSAL_REJECT_REASONS` so RCNS refusals don't get
#: confused with explicit-PROPOSE refusals — they're different debug
#: stories. RCNS-3 plumbs the dispatcher to return these; RCNS-1 just
#: reserves the vocabulary so downstream code can target stable
#: strings.
RCNS_REASON_RCNS_DISABLED            = "rcns-disabled"
RCNS_REASON_TRUST_TIER_INSUFFICIENT  = "trust-tier-insufficient"
RCNS_REASON_COMPOSITION_IMPOSSIBLE   = "composition-impossible"
RCNS_REASON_SYNTHESIS_ERROR          = "synthesis-error"
RCNS_REASON_CONTRACT_NOT_YOURS       = "contract-not-yours"
RCNS_REASON_CONTRACT_REVOKED         = "contract-revoked"

ALL_RCNS_REFUSAL_REASONS = {
    RCNS_REASON_RCNS_DISABLED,
    RCNS_REASON_TRUST_TIER_INSUFFICIENT,
    RCNS_REASON_COMPOSITION_IMPOSSIBLE,
    RCNS_REASON_SYNTHESIS_ERROR,
    RCNS_REASON_CONTRACT_NOT_YOURS,
    RCNS_REASON_CONTRACT_REVOKED,
}

#: Type codes carried on a 262 Authorization Required response.
AUTH_TYPE_SCOPE_REQUIRED              = "scope-required"
AUTH_TYPE_WILDCARDS_REQUIRED          = "wildcards-required"
AUTH_TYPE_CREDENTIALS_MISSING         = "credentials-missing"
AUTH_TYPE_ANONYMOUS_DISCOVERY_DISABLED = "anonymous-discovery-disabled"

ALL_AUTH_TYPES = {
    AUTH_TYPE_SCOPE_REQUIRED,
    AUTH_TYPE_WILDCARDS_REQUIRED,
    AUTH_TYPE_CREDENTIALS_MISSING,
    AUTH_TYPE_ANONYMOUS_DISCOVERY_DISABLED,
}


# -- Pre-§7 refusal reason strings (legacy ``negotiation_refused``) ----
#
# The 422 ``negotiation_refused`` flow has been retired in favor of the
# 463 ``proposal_rejected`` response (§7). These constants stay
# exported for any external caller that still imports them; new code
# should use the ``PROPOSAL_REASON_*`` set above.

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


# -- 262 Authorization Required (unified §7 auth gate) ----------------


def authorization_required(
    *,
    type: str,
    explanation: str,
    details: Optional[Dict[str, Any]] = None,
) -> wire.AGTPResponse:
    """
    Wire-level "the agent's authority is insufficient for this
    operation" response. Wire status: **262 Authorization Required**.

    ``type`` is one of :data:`ALL_AUTH_TYPES`:

      * ``scope-required``               — endpoint required scopes
                                           the agent has not declared.
      * ``wildcards-required``           — agent declares
                                           ``wildcards: true`` and the
                                           server's policy refuses
                                           wildcards.
      * ``credentials-missing``          — the request lacks
                                           credentials the server
                                           requires for the operation.
      * ``anonymous-discovery-disabled`` — server refuses the manifest
                                           fetch without credentials.

    ``details`` carries type-specific structured information:
    ``missing_scopes`` for ``scope-required``, etc. The dispatcher
    surfaces this code wherever authority-related refusal happens —
    pre-§7 servers spread these across 403 / 455 / 462 with code
    fragments; §7 consolidates them under a single status.
    """
    if type not in ALL_AUTH_TYPES:
        raise ValueError(
            f"authorization_required: unknown type {type!r} "
            f"(expected one of {sorted(ALL_AUTH_TYPES)})"
        )
    body: Dict[str, Any] = {
        "error": {
            "code": "authorization-required",
            "type": type,
            "explanation": explanation,
        }
    }
    if details:
        body["error"]["details"] = dict(details)
    return _build(AUTHORIZATION_REQUIRED, body=body)


def insufficient_scope(
    method: str,
    path: str,
    missing_scopes: List[str],
    *,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Endpoint required scope(s) the calling agent has not declared.
    Wire status: **262 Authorization Required**
    (``error.type = "scope-required"``).

    Pre-§7 servers returned 403 with ``error.code = "insufficient_scope"``
    for this case; §7 unifies authority refusals under 262. The
    helper's name and signature stay so existing call sites don't
    churn.
    """
    if explanation is None:
        explanation = (
            f"{method} {path} requires scope(s) the agent has not "
            f"declared: {', '.join(missing_scopes)}"
        )
    return authorization_required(
        type=AUTH_TYPE_SCOPE_REQUIRED,
        explanation=explanation,
        details={
            "method": method,
            "path": path,
            "missing_scopes": sorted(missing_scopes),
        },
    )


def wildcards_refused(
    agent_id: str,
    *,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Server policy ``wildcards_accepted=false`` rejects an agent that
    declares ``requires.wildcards: true`` invoking a non-embedded
    method. Wire status: **262 Authorization Required**
    (``error.type = "wildcards-required"``).

    Pre-§7 servers returned 403 with ``error.code = "wildcards-refused"``
    (and pre-462 drafts returned 462). §7 unifies authority refusals
    under 262.
    """
    if explanation is None:
        explanation = (
            "Server policies.wildcards_accepted is false; "
            "agent declares wildcards: true."
        )
    return authorization_required(
        type=AUTH_TYPE_WILDCARDS_REQUIRED,
        explanation=explanation,
        details={"agent_id": agent_id},
    )


def credentials_missing(
    *,
    explanation: str,
    details: Optional[Dict[str, Any]] = None,
) -> wire.AGTPResponse:
    """
    Authority-related refusal: the request lacks credentials the
    server requires. Wire status: **262 Authorization Required**
    (``error.type = "credentials-missing"``).
    """
    return authorization_required(
        type=AUTH_TYPE_CREDENTIALS_MISSING,
        explanation=explanation,
        details=details,
    )


def anonymous_discovery_disabled(
    *,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    The server's ``policies.anonymous_discovery`` is false; the
    target-less DISCOVER refuses without credentials. Wire status:
    **262 Authorization Required**
    (``error.type = "anonymous-discovery-disabled"``).
    """
    if explanation is None:
        explanation = (
            "Server policies.anonymous_discovery is false; "
            "manifest fetch requires credentials."
        )
    return authorization_required(
        type=AUTH_TYPE_ANONYMOUS_DISCOVERY_DISABLED,
        explanation=explanation,
    )


# -- §7 PROPOSE response helpers (263 / 463 / 261) --------------------


def proposal_approved(
    *,
    synthesis_id: str,
    endpoint: Optional[Dict[str, Any]] = None,
    persistent: bool = False,
    expires_at: Optional[str] = None,
    granted_duration: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> wire.AGTPResponse:
    """
    PROPOSE accept response. Wire status: **263 Proposal Approved**.

    Body:

      * ``synthesis_id`` — identifier the agent uses to invoke the
        composed endpoint via the ``Synthesis-Id`` header.
      * ``endpoint`` — the instantiated endpoint contract (optional;
        the agent may already have it from the proposal).
      * ``persistent`` — whether this synthesis is persistent
        (survives the agent's session up to ``expires_at``).
      * ``expires_at`` — ISO 8601 UTC timestamp after which the
        synthesis_id stops resolving.
      * ``granted_duration`` — the duration string the server
        granted, distinct from the agent's request when capped by
        ``policies.synthesis.persistent_max_duration``.

    ``extra`` lets the PROPOSE handler attach additional fields
    (e.g., a plan dict for multi-step compositions).
    """
    body: Dict[str, Any] = {"synthesis_id": synthesis_id}
    if endpoint is not None:
        body["endpoint"] = endpoint
    body["persistent"] = bool(persistent)
    if expires_at is not None:
        body["expires_at"] = expires_at
    if granted_duration is not None:
        body["granted_duration"] = granted_duration
    if extra:
        body.update(extra)
    return _build(PROPOSAL_APPROVED, body=body)


def proposal_rejected(
    *,
    reason: str,
    explanation: str,
    counter_proposal: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> wire.AGTPResponse:
    """
    PROPOSE refuse response. Wire status: **463 Proposal Rejected**.

    Body's ``error`` carries:

      * ``code``: always ``"proposal-rejected"``.
      * ``reason``: one of :data:`ALL_PROPOSAL_REJECT_REASONS`
        (``out-of-scope`` / ``policy-refused`` /
        ``composition-impossible`` / ``ambiguous``).
      * ``explanation``: operator-facing prose.
      * ``counter_proposal``: optional — the server's suggested
        alternative endpoint contract.

    ``extra`` lets callers attach additional fields (e.g.,
    ``agent_id`` for audit correlation).
    """
    if reason not in ALL_PROPOSAL_REJECT_REASONS:
        raise ValueError(
            f"proposal_rejected: unknown reason {reason!r} "
            f"(expected one of {sorted(ALL_PROPOSAL_REJECT_REASONS)})"
        )
    body: Dict[str, Any] = {
        "error": {
            "code": "proposal-rejected",
            "reason": reason,
            "explanation": explanation,
        }
    }
    if counter_proposal is not None:
        body["error"]["counter_proposal"] = counter_proposal
    if extra:
        body["error"].update(extra)
    return _build(PROPOSAL_REJECTED, body=body)


def negotiation_in_progress(
    *,
    proposal_id: str,
    polling_path: str,
    explanation: Optional[str] = None,
    evaluation_started_at: Optional[str] = None,
    max_evaluation_duration: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    PROPOSE accepted for async evaluation. Wire status: **261
    Negotiation In Progress**.

    Body carries the ``proposal_id`` and instructions for polling
    via ``QUERY {polling_path}`` — typically
    ``QUERY /proposals/{proposal_id}``. Subsequent polls return:

      * **261** while the evaluation is in progress.
      * **263** when the evaluation accepts (with the synthesis).
      * **463** when the evaluation refuses.

    Servers opt into async evaluation via
    ``policies.synthesis.async_evaluation_enabled = true``. Without
    that opt-in every PROPOSE returns 263 or 463 synchronously.
    """
    if explanation is None:
        explanation = (
            f"proposal {proposal_id} is under evaluation; poll "
            f"QUERY {polling_path} for status."
        )
    body: Dict[str, Any] = {
        "proposal_id": proposal_id,
        "polling_path": polling_path,
        "explanation": explanation,
    }
    if evaluation_started_at is not None:
        body["evaluation_started_at"] = evaluation_started_at
    if max_evaluation_duration is not None:
        body["max_evaluation_duration"] = max_evaluation_duration
    return _build(NEGOTIATION_IN_PROGRESS, body=body)


def bad_request_for_propose(
    *,
    issue: str,
    explanation: str,
    details: Optional[Dict[str, Any]] = None,
) -> wire.AGTPResponse:
    """
    Wire-level "the PROPOSE body is malformed" response. Wire status:
    **400 Bad Request**.

    ``issue`` is one of :data:`ALL_BAD_REQUEST_ISSUES`:

      * ``invalid-json``            — body could not be parsed as JSON.
      * ``missing-required-field``  — a required field is absent.
      * ``malformed-semantic-block`` — the semantic block fails §6
                                        validation.
      * ``malformed-schema``        — an embedded JSON Schema is
                                       structurally invalid.

    The pre-§7 dispatcher returned 422 ``negotiation-refused`` with
    ``reason="insufficient"`` for these cases; §7 separates body
    well-formedness (400) from authority/composition refusal (463).
    """
    if issue not in ALL_BAD_REQUEST_ISSUES:
        raise ValueError(
            f"bad_request_for_propose: unknown issue {issue!r} "
            f"(expected one of {sorted(ALL_BAD_REQUEST_ISSUES)})"
        )
    body: Dict[str, Any] = {
        "error": {
            "code": "bad-request",
            "issue": issue,
            "explanation": explanation,
        }
    }
    if details:
        body["error"]["details"] = dict(details)
    return _build(BAD_REQUEST, body=body)


# -- RCNS helpers (461 / 464) ------------------------------------------
#
# RCNS-1 reserves the wire codes and helpers; RCNS-3 wires the
# dispatcher gate that returns them. Keeping the helpers here, beside
# the existing PROPOSE family, makes the shared vocabulary obvious
# and lets downstream phases target stable signatures.


def rcns_contract_available(
    *,
    contract: Dict[str, Any],
    proposed_synthesis_id: str,
    expires_at: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> wire.AGTPResponse:
    """
    Confirm-first RCNS preview. Wire status: **461 RCNS Contract
    Available**.

    Returned by the RCNS dispatcher gate (RCNS-3) when an agent
    requesting ``Allow-RCNS: true`` hits an unregistered (method,
    path) and the synthesis runtime produced a plan. The caller
    inspects ``contract`` and re-issues the original request with
    ``Synthesis-Id: <proposed_synthesis_id>`` to execute.

    Body shape::

        {
          "contract": {
            "method": "...",
            "path": "...",
            "input_schema": {...},
            "output_schema": {...},
            "plan_summary": "...",
            ...
          },
          "proposed_synthesis_id": "syn-...",
          "expires_at": "2026-05-23T05:00:00Z"
        }

    ``contract`` carries the full proposed contract shape; the agent
    is expected to inspect it before re-issuing. ``expires_at`` is
    optional but typically populated so the caller knows how long the
    proposed binding is honored before the daemon evicts it.
    """
    body: Dict[str, Any] = {
        "contract": dict(contract),
        "proposed_synthesis_id": proposed_synthesis_id,
    }
    if expires_at is not None:
        body["expires_at"] = expires_at
    if extra:
        body.update(extra)
    return _build(RCNS_CONTRACT_AVAILABLE, body=body)


def rcns_no_contract(
    *,
    reason: str,
    explanation: str,
    method: Optional[str] = None,
    path: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> wire.AGTPResponse:
    """
    RCNS refusal response. Wire status: **464 RCNS No Contract**.

    Returned when the RCNS dispatcher gate (RCNS-3) attempted
    negotiation on the caller's behalf and could not deliver. Distinct
    from 463 (which is reserved for explicit-PROPOSE refusals): a 464
    is a *negotiation outcome*, not a PROPOSE outcome.

    ``reason`` is one of :data:`ALL_RCNS_REFUSAL_REASONS`:

      * ``rcns-disabled``               — server policy
                                           ``[policies.rcns].enabled``
                                           is false. The gate refused
                                           before any synthesis ran.
      * ``trust-tier-insufficient``     — agent's resolved trust tier
                                           does not meet
                                           ``min_trust_tier``. Refused
                                           before synthesis.
      * ``composition-impossible``      — synthesis runtime tried every
                                           policy and none returned a
                                           plan. Honest negative.
      * ``synthesis-error``             — synthesis runtime raised or
                                           returned a malformed plan;
                                           operator should consult
                                           the high-fidelity audit
                                           record.
      * ``contract-not-yours``          — caller presented a
                                           synthesis_id whose
                                           originating Agent-ID does
                                           not match the caller.
      * ``contract-revoked``            — operator revoked the
                                           contract since synthesis;
                                           caller should re-negotiate.

    ``method`` / ``path`` are included when known so the caller can
    correlate the refusal with the request they made.
    """
    if reason not in ALL_RCNS_REFUSAL_REASONS:
        raise ValueError(
            f"rcns_no_contract: unknown reason {reason!r} "
            f"(expected one of {sorted(ALL_RCNS_REFUSAL_REASONS)})"
        )
    body: Dict[str, Any] = {
        "error": {
            "code": "rcns-no-contract",
            "reason": reason,
            "explanation": explanation,
        }
    }
    if method is not None:
        body["error"]["method"] = str(method).upper()
    if path is not None:
        body["error"]["path"] = path
    if details:
        body["error"]["details"] = dict(details)
    return _build(RCNS_NO_CONTRACT, body=body)


# -- Pre-§7 PROPOSE helpers (422 / counter_proposal) ------------------
#
# Retained for transitional callers. New code should use the §7
# helpers above (``proposal_approved`` / ``proposal_rejected`` /
# ``negotiation_in_progress``).


def negotiation_refused(
    reason: str,
    explanation: str,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> wire.AGTPResponse:
    """
    [Deprecated, pre-§7] PROPOSE refusal with structured reason.
    Wire status: **422**.

    Use :func:`proposal_rejected` (463) instead. This helper stays
    available for back-compat tests and external callers; the
    in-tree dispatcher routes through ``proposal_rejected``.

    Special ``reason`` strings (e.g. ``"synthesis-disabled"``) bypass
    the legacy ``ALL_REFUSAL_REASONS`` check.
    """
    if reason in ALL_REFUSAL_REASONS:
        body_reason: Any = reason
    else:
        body_reason = reason
    body = {
        "error": {
            "code": "negotiation-refused",
            "reason": body_reason,
            "explanation": explanation,
        }
    }
    if extra:
        body["error"].update(extra)
    return _build(UNPROCESSABLE, body=body)


def counter_proposal(spec: Dict[str, Any]) -> wire.AGTPResponse:
    """
    [Deprecated, pre-§7] Server-issued counter-proposal naming an
    existing or near-existing method the server is willing to accept.
    Wire status: **422**.

    Use :func:`proposal_rejected` with the ``counter_proposal``
    keyword instead, which returns 463 with the suggestion attached
    to the error body.
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


# -- 459 Method Violation / 460 Endpoint Violation ---------------------


def method_grammar_violation(
    method: str,
    *,
    suggestions: Optional[List[str]] = None,
    message: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Wire-level rejection for an unrecognized method name. Returned
    by the dispatcher when a method is not in the canonical AGTP
    method list (``core/methods.json``). Wire status: **459 Method
    Violation**.

    ``suggestions`` (typically the result of
    :func:`core.methods.find_close_matches`) is included in the body
    so callers can correct typos cheaply.
    """
    upper = method.upper()
    body: Dict[str, Any] = {
        "error": {
            "code": "method-violation",
            "message": message
            or f"{upper!r} is not a recognized AGTP verb.",
            "method": upper,
        }
    }
    if suggestions:
        body["error"]["suggestions"] = list(suggestions)
    return _build(METHOD_GRAMMAR_VIOLATION, body=body)


def endpoint_grammar_violation(
    path: str,
    reason: str,
    *,
    segment: Optional[str] = None,
    code: str = "endpoint-violation",
) -> wire.AGTPResponse:
    """
    Wire-level rejection for a path that violates AGTP path grammar.
    Returned by the dispatcher when the path does not begin with
    ``/``, has a trailing slash, or contains a recognized AGTP verb
    in any of its segments. Wire status: **460 Endpoint Violation**.
    """
    body: Dict[str, Any] = {
        "error": {
            "code": code,
            "message": reason,
            "path": path,
        }
    }
    if segment is not None:
        body["error"]["segment"] = segment
    return _build(ENDPOINT_GRAMMAR_VIOLATION, body=body)


# -- 404 Not Found ------------------------------------------------------


def not_found(
    method: str,
    path: str,
    *,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Wire-level "no endpoint registered at this path" response. Wire
    status: **404 Not Found**.

    Distinct from 405: 404 means the path itself isn't registered to
    any method on this server; 405 means at least one method is
    registered at the path but not the one requested.
    """
    if explanation is None:
        explanation = (
            f"no endpoint registered at path {path!r} on this server"
        )
    return _build(
        NOT_FOUND,
        body={
            "error": {
                "code": "endpoint-not-found",
                "method": method,
                "path": path,
                "message": explanation,
            }
        },
    )


# -- 405 Method Not Allowed --------------------------------------------


def method_not_allowed(
    method: str,
    path: str,
    *,
    allowed_methods_for_path: Optional[List[str]] = None,
    explanation: Optional[str] = None,
) -> wire.AGTPResponse:
    """
    Wire-level "this path exists but is not bound to that method"
    response. Wire status: **405 Method Not Allowed**.

    Always populates ``error.allowed_methods_for_path`` with the
    other methods registered at the same path so the caller can
    re-issue against an admissible verb without a separate DISCOVER
    round-trip.
    """
    allowed = sorted(allowed_methods_for_path or [])
    if explanation is None:
        if allowed:
            explanation = (
                f"{method} is not registered for {path}; allowed: "
                f"{', '.join(allowed)}"
            )
        else:
            explanation = f"{method} is not registered for {path}"
    return _build(
        METHOD_NOT_ALLOWED,
        body={
            "error": {
                "code": "method-not-allowed",
                "method": method,
                "path": path,
                "message": explanation,
                "allowed_methods_for_path": allowed,
            }
        },
    )


def method_not_implemented(
    method: str,
    *,
    known_methods: Optional[List[str]] = None,
) -> wire.AGTPResponse:
    """
    Wire-level "the verb is in the AGTP catalog but no handler is
    registered on this server" response. Wire status: **405**.

    The path-aware sibling helper :func:`method_not_allowed` is
    preferred for endpoint-registry misses; this helper is for the
    method-only fallback path (embedded methods + ``REGISTRY``).
    """
    body: Dict[str, Any] = {
        "error": {
            "code": "method-not-implemented",
            "method": method,
            "explanation": (
                f"{method} is in the AGTP catalog but no handler is "
                f"registered on this server"
            ),
        }
    }
    if known_methods is not None:
        body["error"]["known_methods"] = sorted(known_methods)
    return _build(METHOD_NOT_ALLOWED, body=body)


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
    # Status (code, text) constants.
    "AUTHORITY_CHAIN_BROKEN",
    "AUTHORIZATION_REQUIRED",
    "BAD_REQUEST",
    "BUDGET_EXCEEDED",
    "COUNTERPARTY_UNVERIFIED",
    "DELEGATION_FAILURE",
    "ENDPOINT_GRAMMAR_VIOLATION",
    "FORBIDDEN",
    "GRAMMAR_VIOLATION",
    "METHOD_GRAMMAR_VIOLATION",
    "METHOD_NOT_ALLOWED",
    "NEGOTIATION_IN_PROGRESS",
    "NOT_FOUND",
    "PROPOSAL_APPROVED",
    "PROPOSAL_REJECTED",
    "RCNS_CONTRACT_AVAILABLE",
    "RCNS_NO_CONTRACT",
    "SCOPE_VIOLATION",
    "UNPROCESSABLE",
    "ZONE_VIOLATION",
    # §7 PROPOSE vocabularies.
    "ALL_AUTH_TYPES",
    "ALL_BAD_REQUEST_ISSUES",
    "ALL_PROPOSAL_REJECT_REASONS",
    "AUTH_TYPE_ANONYMOUS_DISCOVERY_DISABLED",
    "AUTH_TYPE_CREDENTIALS_MISSING",
    "AUTH_TYPE_SCOPE_REQUIRED",
    "AUTH_TYPE_WILDCARDS_REQUIRED",
    "BAD_REQUEST_ISSUE_INVALID_JSON",
    "BAD_REQUEST_ISSUE_MALFORMED_SCHEMA",
    "BAD_REQUEST_ISSUE_MALFORMED_SEMANTIC",
    "BAD_REQUEST_ISSUE_MISSING_REQUIRED_FIELD",
    "PROPOSAL_REASON_AMBIGUOUS",
    "PROPOSAL_REASON_COMPOSITION_IMPOSSIBLE",
    "PROPOSAL_REASON_OUT_OF_SCOPE",
    "PROPOSAL_REASON_POLICY_REFUSED",
    # RCNS vocabulary (RCNS-1 reservation; RCNS-3 wires the dispatcher).
    "ALL_RCNS_REFUSAL_REASONS",
    "RCNS_REASON_COMPOSITION_IMPOSSIBLE",
    "RCNS_REASON_CONTRACT_NOT_YOURS",
    "RCNS_REASON_CONTRACT_REVOKED",
    "RCNS_REASON_RCNS_DISABLED",
    "RCNS_REASON_SYNTHESIS_ERROR",
    "RCNS_REASON_TRUST_TIER_INSUFFICIENT",
    # Pre-§7 refusal reason strings (retained for back-compat).
    "ALL_REFUSAL_REASONS",
    "REFUSAL_AMBIGUOUS",
    "REFUSAL_INSUFFICIENT",
    "REFUSAL_NOT_IMPLEMENTED",
    "REFUSAL_OUT_OF_SCOPE",
    "REFUSAL_POLICY_REFUSED",
    # Helpers.
    "anonymous_discovery_disabled",
    "authority_chain_broken",
    "authorization_required",
    "bad_request_for_propose",
    "budget_exceeded",
    "counter_proposal",
    "counterparty_unverified",
    "credentials_missing",
    "delegation_failure",
    "endpoint_grammar_violation",
    "insufficient_scope",
    "method_grammar_violation",
    "method_not_allowed",
    "method_not_implemented",
    "method_not_permitted_for_agent",
    "method_outside_need",
    "negotiation_in_progress",
    "negotiation_refused",
    "not_found",
    "proposal_approved",
    "proposal_rejected",
    "rcns_contract_available",
    "rcns_no_contract",
    "scope_violation",
    "wildcards_refused",
    "zone_violation",
]
