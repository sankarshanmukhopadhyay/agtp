"""
Composite ranking for DISCOVER results (PDD §6.3 "Ranking").

A ranked DISCOVER response orders candidates by a weighted blend of three
signals::

    score = 0.3 * trust_tier_norm + 0.4 * behavioral_trust + 0.3 * capability_match

The behavioral score carries the most weight because it reflects verified
conduct from the packaging pipeline; the tier is a coarser static
classification; the capability match is a query-time estimate. Weights are
overridable (an ANS MAY publish alternative profiles), so the defaults are
a starting posture rather than a fixed contract.

Ranking is deterministic for identical inputs: equal scores break ties on
Agent-ID, so the same query against the same population always returns the
same order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from presence.records import PresenceRecord


DEFAULT_WEIGHTS = {"tier": 0.3, "behavioral": 0.4, "capability": 0.3}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Scored:
    """A ranked candidate: the record, its composite score, and the
    capability-match component (surfaced to the caller as
    ``capability_match_score``)."""

    record: PresenceRecord
    score: float
    capability_match: float


def tier_norm(tier: Optional[int]) -> float:
    """Normalize a trust tier to [0,1] where tier 1 (most trusted) is 1.0.
    tier1 -> 1.0, tier2 -> 0.667, tier3 -> 0.333. Unknown -> 0.0."""
    if tier not in (1, 2, 3):
        return 0.0
    return (4 - tier) / 3.0


def intent_tokens(intent: Optional[str]) -> Set[str]:
    """Lowercase alphanumeric tokens of a natural-language intent."""
    if not intent:
        return set()
    return set(_TOKEN_RE.findall(intent.lower()))


def capability_match_score(
    agent_caps: Set[str],
    *,
    capability: Optional[str] = None,
    intent: Optional[str] = None,
) -> float:
    """
    How well an agent matches what the query asked for, in [0,1].

    * an exact ``capability`` filter → 1.0 (the agent is in the result set
      only because it carries that capability);
    * else an ``intent`` → the fraction of intent tokens the agent's
      derived capabilities cover;
    * else (no capability signal) → 1.0.

    Deterministic: depends only on the agent's capability set and the query.
    """
    if capability:
        return 1.0
    tokens = intent_tokens(intent)
    if not tokens:
        return 1.0
    hits = sum(1 for t in tokens if t in agent_caps)
    return hits / len(tokens)


def composite_score(
    *,
    tier: Optional[int],
    behavioral: Optional[float],
    capability_match: float,
    weights: Optional[Dict[str, float]] = None,
) -> float:
    w = weights or DEFAULT_WEIGHTS
    beh = behavioral if isinstance(behavioral, (int, float)) else 0.0
    return (
        w["tier"] * tier_norm(tier)
        + w["behavioral"] * float(beh)
        + w["capability"] * capability_match
    )


def rank_records(
    records: List[PresenceRecord],
    agent_caps_of,
    *,
    capability: Optional[str] = None,
    intent: Optional[str] = None,
    weights: Optional[Dict[str, float]] = None,
) -> List[Scored]:
    """
    Score and order ``records`` most-relevant first. ``agent_caps_of`` maps
    a record to its derived capability set (injected so ranking doesn't
    import the scope machinery). Ties break on Agent-ID for a stable order.
    """
    scored: List[Scored] = []
    for rec in records:
        caps = agent_caps_of(rec)
        cap_match = capability_match_score(caps, capability=capability, intent=intent)
        beh = rec.result_entry.get("behavioral_trust_score")
        s = composite_score(
            tier=rec.result_entry.get("trust_tier"),
            behavioral=beh,
            capability_match=cap_match,
            weights=weights,
        )
        scored.append(Scored(record=rec, score=s, capability_match=cap_match))

    scored.sort(key=lambda x: (-x.score, x.record.agent_id))
    return scored
