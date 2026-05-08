"""
Substitution semantics.

Two ways an AMG method can advertise that it stands in for another:

  1. Per-method ``SubstitutionHint`` carried on ``AMGMethodSpec``.
     The author of method X declares ``X.substitutes_for = [Y]``.

  2. Equivalence classes (this module). A curated table of method
     name groups that the protocol treats as interchangeable in
     stated contexts. Every member can stand in for every other
     member when the conditions hold.

The negotiation policy in ``agtp.negotiation`` already uses a flat
synonym table for counter-proposals; this module supplies a richer
equivalence-class graph that future negotiation policies can consult.
The ecosystem catalog will own this list eventually; the seed below
captures the cases that show up most often.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Set


@dataclass(frozen=True)
class EquivalenceClass:
    """
    A named group of method verbs that may substitute for one another.

    ``members`` is treated as canonical, case-sensitive method names
    (uppercase ASCII). ``conditions`` is free-form prose; production
    policies can encode it more strictly when needed.
    """

    name: str
    members: FrozenSet[str]
    conditions: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.members, frozenset):
            object.__setattr__(self, "members", frozenset(self.members))


def _ec(name: str, members: Iterable[str], conditions: Optional[str] = None) -> EquivalenceClass:
    return EquivalenceClass(name=name, members=frozenset(members), conditions=conditions)


# Default substitution catalog. Keep this short and well-justified;
# every entry here trades precision for usability and so should map
# a real-world ambiguity, not just superficial spelling overlap.
DEFAULT_SUBSTITUTIONS: List[EquivalenceClass] = [
    _ec(
        "reservation",
        {"BOOK", "RESERVE", "SCHEDULE"},
        "calendar/booking contexts",
    ),
    _ec(
        "retrieval",
        {"FETCH", "GET", "RETRIEVE", "PULL"},
        "known-id retrieval (GET prohibited in AGTP)",
    ),
    _ec(
        "execution",
        {"EXECUTE", "RUN", "INVOKE"},
        "running named procedures",
    ),
    _ec(
        "validation",
        {"VALIDATE", "VERIFY", "CHECK"},
        "conformance checks",
    ),
    _ec(
        "creation",
        {"CREATE", "GENERATE", "MAKE"},
        "producing new content",
    ),
]


def find_substitutes(
    method_name: str,
    registry: Iterable[str],
    *,
    classes: Optional[Iterable[EquivalenceClass]] = None,
) -> List[str]:
    """
    Given a method name and a set of method names known to the
    server, return the substitutes that exist in the registry.

    The result is sorted, never includes ``method_name`` itself,
    and is empty when the method belongs to no known equivalence
    class.
    """
    upper = (method_name or "").upper()
    reg = {m.upper() for m in registry}
    cls_iter = list(classes) if classes is not None else DEFAULT_SUBSTITUTIONS

    matches: Set[str] = set()
    for ec in cls_iter:
        if upper in ec.members:
            matches.update(ec.members)
    matches.discard(upper)
    return sorted(m for m in matches if m in reg)


def conditions_for(
    method_name: str,
    *,
    classes: Optional[Iterable[EquivalenceClass]] = None,
) -> List[str]:
    """Return condition strings from every equivalence class that
    contains ``method_name``. Useful for human-facing tools that
    explain why a substitution is admissible."""
    upper = (method_name or "").upper()
    cls_iter = list(classes) if classes is not None else DEFAULT_SUBSTITUTIONS
    out: List[str] = []
    for ec in cls_iter:
        if upper in ec.members and ec.conditions:
            out.append(ec.conditions)
    return out


def index_by_member(
    classes: Optional[Iterable[EquivalenceClass]] = None,
) -> Dict[str, List[EquivalenceClass]]:
    """Build a member-name -> [containing classes] index. The index
    is unordered per-key; callers that care about deterministic order
    should sort downstream."""
    cls_iter = list(classes) if classes is not None else DEFAULT_SUBSTITUTIONS
    out: Dict[str, List[EquivalenceClass]] = {}
    for ec in cls_iter:
        for m in ec.members:
            out.setdefault(m, []).append(ec)
    return out


__all__ = [
    "DEFAULT_SUBSTITUTIONS",
    "EquivalenceClass",
    "conditions_for",
    "find_substitutes",
    "index_by_member",
]
