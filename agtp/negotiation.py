"""
PROPOSE negotiation policy and synthesis registry.

PROPOSE has three response paths:

  1. Accept and instantiate
     The server returns 200 and a Synthesis describing how the
     proposal maps onto an existing method. The client subsequently
     invokes by ``synthesis_id`` and the server forwards to the
     underlying method, optionally remapping parameter names.

  2. Refuse with reason (460 Negotiation Refused)
     One of REFUSAL_OUT_OF_SCOPE / REFUSAL_AMBIGUOUS /
     REFUSAL_INSUFFICIENT / REFUSAL_POLICY_REFUSED.

  3. Counter-propose (461 Counter-Proposal)
     The server suggests an existing or near-existing method that
     covers the same intent. The client decides whether to accept the
     counter (re-invoke against the named method) or escalate.

Negotiation policy is pluggable; deployments swap in their own
``NegotiationPolicy`` implementation. The default
``BasicNegotiationPolicy`` performs structural validation, exact-name
matching against the server's universe, a small synonym table, and a
Levenshtein fallback for "close" names. The semantic matcher is
deliberately illustrative; full AMG semantics are future work.

Synthesis lifecycle
-------------------
A Synthesis lives in the process-scoped ``SynthesisRegistry`` from the
moment PROPOSE returns 200 until it is either explicitly cleared (by
SUSPEND naming the synthesis) or the server process exits. Restart
clears all syntheses. A future revision will introduce durable
syntheses tied to long-running session tokens.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from agtp import status
from agtp.methods import REGISTRY, MethodSpec, spec_to_dict


# --------------------------------------------------------------------
# Synthesis registry.
# --------------------------------------------------------------------


@dataclass
class Synthesis:
    """A session-scoped reference to an instantiated proposal."""

    synthesis_id: str
    target_method: str
    parameter_mapping: Dict[str, str] = field(default_factory=dict)
    description: str = ""
    proposal_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "synthesis_id": self.synthesis_id,
            "target_method": self.target_method,
            "parameter_mapping": dict(self.parameter_mapping),
            "description": self.description,
            "proposal_name": self.proposal_name,
        }


class SynthesisRegistry:
    """
    In-memory map of ``synthesis_id -> Synthesis``. Process-scoped.
    Thread-safe so concurrent PROPOSE / SUSPEND requests stay clean.
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


# Process-global registry. Tests can call ``SYNTHESES.clear()`` between
# runs to keep state isolated.
SYNTHESES = SynthesisRegistry()


def new_synthesis_id() -> str:
    return f"syn-{secrets.token_urlsafe(12)}"


# --------------------------------------------------------------------
# Decision objects + Policy protocol.
# --------------------------------------------------------------------


@dataclass
class ProposalDecision:
    """
    One of three outcomes returned by a NegotiationPolicy.

    * outcome="accept"  : ``synthesis`` is set, ``refusal_reason`` and
                          ``counter_proposal`` are None.
    * outcome="refuse"  : ``refusal_reason`` and
                          ``refusal_explanation`` are set.
    * outcome="counter" : ``counter_proposal`` is set to a MethodSpec-
                          shaped dict.
    """

    outcome: str
    synthesis: Optional[Synthesis] = None
    refusal_reason: Optional[str] = None
    refusal_explanation: Optional[str] = None
    counter_proposal: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        valid = {"accept", "refuse", "counter"}
        if self.outcome not in valid:
            raise ValueError(
                f"outcome must be one of {sorted(valid)} (got {self.outcome!r})"
            )


class NegotiationPolicy(Protocol):
    """
    Plug-in interface for PROPOSE handling.

    ``server_methods`` is a snapshot of REGISTRY at the time of
    evaluation. The policy SHOULD treat it as read-only; mutating it
    is not supported.
    """

    def evaluate(
        self,
        proposal: Dict[str, Any],
        server_methods: Dict[str, MethodSpec],
    ) -> ProposalDecision: ...


# --------------------------------------------------------------------
# BasicNegotiationPolicy: the default, illustrative implementation.
# --------------------------------------------------------------------


# Tiny synonym table. Augments Levenshtein for cases where two verbs
# express the same intent but differ wildly in spelling. Real
# deployments would either replace this with semantic embeddings or
# disable counter-proposals entirely.
SEMANTIC_SYNONYMS: Dict[str, List[str]] = {
    "RESERVE": ["BOOK"],
    "BOOK":    ["RESERVE"],
    "AUDIT":   ["RECONCILE"],
    "RECONCILE": ["AUDIT"],
    "FETCH":   ["QUERY", "DESCRIBE"],
    "GET":     ["QUERY", "DESCRIBE"],
    "RUN":     ["EXECUTE"],
    "INVOKE":  ["EXECUTE"],
    "TELL":    ["NOTIFY"],
}


def _levenshtein(a: str, b: str) -> int:
    """Iterative Levenshtein distance; small inputs only."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                prev[j] + 1,        # deletion
                curr[j - 1] + 1,    # insertion
                prev[j - 1] + cost, # substitution
            )
        prev = curr
    return prev[-1]


def _is_amg_valid_name(name: str) -> bool:
    """Stub AMG validation: single uppercase token, alphabetic, length 3+."""
    return (
        isinstance(name, str)
        and name.isupper()
        and name.isalpha()
        and len(name) >= 3
    )


def _find_close_match(
    name: str,
    universe: Dict[str, MethodSpec],
    *,
    max_levenshtein: int = 2,
) -> Optional[str]:
    """
    Return the closest matching method name in ``universe``, or None.

    Order of attempts: exact, synonym table, Levenshtein within
    ``max_levenshtein``.
    """
    if name in universe:
        return name
    for synonym in SEMANTIC_SYNONYMS.get(name, []):
        if synonym in universe:
            return synonym
    best: Optional[tuple[str, int]] = None
    for candidate in universe:
        d = _levenshtein(name, candidate)
        if best is None or d < best[1]:
            best = (candidate, d)
    if best and best[1] <= max_levenshtein:
        return best[0]
    return None


def _proposal_required_keys() -> tuple[str, ...]:
    """Structural keys a proposal must carry."""
    return ("name", "parameters", "outcome")


class BasicNegotiationPolicy:
    """
    Default policy. Refuses on missing structure, refuses on bad
    names, accepts when the proposed name matches an existing method,
    counter-proposes when a close name exists in the server universe,
    refuses ``out_of_scope`` otherwise.
    """

    def evaluate(
        self,
        proposal: Dict[str, Any],
        server_methods: Dict[str, MethodSpec],
    ) -> ProposalDecision:
        # Structural validation.
        missing = [k for k in _proposal_required_keys() if k not in proposal]
        if missing:
            return ProposalDecision(
                outcome="refuse",
                refusal_reason=status.REFUSAL_INSUFFICIENT,
                refusal_explanation=(
                    f"proposal lacks required field(s): {', '.join(missing)}"
                ),
            )

        name = str(proposal["name"]).upper()
        if not _is_amg_valid_name(name):
            return ProposalDecision(
                outcome="refuse",
                refusal_reason=status.REFUSAL_AMBIGUOUS,
                refusal_explanation=(
                    "proposed name fails AMG validation: must be a single "
                    "uppercase alphabetic token of length >= 3"
                ),
            )

        # Exact match: accept and synthesize a passthrough.
        if name in server_methods:
            spec = server_methods[name]
            requested_params = list(proposal.get("parameters", {}) or {})
            mapping = {p: p for p in requested_params if p in (
                set(spec.required_params) | set(spec.optional_params)
            )}
            synth = Synthesis(
                synthesis_id=new_synthesis_id(),
                target_method=name,
                parameter_mapping=mapping,
                description=(
                    proposal.get("description")
                    or f"synthesis pointing at {name}"
                ),
                proposal_name=name,
            )
            return ProposalDecision(outcome="accept", synthesis=synth)

        # Close match: counter-propose.
        close = _find_close_match(name, server_methods)
        if close is not None:
            return ProposalDecision(
                outcome="counter",
                counter_proposal=spec_to_dict(server_methods[close]),
            )

        # Nothing close: out of scope.
        return ProposalDecision(
            outcome="refuse",
            refusal_reason=status.REFUSAL_OUT_OF_SCOPE,
            refusal_explanation=(
                f"proposed verb {name!r} has no close match on this server"
            ),
        )


__all__ = [
    "BasicNegotiationPolicy",
    "NegotiationPolicy",
    "ProposalDecision",
    "SEMANTIC_SYNONYMS",
    "SYNTHESES",
    "Synthesis",
    "SynthesisRegistry",
    "new_synthesis_id",
]
