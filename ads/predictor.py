"""
A minimal ADS predictor: co-occurrence + frequency.

Given an agent's captured signals, predict the capabilities it is most
likely to need next. The model is first-order: blend what has historically
*followed* the agent's most recent capability (transition counts) with the
agent's overall capability frequency. Deterministic — the same signals
always yield the same ranked prediction.

This is intentionally simple; the point of the prototype is the *interface*
(signals in, ranked capability predictions out) and the surrounding privacy
and preloading behavior, not a sophisticated model.
"""

from __future__ import annotations

from collections import Counter
from typing import List, Optional, Tuple

#: Weight on "what follows the current capability" vs. overall frequency.
_TRANSITION_WEIGHT = 2.0
_FREQUENCY_WEIGHT = 1.0


def predict_capabilities(
    store, agent_id: str, *, top: int = 3, context: Optional[str] = None
) -> List[Tuple[str, float]]:
    """
    Ranked ``(capability, score)`` predictions for ``agent_id`` — capabilities
    it is likely to need next but is *not* currently exercising. ``context``
    overrides the "current" capability (defaults to the last observed).

    Derives strictly from ``agent_id``'s own signals (per-agent containment).
    """
    current = context if context is not None else store.last_capability(agent_id)
    freq: Counter = store.frequency(agent_id)
    trans: Counter = store.transitions_from(agent_id, current) if current else Counter()

    scores: Counter = Counter()
    for cap, n in trans.items():
        scores[cap] += _TRANSITION_WEIGHT * n
    for cap, n in freq.items():
        scores[cap] += _FREQUENCY_WEIGHT * n

    # Don't predict the capability the agent is already exercising.
    if current in scores:
        del scores[current]

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:top]
