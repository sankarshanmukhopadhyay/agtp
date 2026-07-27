"""
Adaptive scope partitioning (PDD §6.1 "Scoped overlays", §9 "At one billion
agents").

A bounded scope keeps total awareness affordable; when a scope's population
grows past a working-set threshold it must **split** so each overlay stays
inside the gossip-convergence envelope, and when sibling partitions go
sparse they **merge** back. This module is the split/merge decision logic:
an extendible-hashing-style trie over Agent-ID bit prefixes.

Members of a scope are placed into leaf partitions by successive bits of
their Agent-ID. A leaf that exceeds ``split_threshold`` divides into two
children on the next bit; two sibling leaves whose combined population falls
below ``merge_threshold`` collapse back into their parent. Each leaf has its
own overlay id (``overlay_id(base_scope + '#' + prefix)``) so a coordinator
can gossip/rendezvous per-leaf.

The production concern of *migrating* members across coordinators when a
split happens is governance/operations; this is the algorithm that drives
those decisions, kept pure and deterministic so it is fully testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from presence import scopes as _scopes
from dht.distance import to_int


@dataclass
class _Leaf:
    prefix: str                       # bit string, e.g. "", "0", "10"
    members: Set[str] = field(default_factory=set)


def _bit(agent_id: str, depth: int) -> str:
    """The ``depth``-th most-significant bit of the 256-bit Agent-ID."""
    value = to_int(agent_id)
    return "1" if (value >> (255 - depth)) & 1 else "0"


class PartitionManager:
    """
    Maintains the leaf partitions of one base scope under a split/merge
    policy. ``split_threshold`` and ``merge_threshold`` default to small
    values for tests; real deployments use 10k–100k (PDD §9). A merge only
    fires when *both* siblings are leaves and their combined size is below
    the merge threshold, which keeps split↔merge from thrashing around a
    single boundary (``merge_threshold`` should be well under
    ``split_threshold``).
    """

    def __init__(self, base_scope: str, *, split_threshold: int = 8,
                 merge_threshold: int = 3):
        if merge_threshold >= split_threshold:
            raise ValueError("merge_threshold must be < split_threshold")
        self.base_scope = base_scope
        self.split_threshold = split_threshold
        self.merge_threshold = merge_threshold
        self._leaves: Dict[str, _Leaf] = {"": _Leaf("")}

    # -- placement ----------------------------------------------------

    def _leaf_for(self, agent_id: str) -> _Leaf:
        depth = 0
        prefix = ""
        while prefix not in self._leaves:
            prefix = prefix + _bit(agent_id, depth)
            depth += 1
        return self._leaves[prefix]

    def add_member(self, agent_id: str) -> None:
        leaf = self._leaf_for(agent_id)
        leaf.members.add(agent_id)
        if len(leaf.members) > self.split_threshold:
            self._split(leaf)

    def remove_member(self, agent_id: str) -> None:
        leaf = self._leaf_for(agent_id)
        leaf.members.discard(agent_id)
        self._maybe_merge(leaf.prefix)

    # -- split / merge ------------------------------------------------

    def _split(self, leaf: _Leaf) -> None:
        depth = len(leaf.prefix)
        child0 = _Leaf(leaf.prefix + "0")
        child1 = _Leaf(leaf.prefix + "1")
        for aid in leaf.members:
            (child1 if _bit(aid, depth) == "1" else child0).members.add(aid)
        del self._leaves[leaf.prefix]
        self._leaves[child0.prefix] = child0
        self._leaves[child1.prefix] = child1
        # A skewed split can immediately overflow a child; recurse.
        for child in (child0, child1):
            if len(child.members) > self.split_threshold:
                self._split(child)

    def _maybe_merge(self, prefix: str) -> None:
        if prefix == "":
            return
        parent = prefix[:-1]
        sib = parent + ("1" if prefix[-1] == "0" else "0")
        # Both children must be leaves to merge them into the parent.
        if prefix not in self._leaves or sib not in self._leaves:
            return
        combined = len(self._leaves[prefix].members) + len(self._leaves[sib].members)
        if combined < self.merge_threshold:
            merged = _Leaf(parent)
            merged.members = self._leaves[prefix].members | self._leaves[sib].members
            del self._leaves[prefix]
            del self._leaves[sib]
            self._leaves[parent] = merged
            self._maybe_merge(parent)  # cascade upward if still sparse

    # -- views --------------------------------------------------------

    def leaf_prefixes(self) -> List[str]:
        return sorted(self._leaves)

    def leaf_count(self) -> int:
        return len(self._leaves)

    def scope_key(self, prefix: str) -> str:
        """The overlay id for a leaf partition."""
        label = self.base_scope if not prefix else f"{self.base_scope}#{prefix}"
        return _scopes.overlay_id(label)

    def leaf_scope_keys(self) -> Dict[str, str]:
        """Map each leaf prefix to its overlay id — the set of overlays this
        scope currently spans."""
        return {p: self.scope_key(p) for p in self._leaves}

    def members(self, prefix: str) -> Set[str]:
        leaf = self._leaves.get(prefix)
        return set(leaf.members) if leaf else set()

    def total_members(self) -> int:
        return sum(len(leaf.members) for leaf in self._leaves.values())
