"""
PROPOSE counter-proposal helper.

PROPOSE acceptance flows through :mod:`server.synthesis` (composition
runtime + plan execution). This module owns the *counter-proposal*
fallback: when the runtime declines, it scans the server's universe
for a near-match method (synonym table or Levenshtein) and returns
its serialized spec so the client receives a 422 with a
``counter_proposal`` body.

Refusal-with-reason (the third PROPOSE path) is built directly from
:func:`core.status.negotiation_refused` in
:func:`server.methods.handle_propose` — no policy class is needed
for it.

Backward compat: the legacy registry types (:class:`Synthesis`,
:class:`SynthesisRegistry`, :data:`SYNTHESES`,
:func:`new_synthesis_id`) are re-exported here so existing imports
(``from server.negotiation import SYNTHESES``) keep resolving while
the runtime owns their lifecycle.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.endpoint import EndpointSpec
from server.methods import MethodSpec, REGISTRY, spec_to_dict
from server.synthesis.runtime import (
    SYNTHESES,
    Synthesis,
    SynthesisRegistry,
    new_synthesis_id,
)


# ---------------------------------------------------------------------------
# Synonym table + close-match search.
# ---------------------------------------------------------------------------


# Tiny synonym table. Augments Levenshtein for cases where two verbs
# express the same intent but differ wildly in spelling. Real
# deployments would either replace this with semantic embeddings or
# disable counter-proposals entirely.
SEMANTIC_SYNONYMS: Dict[str, List[str]] = {
    "RESERVE":   ["BOOK"],
    "BOOK":      ["RESERVE"],
    "AUDIT":     ["RECONCILE"],
    "RECONCILE": ["AUDIT"],
    "FETCH":     ["QUERY", "DESCRIBE"],
    "GET":       ["QUERY", "DESCRIBE"],
    "RUN":       ["EXECUTE"],
    "INVOKE":    ["EXECUTE"],
    "TELL":      ["NOTIFY"],
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
                prev[j] + 1,         # deletion
                curr[j - 1] + 1,     # insertion
                prev[j - 1] + cost,  # substitution
            )
        prev = curr
    return prev[-1]


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


# ---------------------------------------------------------------------------
# find_counter_proposal — the public entry point.
# ---------------------------------------------------------------------------


def find_counter_proposal(
    proposal: EndpointSpec,
    server_methods: Optional[Dict[str, MethodSpec]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Search the server's method universe for a near-match the
    proposer probably meant. Returns a ``MethodSpec``-shaped dict
    (suitable for the body of a 422 ``counter_proposal`` response)
    when a close match is found, or ``None`` when nothing is close
    enough — in which case the caller surfaces a plain
    ``negotiation-refused`` response.

    The search runs the same steps the v1 ``BasicNegotiationPolicy``
    used (synonym table + Levenshtein-2). Lifted to a free function
    so the policy class can retire.

    :param proposal: validated proposal spec (from
        :func:`EndpointSpec.from_proposal`).
    :param server_methods: optional override of the universe; defaults
        to the live ``REGISTRY``. Tests pass a snapshot here.
    """
    universe = server_methods if server_methods is not None else REGISTRY
    name = proposal.name.upper()
    close = _find_close_match(name, universe)
    if close is None or close == name:
        # An exact match would have been an accept (handled by the
        # synthesis runtime upstream of us); if we got here at all,
        # the runtime already declined and an exact match means the
        # method exists but no policy could compose it.
        return None
    return spec_to_dict(universe[close])


__all__ = [
    "SEMANTIC_SYNONYMS",
    "find_counter_proposal",
    # Re-exported from synthesis_runtime for backward-compat:
    "SYNTHESES",
    "Synthesis",
    "SynthesisRegistry",
    "new_synthesis_id",
]
