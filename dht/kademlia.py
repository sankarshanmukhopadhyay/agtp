"""
A Kademlia node: routing table + iterative lookup + bootstrap.

The lookup transport is injected as ``rpc(node, key) -> List[NodeInfo]`` so
the identical logic drives an in-process simulated network (tests) and the
AGTP ``LOCATE`` method over the wire (a live federation).

Lookup is the classic iterative node lookup: keep a shortlist of the k
closest peers seen, query the α closest un-queried, fold their answers back
in, and repeat until every one of the current k closest has been queried.
That converges on the globally closest reachable nodes in O(log N) rounds.

:meth:`iterative_locate_disjoint` is the S/Kademlia hardening: run several
lookups whose paths share no nodes, so a cluster of adversarial peers
sitting on one path cannot eclipse the target — an honest disjoint path
still reaches it. Content-derived node IDs (hashes) already deny an attacker
cheap IDs clustered around a victim key.
"""

from __future__ import annotations

import hashlib
from typing import Callable, List, Optional, Set

from dht.distance import xor_distance
from dht.routing import NodeInfo, RoutingTable


Rpc = Callable[[NodeInfo, str], List[NodeInfo]]


def _safe_rpc(rpc: Rpc, node: NodeInfo, key: str) -> List[NodeInfo]:
    try:
        result = rpc(node, key)
    except Exception:  # noqa: BLE001 - a dead/hostile peer must not abort the walk
        return []
    return result or []


class KademliaNode:
    def __init__(self, node_id: str, host: str, port: int,
                 *, k: int = 20, alpha: int = 3):
        self.info = NodeInfo(node_id=node_id, host=host, port=port)
        self.k = k
        self.alpha = alpha
        self.table = RoutingTable(node_id, k=k)

    @property
    def node_id(self) -> str:
        return self.info.node_id

    # -- table maintenance -------------------------------------------

    def observe(self, node: NodeInfo) -> None:
        """Learn of a peer (from an inbound request or an RPC answer)."""
        if node.node_id != self.info.node_id:
            self.table.add_node(node)

    # -- served side -------------------------------------------------

    def handle_locate(self, key: str, *, from_node: Optional[NodeInfo] = None
                      ) -> List[NodeInfo]:
        """FIND_NODE: return the k closest peers this node knows to ``key``.
        Observing the requester is a Kademlia side effect that keeps tables
        fresh."""
        if from_node is not None:
            self.observe(from_node)
        return self.table.closest(key, self.k)

    # -- lookup side -------------------------------------------------

    def iterative_locate(self, key: str, rpc: Rpc,
                         *, count: Optional[int] = None) -> List[NodeInfo]:
        """One iterative lookup, returning the ``count`` (default k) closest
        nodes found to ``key``."""
        found = self._lookup(key, rpc, exclude=set())
        return self._closest(found, key, count or self.k)

    def iterative_locate_disjoint(self, key: str, rpc: Rpc, *,
                                  disjoint: int = 3,
                                  count: Optional[int] = None) -> List[NodeInfo]:
        """
        S/Kademlia: ``disjoint`` node-disjoint lookups, unioned. A peer used
        on one path is excluded from the others, so no single set of
        adversarial nodes can block every path to the target.
        """
        used: Set[str] = set()
        merged = {}
        for _ in range(max(1, disjoint)):
            found = self._lookup(key, rpc, exclude=used)
            if not found:
                continue
            for n in found:
                merged[n.node_id] = n
                used.add(n.node_id)
        return self._closest(list(merged.values()), key, count or self.k)

    def _lookup(self, key: str, rpc: Rpc, *, exclude: Set[str]) -> List[NodeInfo]:
        # Seed the shortlist from the local table, skipping excluded peers
        # (path-disjointness for S/Kademlia).
        shortlist = {
            n.node_id: n
            for n in self.table.closest(key, self.k)
            if n.node_id not in exclude
        }
        queried: Set[str] = set()
        while True:
            topk = self._closest(list(shortlist.values()), key, self.k)
            unqueried = [n for n in topk if n.node_id not in queried]
            if not unqueried:
                break
            for node in unqueried[: self.alpha]:
                queried.add(node.node_id)
                exclude.add(node.node_id)
                for r in _safe_rpc(rpc, node, key):
                    self.observe(r)
                    if (
                        r.node_id != self.info.node_id
                        and r.node_id not in shortlist
                        and r.node_id not in exclude
                    ):
                        shortlist[r.node_id] = r
        return list(shortlist.values())

    def _closest(self, nodes: List[NodeInfo], key: str, count: int) -> List[NodeInfo]:
        uniq = {n.node_id: n for n in nodes}
        ordered = sorted(uniq.values(), key=lambda n: xor_distance(n.node_id, key))
        return ordered[:count]

    # -- join --------------------------------------------------------

    def bootstrap(self, seeds: List[NodeInfo], rpc: Rpc, *, refresh: int = 8) -> int:
        """
        Join the network via ``seeds``: record them, look up our own ID to
        learn nearby peers, then refresh ``refresh`` buckets by looking up
        keys spread across the ID space — so the routing table gains peers in
        distant buckets, not just our own neighborhood. Returns the table
        size after the join.

        Refresh keys are derived deterministically from our node id so a
        join is reproducible (no wall-clock randomness).
        """
        for seed in seeds:
            self.observe(seed)
        self.iterative_locate(self.info.node_id, rpc)
        for i in range(max(0, refresh)):
            key = hashlib.sha256(f"{self.info.node_id}:refresh:{i}".encode()).hexdigest()
            self.iterative_locate(key, rpc)
        return len(self.table)
