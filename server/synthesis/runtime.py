"""
Synthesis runtime.

Three things live in this module:

  1. The legacy data types (:class:`Synthesis`, :class:`SynthesisRegistry`,
     :data:`SYNTHESES`, :func:`new_synthesis_id`) preserved here from
     the previous ``server.synthesis_runtime`` module. They keep their
     v1 shape so existing callers continue to work; the new runtime
     drives them under the hood.

  2. The :class:`SynthesisRuntime` class — the heart of this prompt.
     It tries composition policies in order, instantiates the winning
     plan as an active synthesis, and executes plans by walking each
     :class:`CompositionStep` through the same dispatcher every
     external invocation goes through. Authority preservation is
     baked in: every step is dispatched with the same agent
     identity, so capability checks and scope assertions still fire.

  3. A small ``ServerState``-shaped accessor (:func:`for_state`) that
     server code uses to fetch the current process-global runtime.

The module is thread-safe: the active-synthesis dict is guarded by
a lock so concurrent PROPOSE / SUSPEND / Synthesis-Id requests stay
clean.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from core.endpoint import EndpointSpec
from server.synthesis.errors import SynthesisError
from server.synthesis.plan import (
    CompositionStep,
    ParameterSource,
    SynthesisPlan,
)
from server.synthesis.policies import CompositionPolicy, PassthroughPolicy

if TYPE_CHECKING:
    from core import wire
    from core.identity import AgentDocument


# ---------------------------------------------------------------------------
# Legacy types kept for backward compatibility.
# ---------------------------------------------------------------------------


@dataclass
class Synthesis:
    """A session-scoped reference to an instantiated proposal.

    Preserved from v1 for backward compat with the response-body shape
    every test against ``handle_propose`` checks. New compositions
    populate the same fields by reading the canonical
    :class:`SynthesisPlan` they were built from.
    """

    synthesis_id: str
    target_method: str
    parameter_mapping: Dict[str, str] = field(default_factory=dict)
    description: str = ""
    proposal_name: Optional[str] = None
    plan: Optional[SynthesisPlan] = None  # set when produced via the new runtime

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "synthesis_id": self.synthesis_id,
            "target_method": self.target_method,
            "parameter_mapping": dict(self.parameter_mapping),
            "description": self.description,
            "proposal_name": self.proposal_name,
        }
        if self.plan is not None:
            out["plan"] = self.plan.to_dict()
        return out


class SynthesisRegistry:
    """
    In-memory map of ``synthesis_id -> Synthesis``. Process-scoped.
    Thread-safe so concurrent PROPOSE / SUSPEND requests stay clean.

    The legacy registry is preserved as the simple lookup API so
    existing callers (``_maybe_redirect_via_synthesis``) keep working.
    The new :class:`SynthesisRuntime` keeps a parallel dict of plans
    keyed by the same synthesis_id and wires the two together.
    """

    def __init__(self) -> None:
        self._items: Dict[str, Synthesis] = {}
        self._lock = threading.Lock()

    def add(self, synth: Synthesis) -> None:
        with self._lock:
            self._items[synth.synthesis_id] = synth

    def get(self, synthesis_id: str) -> Optional[Synthesis]:
        with self._lock:
            return self._items.get(synthesis_id)

    def remove(self, synthesis_id: str) -> bool:
        with self._lock:
            return self._items.pop(synthesis_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


SYNTHESES = SynthesisRegistry()


def new_synthesis_id() -> str:
    return f"syn-{secrets.token_urlsafe(12)}"


# ---------------------------------------------------------------------------
# SynthesisRuntime — the main class introduced in this prompt.
# ---------------------------------------------------------------------------


# Type alias for the dispatcher callback the runtime uses to dispatch
# each step. Having this as an injected dependency lets tests run the
# runtime against a stub dispatcher without spinning up a server.
StepDispatcher = Callable[
    ["wire.AGTPRequest", Any, "AgentDocument"],
    "wire.AGTPResponse",
]


class SynthesisRuntime:
    """
    Owns the active synthesis plans and orchestrates execution.

    Responsibilities:

      * Try composition policies in order (recipe-based first, then
        passthrough) to find a plan that fulfills a proposal.
      * Instantiate the winning plan: assign a synthesis_id, register
        in :attr:`active`, and (for backward compat) drop a parallel
        :class:`Synthesis` into :data:`SYNTHESES`.
      * Execute a synthesis_id at invocation time by walking the
        plan's steps through the injected dispatcher, threading
        outputs forward via the captured-name context, and aggregating
        the final response per the plan's ``output_aggregation`` mode.
      * Expire syntheses on SUSPEND or other lifecycle triggers.
    """

    def __init__(
        self,
        *,
        policies: Optional[List[CompositionPolicy]] = None,
        step_dispatcher: Optional[StepDispatcher] = None,
        legacy_registry: Optional[SynthesisRegistry] = None,
        max_synthesis_depth: int = 10,
    ) -> None:
        self.policies: List[CompositionPolicy] = list(policies or [])
        # Always honor the v1 passthrough behavior as the final
        # fallback: a proposal whose name matches an existing method
        # turns into a one-step identity plan.
        if not any(getattr(p, "name", "") == "passthrough" for p in self.policies):
            self.policies.append(PassthroughPolicy())
        self.step_dispatcher = step_dispatcher
        self.active: Dict[str, SynthesisPlan] = {}
        self.legacy_registry = legacy_registry or SYNTHESES
        self._lock = threading.Lock()
        #: Maximum number of plan steps a composition may produce.
        #: Plans with more steps are refused as a defense against
        #: unbounded synthesis depth. Mirrors
        #: :attr:`server.config.ServerPolicy.max_synthesis_depth`.
        self.max_synthesis_depth: int = int(max_synthesis_depth)
        #: §7 expiration tracking. ``_expires_at[id]`` is the UTC
        #: datetime past which :meth:`get` evicts the synthesis;
        #: missing key means "no hard expiration". ``_persistent`` is
        #: the set of synthesis_ids the agent marked as persistent
        #: (advisory; affects only the manifest shape, not
        #: dispatcher semantics).
        self._expires_at: Dict[str, Any] = {}
        self._persistent: set = set()
        #: RCNS-3 contract scoping: the Agent-ID that originated each
        #: synthesis. A request from a different Agent-ID presenting
        #: a synthesis_id is refused with 464 ``contract-not-yours``.
        #: Optional (PROPOSE callers populate it; tests may instantiate
        #: without). When unset, the contract is unscoped — every
        #: caller can present the id. RCNS-3 negotiation always sets
        #: this; explicit PROPOSE can choose to.
        self._originating: Dict[str, str] = {}
        #: RCNS-3 contract hash: sha256 of the canonical contract
        #: JSON. Stamped onto Attribution-Records for any action
        #: dispatched through the synthesis so the chain inspector
        #: can group invocations by contract identity (same hash =
        #: same contract, even across different synthesis_ids).
        self._contract_hashes: Dict[str, str] = {}
        #: RCNS-3 negotiation origin: ``"propose-explicit"`` (an
        #: agent called PROPOSE directly), ``"rcns-confirmed"`` (the
        #: dispatcher gate produced a 461 preview the caller
        #: accepted), or ``"rcns-optimistic"`` (the gate executed
        #: inline). Rides on Attribution-Records.
        self._origin: Dict[str, str] = {}

    # ---- composition ----

    def attempt_synthesis(
        self,
        proposal: EndpointSpec,
        available_methods: List[EndpointSpec],
    ) -> Optional[SynthesisPlan]:
        """
        Walk the policy list and return the first plan a policy
        produces, or None if no policy can fulfill the proposal.
        """
        for policy in self.policies:
            try:
                if not policy.can_fulfill(proposal, available_methods):
                    continue
                plan = policy.compose(proposal, available_methods)
            except Exception:
                # A misbehaving policy must not crash the negotiator;
                # log-shaped behavior is left to deployments to wire.
                continue
            if plan is not None:
                # Tag the plan with the producing policy if missing.
                if plan.policy_name is None:
                    plan = SynthesisPlan(
                        proposed_method=plan.proposed_method,
                        steps=plan.steps,
                        output_aggregation=plan.output_aggregation,
                        description=plan.description,
                        policy_name=getattr(policy, "name", None),
                    )
                # Sanity: every underlying method the plan names must
                # exist in the available set.
                names = {m.name for m in available_methods}
                missing = [s.method_name for s in plan.steps if s.method_name not in names]
                if missing:
                    # Policy bug: refuse the plan; try the next policy.
                    continue
                # Depth bound (agtp-api §7 policies.max_synthesis_depth).
                # Plans deeper than the configured limit are refused
                # as a defense against runaway composition; the
                # negotiator falls through to counter-proposal or
                # plain refusal.
                if (
                    self.max_synthesis_depth > 0
                    and len(plan.steps) > self.max_synthesis_depth
                ):
                    continue
                return plan
        return None

    # ---- instantiation ----

    def instantiate(
        self,
        plan: SynthesisPlan,
        *,
        expires_at: Optional[Any] = None,
        persistent: bool = False,
        originating_agent_id: Optional[str] = None,
        contract_hash: Optional[str] = None,
        negotiation_origin: str = "propose-explicit",
    ) -> str:
        """
        Register a plan as an active synthesis. Returns the
        synthesis_id. Also writes a backward-compat
        :class:`Synthesis` into the legacy registry so existing
        callers (the ``_maybe_redirect_via_synthesis`` rewrite path,
        for example) see the synthesis exists.

        §7 fields:

          * ``expires_at`` — UTC ``datetime`` past which
            :meth:`get` returns ``None`` and the synthesis is
            evicted. ``None`` means "no hard expiration" (the v1
            behavior).
          * ``persistent`` — whether this synthesis is persistent
            (true) or session-scoped (false). Persistent syntheses
            survive their originating agent's session up to
            ``expires_at``.

        RCNS-3 fields:

          * ``originating_agent_id`` — Agent-ID that produced this
            synthesis. A request from a different Agent-ID
            presenting this synthesis_id is refused at the
            dispatcher with 464 ``contract-not-yours``. ``None``
            leaves the contract unscoped (the v1 / legacy
            behavior).
          * ``contract_hash`` — sha256 hex digest of the canonical
            contract JSON. Stamped onto Attribution-Records so
            chain inspectors can group invocations by contract
            identity even across renegotiations.
          * ``negotiation_origin`` — one of ``"propose-explicit"``
            (default; an agent called PROPOSE directly),
            ``"rcns-confirmed"`` (the dispatcher gate produced a 461
            preview the caller accepted), or ``"rcns-optimistic"``
            (the gate executed inline). Rides on Attribution-Records.
        """
        synthesis_id = new_synthesis_id()
        with self._lock:
            self.active[synthesis_id] = plan
            if expires_at is not None:
                self._expires_at[synthesis_id] = expires_at
            if persistent:
                self._persistent.add(synthesis_id)
            if originating_agent_id:
                self._originating[synthesis_id] = str(originating_agent_id)
            if contract_hash:
                self._contract_hashes[synthesis_id] = str(contract_hash)
            self._origin[synthesis_id] = str(negotiation_origin)
        # Backward-compat shim: write a legacy Synthesis entry whose
        # target_method/parameter_mapping reflect the plan's first step
        # (good enough for existing callers; multi-step plans always
        # come back through the runtime anyway).
        first_step = plan.steps[0]
        legacy = Synthesis(
            synthesis_id=synthesis_id,
            target_method=first_step.method_name,
            parameter_mapping={
                src.value: target
                for target, src in first_step.parameter_source.items()
                if src.kind == "proposal" and isinstance(src.value, str)
            },
            description=plan.description or "",
            proposal_name=plan.proposed_method.name,
            plan=plan,
        )
        self.legacy_registry.add(legacy)
        return synthesis_id

    def resolve(
        self, method: str, path: str,
    ) -> Optional[Dict[str, Any]]:
        """RCNS-2: look up an active synthesis by ``(method, path)``.

        Scans the active plans for one whose ``proposed_method``
        matches the given verb and path. Returns a small record
        carrying the synthesis_id plus the plan's recipe metadata,
        or ``None`` when nothing matches.

        This is the hook
        :func:`core.endpoint_tiers.classify_tier` will consult once
        RCNS-3 wires the dispatcher gate — a Tier C classification
        is exactly "the synthesis runtime has an active plan keyed
        to this (method, path)". A linear scan over the active dict
        is fine for v00; an active deployment is unlikely to carry
        more than a few hundred live contracts and the gate is not
        on the hot path of every request.

        Method matching is case-insensitive (per AGTP convention);
        path matching is byte-exact (the path grammar enforces
        canonical form). Method-only plans (path = ``None``) match
        the path ``"/"`` only.
        """
        method_upper = method.upper()
        with self._lock:
            for sid, plan in self.active.items():
                spec_method = plan.proposed_method.name.upper()
                spec_path = plan.proposed_method.path or "/"
                if spec_method == method_upper and spec_path == path:
                    return {
                        "synthesis_id": sid,
                        "method": method_upper,
                        "path": spec_path,
                        "recipe_name": plan.recipe_name,
                        "recipe_version": plan.recipe_version,
                        "policy_name": plan.policy_name,
                        "originating_agent_id": self._originating.get(sid),
                        "contract_hash": self._contract_hashes.get(sid),
                        "negotiation_origin": self._origin.get(
                            sid, "propose-explicit",
                        ),
                    }
        return None

    def get(self, synthesis_id: str) -> Optional[SynthesisPlan]:
        """
        Return the active plan for ``synthesis_id``, or ``None`` if
        the id is unknown or the synthesis has expired.

        §7 expiration check: if ``expires_at`` was set at
        :meth:`instantiate` time and the current time has passed it,
        the synthesis is evicted (a "lazy sweep" at lookup) before
        the ``None`` return — subsequent calls keep returning ``None``.
        """
        with self._lock:
            plan = self.active.get(synthesis_id)
            if plan is None:
                return None
            expires_at = self._expires_at.get(synthesis_id)
        if expires_at is not None:
            from datetime import datetime, timezone
            if datetime.now(tz=timezone.utc) >= expires_at:
                self.expire(synthesis_id, reason="expired")
                return None
        return plan

    def is_expired(self, synthesis_id: str) -> bool:
        """True when ``synthesis_id`` is gone from the runtime
        (either never existed or has been expired). Useful for
        clients that want the dispatcher to distinguish
        ``not-found`` from ``expired-synthesis``."""
        with self._lock:
            return synthesis_id not in self.active

    def is_persistent(self, synthesis_id: str) -> bool:
        with self._lock:
            return synthesis_id in self._persistent

    def expires_at(self, synthesis_id: str) -> Optional[Any]:
        with self._lock:
            return self._expires_at.get(synthesis_id)

    def originating_agent_id(self, synthesis_id: str) -> Optional[str]:
        """RCNS-3: the Agent-ID that produced this synthesis, or
        ``None`` when the contract is unscoped (legacy / pre-RCNS-3
        explicit PROPOSE callers that didn't pass the field)."""
        with self._lock:
            return self._originating.get(synthesis_id)

    def contract_hash(self, synthesis_id: str) -> Optional[str]:
        """RCNS-3: the canonical-contract sha256 stamped at
        instantiation, or ``None`` if the caller didn't compute one
        (legacy syntheses)."""
        with self._lock:
            return self._contract_hashes.get(synthesis_id)

    def negotiation_origin(self, synthesis_id: str) -> str:
        """RCNS-3: ``"propose-explicit"`` / ``"rcns-confirmed"`` /
        ``"rcns-optimistic"`` depending on how the synthesis was
        created. Defaults to ``"propose-explicit"`` for entries
        that predate the origin tracking."""
        with self._lock:
            return self._origin.get(synthesis_id, "propose-explicit")

    # ---- RCNS-4 follow-up: on_policy_change sweep ----

    def sweep_for_policy_change(
        self,
        *,
        mode: str = "grandfather",
    ) -> List[Dict[str, Any]]:
        """Identify (and in ``invalidate`` mode, evict) contracts whose
        captured recipe lineage no longer matches the current recipe
        set on this runtime.

        Two policy-change modes per ``[policies.rcns].on_policy_change``:

          * ``grandfather`` (default) — read-only. Walk active
            contracts, return records for every one whose captured
            ``recipe_version`` differs from the current version of
            the same-named recipe (or whose recipe was removed
            entirely). Records carry ``action = "grandfathered"`` so
            the operator can see the drift without losing any
            contracts.
          * ``invalidate`` — destructive. Same walk; for each
            mismatch, expire the contract from the runtime with
            ``reason = "policy-change-invalidation"``. Records carry
            ``action = "evicted"``.

        Passthrough contracts (no recipe lineage, e.g. plain
        verb-match syntheses) are unaffected — there's no recipe to
        drift against.

        Returns the list of records (synthesis_id, originating_agent_id,
        recipe_name, captured_version, current_version, action).
        Operators surface this via ``REVOKE target=stale-contracts``;
        the audit-event emission lives there too so the runtime
        stays free of audit-store coupling.
        """
        if mode not in ("grandfather", "invalidate"):
            raise ValueError(
                f"sweep mode must be 'grandfather' or 'invalidate' "
                f"(got {mode!r})"
            )
        # Build a fresh snapshot of current recipe versions across
        # every recipe-bearing policy. A recipe that's been removed
        # entirely (no entry under its name) counts as a mismatch.
        current_versions: Dict[str, str] = {}
        for policy in self.policies:
            recipes = getattr(policy, "recipes", None)
            if not recipes:
                continue
            for r in recipes:
                current_versions[r.name] = r.version

        records: List[Dict[str, Any]] = []
        with self._lock:
            sids = list(self.active.keys())
        for sid in sids:
            with self._lock:
                plan = self.active.get(sid)
            if plan is None:
                continue
            captured_name = plan.recipe_name
            captured_version = plan.recipe_version
            if captured_name is None:
                # Passthrough or other non-recipe origin — no recipe
                # to drift against, nothing to invalidate.
                continue
            current = current_versions.get(captured_name)
            if current == captured_version:
                continue
            # Mismatch — recipe was edited, replaced, or removed.
            with self._lock:
                originator = self._originating.get(sid, "")
                contract_h = self._contract_hashes.get(sid, "")
                negotiation_origin = self._origin.get(
                    sid, "propose-explicit",
                )
            record: Dict[str, Any] = {
                "synthesis_id": sid,
                "originating_agent_id": originator or None,
                "contract_hash": contract_h or None,
                "negotiation_origin": negotiation_origin,
                "method": plan.proposed_method.name,
                "path": plan.proposed_method.path or "/",
                "recipe_name": captured_name,
                "captured_version": captured_version,
                "current_version": current,  # None when recipe removed
                "action": "evicted" if mode == "invalidate" else "grandfathered",
            }
            if mode == "invalidate":
                self.expire(sid, reason="policy-change-invalidation")
            records.append(record)
        return records

    def sweep_expired(self) -> List[str]:
        """Walk active syntheses and expire any past their
        ``expires_at``. Returns the list of expired ids. Called at
        startup by ``server.main`` alongside the catalog-evolution
        invalidation sweep."""
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc)
        with self._lock:
            expired_ids = [
                sid for sid, ts in list(self._expires_at.items())
                if ts is not None and now >= ts
            ]
        for sid in expired_ids:
            self.expire(sid, reason="expired")
        return expired_ids

    def expire(
        self,
        synthesis_id: str,
        *,
        reason: str = "",
    ) -> bool:
        """
        Remove a synthesis. Returns True iff it existed.

        ``reason`` is a structured tag describing why the synthesis
        was expired. Phase-6 introduces this for catalog-evolution
        cleanups (``"catalog-evolution-removed-verb"``); pre-Phase-6
        callers continue to call ``expire(synthesis_id)`` and the
        reason rides as the empty string.
        """
        existed = False
        with self._lock:
            if synthesis_id in self.active:
                del self.active[synthesis_id]
                existed = True
            # Clean up the RCNS-3 side-tables so an expired
            # synthesis_id can't leak originating-agent or contract
            # info to a future negotiation that happens to reuse
            # the id (unlikely but defensible).
            self._originating.pop(synthesis_id, None)
            self._contract_hashes.pop(synthesis_id, None)
            self._origin.pop(synthesis_id, None)
            self._expires_at.pop(synthesis_id, None)
            self._persistent.discard(synthesis_id)
        # Mirror to the legacy registry.
        if self.legacy_registry.remove(synthesis_id):
            existed = True
        if existed and reason:
            # Stderr matches the rest of the boot logging in this
            # repo — once a structured logger lands this becomes a
            # ``logger.info`` with the same fields.
            import sys as _sys
            print(
                f"[server] synthesis {synthesis_id} expired ({reason})",
                file=_sys.stderr,
            )
        return existed

    # ---- catalog evolution (Phase 6) ----

    def invalidate_against_catalog(self) -> List[str]:
        """
        Walk active syntheses; expire any whose plans reference a
        verb the current catalog no longer admits.

        Called at server startup (after the runtime has been
        constructed but before the listener accepts requests) so
        in-flight plans don't fail mid-execution after a catalog
        upgrade. Returns the list of expired synthesis IDs so the
        boot sequence can log a count.

        Reason tag on expiry: ``catalog-evolution-removed-verb``.
        """
        from core.methods import is_approved_verb

        expired: List[str] = []
        with self._lock:
            ids = list(self.active.keys())
        for synthesis_id in ids:
            plan = self.get(synthesis_id)
            if plan is None:
                continue
            if not all(
                is_approved_verb(step.method_name) for step in plan.steps
            ):
                self.expire(
                    synthesis_id,
                    reason="catalog-evolution-removed-verb",
                )
                expired.append(synthesis_id)
        return expired

    def clear(self) -> None:
        with self._lock:
            self.active.clear()
        self.legacy_registry.clear()

    # ---- recipe introspection (Phase-3 composition-binding helpers) ----

    def list_recipes(self) -> List[str]:
        """
        Return the names of every recipe currently loaded into a
        :class:`RecipeBasedPolicy` on this runtime, in declaration
        order. Composition-bound endpoints reference recipes by name;
        this helper surfaces what's available so a misconfiguration
        can be diagnosed at startup with a clean ``InvalidHandlerError``
        message.
        """
        names: List[str] = []
        for policy in self.policies:
            recipes = getattr(policy, "recipes", None)
            if recipes is None:
                continue
            for r in recipes:
                if r.name not in names:
                    names.append(r.name)
        return names

    def has_recipe(self, name: str) -> bool:
        """True iff ``name`` matches a loaded recipe."""
        return self.get_recipe(name) is not None

    def get_recipe(self, name: str) -> Optional[Any]:
        """Return the :class:`Recipe` named ``name``, or ``None``."""
        for policy in self.policies:
            recipes = getattr(policy, "recipes", None)
            if recipes is None:
                continue
            for r in recipes:
                if r.name == name:
                    return r
        return None

    # ---- execution ----

    def execute(
        self,
        synthesis_id: str,
        request: "wire.AGTPRequest",
        server_state: Any,
        agent_doc: "AgentDocument",
    ) -> "wire.AGTPResponse":
        """
        Execute the plan registered under ``synthesis_id``.

        Looks the plan up from the active dict and forwards to
        :meth:`execute_plan`. Used by the Synthesis-Id rewrite path
        in the dispatcher.
        """
        plan = self.get(synthesis_id)
        if plan is None:
            return _build_error_response(
                404,
                "Not Found",
                "synthesis-not-found",
                f"synthesis {synthesis_id!r} is not active on this server",
                extra={"synthesis_id": synthesis_id},
            )
        return self.execute_plan(
            plan, request, server_state, agent_doc,
            synthesis_id=synthesis_id,
        )

    def execute_plan(
        self,
        plan: SynthesisPlan,
        request: "wire.AGTPRequest",
        server_state: Any,
        agent_doc: "AgentDocument",
        *,
        synthesis_id: str = "",
    ) -> "wire.AGTPResponse":
        """
        Execute a :class:`SynthesisPlan` against an incoming request.

        ``server_state`` and ``agent_doc`` are passed unchanged into
        the step dispatcher so each step's authority check uses the
        same agent identity as the original request — that's the
        runtime's "authority preservation" guarantee.

        ``synthesis_id`` is included in the response body when set;
        composition-bound endpoints (Phase 3) use this method
        directly without ever instantiating an active synthesis, so
        they pass an empty string.

        Failures are surfaced as a non-200 with a structured body
        identifying which step failed; capability/scope failures
        inside steps surface as the underlying status code with body
        fields naming the failed step.
        """
        if self.step_dispatcher is None:
            return _build_error_response(
                500,
                "Internal Server Error",
                "synthesis-runtime-misconfigured",
                "synthesis runtime has no step dispatcher configured",
                extra={"synthesis_id": synthesis_id} if synthesis_id else None,
            )

        # Parse the proposal's parameters from the incoming request body.
        proposal_params = _parse_request_params(request)
        context: Dict[str, Any] = {}
        captured: List[Dict[str, Any]] = []  # ordered audit trail of step outcomes

        for i, step in enumerate(plan.steps):
            try:
                step_params = self._resolve_parameters(
                    step, proposal_params, context
                )
            except KeyError as exc:
                # A previous-step reference that wasn't captured.
                extra = {
                    "failed_step": i,
                    "method": step.method_name,
                }
                if synthesis_id:
                    extra["synthesis_id"] = synthesis_id
                return _build_error_response(
                    500,
                    "Internal Server Error",
                    "synthesis-bad-reference",
                    (
                        f"step {i + 1} ({step.method_name}) references "
                        f"undefined output {str(exc).strip(chr(39))!r}"
                    ),
                    extra=extra,
                )

            step_request = self._build_step_request(
                request, step.method_name, step_params,
            )
            step_response = self.step_dispatcher(
                step_request, server_state, agent_doc
            )
            step_record: Dict[str, Any] = {
                "step": i,
                "method": step.method_name,
                "status_code": step_response.status_code,
            }
            captured.append(step_record)

            if step_response.status_code != 200:
                # The dispatcher's standard error response already has
                # a structured JSON body. Wrap it with a synthesis
                # envelope so the agent sees which step failed.
                err = SynthesisError(
                    failed_step=i,
                    method=step.method_name,
                    underlying_error=step_response,
                    captured_outputs=dict(context),
                )
                return self._build_failure_response(
                    plan, synthesis_id, err, captured,
                )

            parsed_body = _parse_response_body(step_response)
            step_record["output"] = parsed_body
            if step.capture_output_as:
                context[step.capture_output_as] = parsed_body

        return self._aggregate(plan, synthesis_id, context, captured)

    # ---- internals ----

    @staticmethod
    def _resolve_parameters(
        step: CompositionStep,
        proposal_params: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for target, src in step.parameter_source.items():
            if src.kind == "constant":
                out[target] = src.value
            elif src.kind == "proposal":
                if src.value in proposal_params:
                    out[target] = proposal_params[src.value]
                # missing proposal params are simply absent; the
                # underlying handler will surface its own 422 if
                # required parameters are missing
            elif src.kind == "previous_step":
                if src.value not in context:
                    raise KeyError(src.value)
                out[target] = context[src.value]
        return out

    @staticmethod
    def _build_step_request(
        original: "wire.AGTPRequest",
        method_name: str,
        params: Dict[str, Any],
    ) -> "wire.AGTPRequest":
        """
        Build the request object for a single step. Carries the
        original request's auth-relevant headers (Target-Agent,
        Authority-Scope, etc.) so the step dispatcher's checks see
        the same agent identity.
        """
        from core import wire as wire_mod

        body_bytes = (
            json.dumps(params).encode("utf-8") if params else b""
        )
        headers = dict(original.headers)
        # Strip Synthesis-Id from inner steps so the step dispatcher
        # does not recurse into the runtime.
        for k in list(headers):
            if k.lower() == "synthesis-id":
                del headers[k]
        if body_bytes:
            headers["Content-Type"] = "application/json"
        return wire_mod.AGTPRequest(
            method=method_name,
            headers=headers,
            body_bytes=body_bytes,
        )

    @staticmethod
    def _aggregate(
        plan: SynthesisPlan,
        synthesis_id: str,
        context: Dict[str, Any],
        captured: List[Dict[str, Any]],
    ) -> "wire.AGTPResponse":
        if plan.output_aggregation == "last":
            last = captured[-1] if captured else {}
            payload = last.get("output")
        elif plan.output_aggregation == "merge":
            payload = {}
            for entry in captured:
                out = entry.get("output")
                if isinstance(out, dict):
                    payload.update(out)
        else:  # "list"
            payload = [entry.get("output") for entry in captured]

        body = {
            "method": plan.proposed_method.name,
            "synthesis_id": synthesis_id,
            "outcome": "ok",
            "output": payload,
            "steps": [
                {"method": e["method"], "status_code": e["status_code"]}
                for e in captured
            ],
        }
        return _json_response(200, "OK", body)

    @staticmethod
    def _build_failure_response(
        plan: SynthesisPlan,
        synthesis_id: str,
        err: SynthesisError,
        captured: List[Dict[str, Any]],
    ) -> "wire.AGTPResponse":
        underlying = err.underlying_error
        underlying_body: Any = None
        try:
            underlying_body = json.loads(underlying.body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            underlying_body = underlying.body_bytes.decode(
                "utf-8", errors="replace"
            )
        body = {
            "method": plan.proposed_method.name,
            "synthesis_id": synthesis_id,
            "outcome": "error",
            "error": {
                "code": "synthesis-step-failed",
                "failed_step": err.failed_step,
                "method": err.method,
                "underlying_status": underlying.status_code,
                "underlying": underlying_body,
                "captured_outputs": err.captured_outputs,
            },
            "steps": [
                {"method": e["method"], "status_code": e["status_code"]}
                for e in captured
            ],
        }
        # Surface the same status code as the underlying failure so
        # callers can branch on auth (403), scope (455), invocation
        # (4xx/5xx) without inspecting the body.
        return _json_response(
            underlying.status_code, underlying.status_text or "Error", body,
        )


# ---------------------------------------------------------------------------
# Module helpers (kept private to avoid import churn).
# ---------------------------------------------------------------------------


def _parse_request_params(request: "wire.AGTPRequest") -> Dict[str, Any]:
    if not request.body_bytes:
        return {}
    try:
        parsed = json.loads(request.body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_response_body(response: "wire.AGTPResponse") -> Any:
    if not response.body_bytes:
        return None
    try:
        return json.loads(response.body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return response.body_bytes.decode("utf-8", errors="replace")


def _json_response(status: int, text: str, body: Dict[str, Any]) -> "wire.AGTPResponse":
    from core import wire as wire_mod

    body_bytes = json.dumps(body, indent=2).encode("utf-8")
    return wire_mod.AGTPResponse(
        status_code=status,
        status_text=text,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body_bytes)),
        },
        body_bytes=body_bytes,
    )


def _build_error_response(
    status: int,
    text: str,
    code: str,
    explanation: str,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> "wire.AGTPResponse":
    body: Dict[str, Any] = {
        "error": {"code": code, "explanation": explanation},
    }
    if extra:
        body["error"].update(extra)
    return _json_response(status, text, body)


__all__ = [
    "SYNTHESES",
    "Synthesis",
    "SynthesisRegistry",
    "SynthesisRuntime",
    "StepDispatcher",
    "new_synthesis_id",
]
