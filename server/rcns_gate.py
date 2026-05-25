"""
RCNS dispatcher gate — Runtime Contract Negotiation Substrate.

Sits between the endpoint-registry lookup miss and the 404 response
in :func:`server.methods._dispatch_inner`. When all four locks are
open the gate runs the synthesis runtime against the unregistered
``(method, path)`` and either:

  * returns **461 RCNS Contract Available** with a proposed
    contract preview (confirm-first; the caller re-issues with
    ``Synthesis-Id`` to execute), or
  * executes the synthesis inline and returns its response with a
    ``Contract-Synthesized: <id>`` response header (optimistic
    mode; one round-trip), or
  * returns **464 RCNS No Contract** with a structured refusal
    reason.

When any lock is closed the gate returns ``None`` and the
dispatcher falls through to the ordinary 404 — RCNS is opt-in by
design.

Four-lock evaluation
~~~~~~~~~~~~~~~~~~~~

Evaluated cheapest-first so the gate fails fast before any
synthesis cost:

  1. ``[policies.rcns].enabled`` — server policy lock. If false,
     the gate returns ``None`` (silent fall-through to 404; the
     server doesn't advertise RCNS at all).
  2. ``Allow-RCNS`` header present and well-formed — caller intent
     lock. The header value is ``"true"`` (confirm-first) or
     ``"optimistic"`` (execute inline). Missing or unknown values
     fall through to 404.
  3. ``rcns:negotiate`` scope — agent capability lock. Missing
     scope returns **262 Authorization Required** with
     ``type = "scope-required"``.
  4. ``trust_tier`` meets ``min_trust_tier`` — trust posture lock.
     Below threshold returns **464** with
     ``reason = "trust-tier-insufficient"``.

Abuse mitigations
~~~~~~~~~~~~~~~~~

  * Per-agent rolling rate limit
    (:attr:`RcnsConfig.max_negotiations_per_minute`). Exceeded
    returns **429** with ``error.scope = "rcns"``.
  * ``RCNS-Idempotency-Key`` header — same key + same agent within
    :attr:`RcnsConfig.idempotency_window_seconds` returns the
    previously-negotiated synthesis_id without re-running
    composition. Stops retry storms from spawning duplicates.
  * No recursive RCNS: requests carrying ``Synthesis-Id`` skip the
    gate (they're already inside a contract). Steps inside a
    synthesis dispatch the same way.

Contract scoping
~~~~~~~~~~~~~~~~

Every synthesis_id produced by RCNS carries the originating
Agent-ID. A request from a different Agent-ID presenting that
synthesis_id is refused at the dispatcher with **464**
``contract-not-yours``. The check lives in
:func:`server.methods._dispatch_inner` so it applies to both
RCNS-spawned and PROPOSE-spawned syntheses uniformly.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from core import status as _status
from core import wire

if TYPE_CHECKING:
    from core.identity import AgentDocument
    from server.config import RcnsConfig


# ---------------------------------------------------------------------------
# Module state — rate limiter + idempotency cache.
# ---------------------------------------------------------------------------


_LOCK = threading.Lock()

#: Per-agent rolling window of negotiation timestamps.
#: ``agent_id -> [unix_ts, ...]``. Bounded by the rate-limit window.
_NEGOTIATION_LOG: Dict[str, List[float]] = {}

#: ``(agent_id, idempotency_key) -> (synthesis_id, expires_at_unix_ts)``.
#: Lazy eviction at lookup time.
_IDEMPOTENCY_CACHE: Dict[Tuple[str, str], Tuple[str, float]] = {}

#: Ring buffer of recent failed negotiation diagnostics (RCNS-4).
#: ``INSPECT target=rcns-attempt {attempt_id}`` looks up entries here
#: when an operator is diagnosing a 464 in the field. Bounded so the
#: buffer doesn't grow without bound — older entries fall off the
#: end. Entries are operator-facing and intentionally exclude any
#: caller-supplied data beyond the method / path that fired.
_RCNS_ATTEMPT_BUFFER_LIMIT: int = 200
_RCNS_ATTEMPTS: Dict[str, Dict[str, Any]] = {}
_RCNS_ATTEMPT_ORDER: List[str] = []


def _now() -> float:
    return time.time()


def _record_negotiation(agent_id: str, *, now: float) -> None:
    """Append the current timestamp to the agent's negotiation log
    after trimming entries older than the rolling window."""
    with _LOCK:
        log = _NEGOTIATION_LOG.setdefault(agent_id, [])
        cutoff = now - 60.0
        # Trim and append. ``log[:] = ...`` mutates in place so the
        # dict's reference doesn't change.
        log[:] = [t for t in log if t >= cutoff]
        log.append(now)


def _rate_limited(
    agent_id: str, limit: int, *, now: Optional[float] = None,
) -> bool:
    """True iff the agent has hit or exceeded the per-minute
    negotiation limit. Read-only — the caller records the
    negotiation only when proceeding."""
    if limit <= 0:
        return False
    now = _now() if now is None else now
    cutoff = now - 60.0
    with _LOCK:
        log = _NEGOTIATION_LOG.get(agent_id, [])
        recent = sum(1 for t in log if t >= cutoff)
    return recent >= limit


def _idempotent_lookup(
    agent_id: str, key: str, *, window_seconds: int,
) -> Optional[str]:
    """Return a cached synthesis_id for ``(agent_id, key)`` if the
    cached entry is still within the window. Lazy eviction."""
    if not key or window_seconds <= 0:
        return None
    now = _now()
    with _LOCK:
        entry = _IDEMPOTENCY_CACHE.get((agent_id, key))
        if entry is None:
            return None
        sid, expires_at = entry
        if now >= expires_at:
            _IDEMPOTENCY_CACHE.pop((agent_id, key), None)
            return None
        return sid


def _idempotent_record(
    agent_id: str, key: str, synthesis_id: str, *, window_seconds: int,
) -> None:
    if not key or window_seconds <= 0:
        return
    with _LOCK:
        _IDEMPOTENCY_CACHE[(agent_id, key)] = (
            synthesis_id, _now() + window_seconds,
        )


def reset_state_for_tests() -> None:
    """Clear the rate-limit log, idempotency cache, and attempt
    diagnostics. Tests reach for this between cases; production
    never calls it."""
    with _LOCK:
        _NEGOTIATION_LOG.clear()
        _IDEMPOTENCY_CACHE.clear()
        _RCNS_ATTEMPTS.clear()
        _RCNS_ATTEMPT_ORDER.clear()


def _record_attempt(
    *,
    agent_id: str,
    method: str,
    path: str,
    reason: str,
    explanation: str,
    details: Optional[Dict[str, Any]] = None,
) -> str:
    """Append a failed-attempt diagnostic to the ring buffer and
    return its id. Eviction is FIFO when the buffer fills."""
    attempt_id = f"rcns-{secrets.token_urlsafe(9)}"
    record = {
        "attempt_id": attempt_id,
        "agent_id": agent_id,
        "method": method,
        "path": path,
        "reason": reason,
        "explanation": explanation,
        "details": dict(details or {}),
        "at": _now(),
    }
    with _LOCK:
        _RCNS_ATTEMPTS[attempt_id] = record
        _RCNS_ATTEMPT_ORDER.append(attempt_id)
        while len(_RCNS_ATTEMPT_ORDER) > _RCNS_ATTEMPT_BUFFER_LIMIT:
            evict = _RCNS_ATTEMPT_ORDER.pop(0)
            _RCNS_ATTEMPTS.pop(evict, None)
    return attempt_id


def lookup_attempt(attempt_id: str) -> Optional[Dict[str, Any]]:
    """Public accessor for the failed-attempt diagnostics, used by
    ``INSPECT target=rcns-attempt``. Returns ``None`` when the
    attempt has been evicted from the buffer or never existed."""
    with _LOCK:
        record = _RCNS_ATTEMPTS.get(attempt_id)
        return dict(record) if record else None


# ---------------------------------------------------------------------------
# Contract hashing.
# ---------------------------------------------------------------------------


def contract_hash(contract: Dict[str, Any]) -> str:
    """sha256 hex digest of the canonical-JSON serialization of a
    contract dict.

    Two contracts with identical body shape share a hash even if
    they're held under different synthesis_ids — the chain
    inspector uses this to group invocations by contract identity
    across re-negotiations and across agents.
    """
    canonical = json.dumps(
        contract, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# ---------------------------------------------------------------------------
# Header parsing.
# ---------------------------------------------------------------------------


_ALLOW_RCNS_VALUES = {"true", "optimistic"}


def parse_allow_rcns(request: wire.AGTPRequest) -> Optional[str]:
    """Return the normalized ``Allow-RCNS`` header value or ``None``.

    ``true`` and ``optimistic`` are the two recognized forms;
    anything else (including ``false``) is treated as "header not
    set" so a misspelling falls through to 404 rather than
    accidentally triggering negotiation.
    """
    raw = wire.header(request, "Allow-RCNS")
    if not raw:
        return None
    val = raw.strip().lower()
    if val not in _ALLOW_RCNS_VALUES:
        return None
    return val


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------


def try_rcns(
    request: wire.AGTPRequest,
    server_state: Any,
    agent_doc: "AgentDocument",
    *,
    method: str,
    path: str,
) -> Optional[wire.AGTPResponse]:
    """Return an RCNS response, or ``None`` to fall through to 404.

    Called by :func:`server.methods._dispatch_inner` after the
    endpoint registry confirms there's no binding for the
    ``(method, path)`` pair.

    The four-lock check runs cheapest-first; refusals carry
    structured ``reason`` values from :data:`core.status.ALL_RCNS_REFUSAL_REASONS`.
    """
    config = getattr(server_state, "config", None)
    rcns_cfg: Optional["RcnsConfig"] = getattr(config, "rcns", None) if config else None

    # Lock 1 — server policy. Silent fall-through to 404 when the
    # server hasn't enabled RCNS; the server doesn't advertise the
    # mechanism at all in that posture.
    if rcns_cfg is None or not rcns_cfg.enabled:
        return None

    # Lock 2 — caller intent. Header absent = silent fall-through.
    mode = parse_allow_rcns(request)
    if mode is None:
        return None

    # Lock 3 — agent capability. Missing scope is a structured 262
    # refusal so the caller learns what to add to its scope claim.
    declared_scopes = set(
        getattr(getattr(agent_doc, "requires", None), "scopes", []) or []
    )
    if "rcns:negotiate" not in declared_scopes:
        return _status.authorization_required(
            type=_status.AUTH_TYPE_SCOPE_REQUIRED,
            explanation=(
                "RCNS negotiation requires the 'rcns:negotiate' scope. "
                "Declare it in the agent's requires.scopes to opt in."
            ),
            details={
                "code": "rcns-scope-required",
                "missing_scopes": ["rcns:negotiate"],
                "method": method,
                "path": path,
            },
        )

    # Lock 4 — trust posture. ``min_trust_tier`` semantics: lower
    # numbers are stronger trust, the request must be at least as
    # strong (numerically less than or equal).
    agent_tier = getattr(agent_doc, "trust_tier", None)
    try:
        agent_tier_int = int(agent_tier) if agent_tier is not None else 3
    except (TypeError, ValueError):
        agent_tier_int = 3
    if agent_tier_int > rcns_cfg.min_trust_tier:
        attempt_id = _record_attempt(
            agent_id=agent_doc.agent_id, method=method, path=path,
            reason=_status.RCNS_REASON_TRUST_TIER_INSUFFICIENT,
            explanation="agent trust tier below configured minimum",
            details={
                "min_trust_tier": rcns_cfg.min_trust_tier,
                "agent_trust_tier": agent_tier_int,
            },
        )
        resp = _status.rcns_no_contract(
            reason=_status.RCNS_REASON_TRUST_TIER_INSUFFICIENT,
            explanation=(
                f"RCNS requires trust_tier <= {rcns_cfg.min_trust_tier}; "
                f"agent's resolved trust_tier is {agent_tier_int}"
            ),
            method=method, path=path,
            details={
                "min_trust_tier": rcns_cfg.min_trust_tier,
                "agent_trust_tier": agent_tier_int,
                "attempt_id": attempt_id,
            },
        )
        # RCNS-4: surface the attempt id as a response header too so
        # operators can copy/paste into INSPECT target=rcns-attempt.
        resp.headers["RCNS-Attempt-Id"] = attempt_id
        return resp

    # Rate limit — applies once all four locks are open. Returns
    # 429 with scope="rcns" so the caller can distinguish negotiation
    # throttling from ordinary request throttling.
    if _rate_limited(
        agent_doc.agent_id,
        rcns_cfg.max_negotiations_per_minute,
    ):
        body = {
            "error": {
                "code": "rate-limited",
                "scope": "rcns",
                "explanation": (
                    f"agent exceeded {rcns_cfg.max_negotiations_per_minute} "
                    f"RCNS negotiations per minute"
                ),
                "method": method,
                "path": path,
            },
        }
        body_bytes = json.dumps(body, indent=2).encode("utf-8")
        return wire.AGTPResponse(
            status_code=429,
            status_text="Too Many Requests",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body_bytes)),
            },
            body_bytes=body_bytes,
        )

    # Idempotency key — return the cached synthesis_id for repeated
    # negotiations with the same key within the window.
    idem_key = wire.read_idempotency_key(request) or ""
    cached_sid: Optional[str] = None
    if idem_key:
        cached_sid = _idempotent_lookup(
            agent_doc.agent_id, idem_key,
            window_seconds=rcns_cfg.idempotency_window_seconds,
        )

    runtime = getattr(server_state, "synthesis_runtime", None)
    if runtime is None:
        attempt_id = _record_attempt(
            agent_id=agent_doc.agent_id, method=method, path=path,
            reason=_status.RCNS_REASON_SYNTHESIS_ERROR,
            explanation="server has no synthesis_runtime configured",
        )
        resp = _status.rcns_no_contract(
            reason=_status.RCNS_REASON_SYNTHESIS_ERROR,
            explanation=(
                "RCNS is enabled but the server has no synthesis runtime "
                "configured; this is a deployment misconfiguration"
            ),
            method=method, path=path,
            details={"attempt_id": attempt_id},
        )
        resp.headers["RCNS-Attempt-Id"] = attempt_id
        return resp

    # If the agent's last negotiation for this key resolved already,
    # short-circuit. Optimistic mode still executes; confirm-first
    # returns 461 with the same synthesis_id.
    plan = None
    synthesis_id = cached_sid
    contract_h: Optional[str] = None

    if cached_sid is not None:
        plan = runtime.get(cached_sid)
        contract_h = runtime.contract_hash(cached_sid)

    if plan is None:
        # Build a proposal spec for the unregistered (method, path)
        # and walk the policies.
        from core.endpoint import EndpointSpec
        try:
            available = _available_methods(server_state)
        except Exception as exc:
            return _status.rcns_no_contract(
                reason=_status.RCNS_REASON_SYNTHESIS_ERROR,
                explanation=(
                    f"could not enumerate available methods: {exc}"
                ),
                method=method, path=path,
            )

        # The proposal carries the unregistered endpoint shape. RCNS
        # is endpoint-keyed so path is always populated (the legacy
        # method-only PROPOSE path stays available via the explicit
        # PROPOSE handler).
        proposal = EndpointSpec(
            name=method.upper(),
            path=path,
            description=f"RCNS-negotiated endpoint for {method} {path}",
            required_params=[],
            optional_params=[],
            namespace="rcns",
            category="negotiated",
            error_codes=[400, 464],
        )

        try:
            plan = runtime.attempt_synthesis(proposal, available)
        except Exception as exc:
            # Synthesis runtime crashed — surface a structured 464 so
            # the operator can find this in the audit record rather
            # than chasing a 500.
            _record_negotiation(agent_doc.agent_id, now=_now())
            attempt_id = _record_attempt(
                agent_id=agent_doc.agent_id, method=method, path=path,
                reason=_status.RCNS_REASON_SYNTHESIS_ERROR,
                explanation=f"runtime raised {type(exc).__name__}: {exc}",
                details={
                    "exception": type(exc).__name__,
                    "policies_tried": [
                        getattr(p, "name", "?")
                        for p in getattr(runtime, "policies", [])
                    ],
                },
            )
            resp = _status.rcns_no_contract(
                reason=_status.RCNS_REASON_SYNTHESIS_ERROR,
                explanation=(
                    f"synthesis runtime raised {type(exc).__name__}: {exc}"
                ),
                method=method, path=path,
                details={
                    "exception": type(exc).__name__,
                    "attempt_id": attempt_id,
                },
            )
            resp.headers["RCNS-Attempt-Id"] = attempt_id
            return resp

        # Record the negotiation regardless of outcome — composition
        # cost was paid whether or not a plan was produced. This is
        # the rate-limit signal.
        _record_negotiation(agent_doc.agent_id, now=_now())

        if plan is None:
            attempt_id = _record_attempt(
                agent_id=agent_doc.agent_id, method=method, path=path,
                reason=_status.RCNS_REASON_COMPOSITION_IMPOSSIBLE,
                explanation="no composition policy returned a plan",
                details={
                    "policies_tried": [
                        getattr(p, "name", "?")
                        for p in getattr(runtime, "policies", [])
                    ],
                },
            )
            resp = _status.rcns_no_contract(
                reason=_status.RCNS_REASON_COMPOSITION_IMPOSSIBLE,
                explanation=(
                    f"no composition policy could fulfill {method} {path} "
                    f"from the server's available methods"
                ),
                method=method, path=path,
                details={"attempt_id": attempt_id},
            )
            resp.headers["RCNS-Attempt-Id"] = attempt_id
            return resp

        # Build the contract preview shape (this is what 461 returns
        # and what gets hashed for the Attribution-Record link).
        contract = {
            "method": method.upper(),
            "path": path,
            "plan_summary": plan.description or "",
            "recipe_name": plan.recipe_name,
            "recipe_version": plan.recipe_version,
            "policy_name": plan.policy_name,
        }
        contract_h = contract_hash(contract)

        # Instantiate with the RCNS-3 scoping + origin fields.
        synthesis_id = runtime.instantiate(
            plan,
            originating_agent_id=agent_doc.agent_id,
            contract_hash=contract_h,
            negotiation_origin=(
                "rcns-optimistic" if mode == "optimistic"
                else "rcns-confirmed"
            ),
        )

        if idem_key:
            _idempotent_record(
                agent_doc.agent_id, idem_key, synthesis_id,
                window_seconds=rcns_cfg.idempotency_window_seconds,
            )

        # RCNS-4: write a durable ``rcns_propose_accepted`` event
        # onto the agent's lifecycle stream so the negotiation is
        # auditable beyond the in-memory runtime state. Best-effort:
        # deployments without attribution-records enabled skip this
        # silently — the contract still works in-memory.
        try:
            from server.methods import _maybe_emit_rcns_event
            snapshot = {
                "synthesis_id": synthesis_id,
                "contract_hash": contract_h,
                "negotiation_origin": (
                    "rcns-optimistic" if mode == "optimistic"
                    else "rcns-confirmed"
                ),
                "method": method.upper(),
                "path": path,
                "recipe_name": plan.recipe_name,
                "recipe_version": plan.recipe_version,
            }
            _maybe_emit_rcns_event(
                server_state=server_state,
                event_type="rcns_propose_accepted",
                agent_id=agent_doc.agent_id,
                snapshot=snapshot,
                actor_agent_id=agent_doc.agent_id,
                reason="",
            )
        except Exception:
            # Audit failure must never break the negotiation path.
            pass

    # At this point we have a synthesis_id + plan. Two delivery modes:

    if mode == "true":
        # Confirm-first preview. The caller inspects the contract
        # and re-issues with Synthesis-Id to execute.
        preview = {
            "method": method.upper(),
            "path": path,
            "plan_summary": plan.description or "",
            "recipe_name": plan.recipe_name,
            "recipe_version": plan.recipe_version,
            "policy_name": plan.policy_name,
        }
        return _status.rcns_contract_available(
            contract=preview,
            proposed_synthesis_id=synthesis_id,
        )

    # Optimistic mode: execute the synthesis inline. The runtime's
    # ``execute`` walks the plan's steps through the same dispatcher
    # every external invocation uses, so authority checks still
    # fire. Returned response carries a ``Contract-Synthesized``
    # header so the caller learns the synthesis_id for reuse.
    response = runtime.execute(synthesis_id, request, server_state, agent_doc)
    headers = dict(response.headers or {})
    headers["Contract-Synthesized"] = synthesis_id
    response = wire.AGTPResponse(
        status_code=response.status_code,
        status_text=response.status_text,
        headers=headers,
        body_bytes=response.body_bytes,
    )
    # Stash attribution extras so _finalize_response stamps them
    # onto the Attribution-Record for chain inspection.
    response._attribution_extra = {  # type: ignore[attr-defined]
        "synthesis_id": synthesis_id,
        "contract_hash": contract_h or "",
        "negotiation_origin": "rcns-optimistic",
    }
    return response


def _available_methods(server_state: Any) -> list:
    """Snapshot the server's registered methods (REGISTRY + endpoint
    registry specs) into EndpointSpec form for the runtime."""
    from server.methods import REGISTRY, spec_to_endpoint_spec
    out = [spec_to_endpoint_spec(s) for s in REGISTRY.values()]
    # Endpoint-registry entries already carry EndpointSpec shape;
    # include them when present so multi-step plans can reach
    # operator-registered endpoints too.
    er = getattr(server_state, "endpoint_registry", None)
    if er is not None and hasattr(er, "all_endpoints"):
        try:
            for entry in er.all_endpoints():
                spec = entry[0] if isinstance(entry, tuple) else entry
                if spec is not None:
                    out.append(spec)
        except Exception:
            pass
    return out


__all__ = [
    "contract_hash",
    "lookup_attempt",
    "parse_allow_rcns",
    "reset_state_for_tests",
    "try_rcns",
]
