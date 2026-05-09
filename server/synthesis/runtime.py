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

from server.amg.grammar import AMGMethodSpec
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

    # ---- composition ----

    def attempt_synthesis(
        self,
        proposal: AMGMethodSpec,
        available_methods: List[AMGMethodSpec],
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
                return plan
        return None

    # ---- instantiation ----

    def instantiate(self, plan: SynthesisPlan) -> str:
        """
        Register a plan as an active synthesis. Returns the
        synthesis_id. Also writes a backward-compat
        :class:`Synthesis` into the legacy registry so existing
        callers (the ``_maybe_redirect_via_synthesis`` rewrite path,
        for example) see the synthesis exists.
        """
        synthesis_id = new_synthesis_id()
        with self._lock:
            self.active[synthesis_id] = plan
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

    def get(self, synthesis_id: str) -> Optional[SynthesisPlan]:
        with self._lock:
            return self.active.get(synthesis_id)

    def expire(self, synthesis_id: str) -> bool:
        """Remove a synthesis. Returns True iff it existed."""
        existed = False
        with self._lock:
            if synthesis_id in self.active:
                del self.active[synthesis_id]
                existed = True
        # Mirror to the legacy registry.
        if self.legacy_registry.remove(synthesis_id):
            existed = True
        return existed

    def clear(self) -> None:
        with self._lock:
            self.active.clear()
        self.legacy_registry.clear()

    # ---- execution ----

    def execute(
        self,
        synthesis_id: str,
        request: "wire.AGTPRequest",
        server_state: Any,
        agent_doc: "AgentDocument",
    ) -> "wire.AGTPResponse":
        """
        Execute a plan against an incoming request.

        ``server_state`` and ``agent_doc`` are passed unchanged into
        the step dispatcher so each step's authority check uses the
        same agent identity as the original request.

        Failures are surfaced as a 500 with a structured body
        identifying which step failed; capability/scope failures
        inside steps surface as 4xx with body fields naming the
        failed step.
        """
        from core import wire as wire_mod
        from core.identity import CONTENT_TYPE_JSON

        plan = self.get(synthesis_id)
        if plan is None:
            return _build_error_response(
                404,
                "Not Found",
                "synthesis-not-found",
                f"synthesis {synthesis_id!r} is not active on this server",
                extra={"synthesis_id": synthesis_id},
            )

        if self.step_dispatcher is None:
            return _build_error_response(
                500,
                "Internal Server Error",
                "synthesis-runtime-misconfigured",
                "synthesis runtime has no step dispatcher configured",
                extra={"synthesis_id": synthesis_id},
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
                return _build_error_response(
                    500,
                    "Internal Server Error",
                    "synthesis-bad-reference",
                    (
                        f"step {i + 1} ({step.method_name}) references "
                        f"undefined output {str(exc).strip(chr(39))!r}"
                    ),
                    extra={
                        "synthesis_id": synthesis_id,
                        "failed_step": i,
                        "method": step.method_name,
                    },
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
