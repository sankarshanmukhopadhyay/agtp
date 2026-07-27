"""
Routing table: 256 k-buckets over the XOR metric.

A :class:`RoutingTable` belongs to one node and holds the peers it knows,
partitioned into buckets by :func:`dht.distance.bucket_index`. Each bucket
holds up to ``k`` peers (default 20). Kademlia keeps *live* peers over new
ones: a full bucket does not evict a known-good peer to admit a stranger —
it flags the least-recently-seen peer for a liveness check and only
replaces it if that peer is dead. We surface that decision rather than
performing the ping here (the caller owns the transport).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from dht.distance import bucket_index, xor_distance


@dataclass(frozen=True)
class NodeInfo:
    """A peer: its 256-bit node id and where to reach it."""

    node_id: str
    host: str
    port: int

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def to_dict(self) -> Dict[str, object]:
        return {"node_id": self.node_id, "host": self.host, "port": self.port}

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "NodeInfo":
        return cls(
            node_id=str(data["node_id"]),
            host=str(data["host"]),
            port=int(data["port"]),  # type: ignore[arg-type]
        )


class KBucket:
    """Up to ``k`` peers, ordered least-recently-seen first (index 0)."""

    def __init__(self, k: int = 20):
        self.k = k
        self._nodes: List[NodeInfo] = []

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node: NodeInfo) -> bool:
        return any(n.node_id == node.node_id for n in self._nodes)

    @property
    def nodes(self) -> List[NodeInfo]:
        return list(self._nodes)

    def add(self, node: NodeInfo) -> Optional[NodeInfo]:
        """
        Record ``node`` as most-recently-seen. Returns:
          * ``None`` if the node was added or refreshed (space existed or it
            was already known);
          * the least-recently-seen peer (the eviction candidate) if the
            bucket is full — the caller pings it and, if dead, calls
            :meth:`replace`.
        """
        for i, existing in enumerate(self._nodes):
            if existing.node_id == node.node_id:
                # Refresh: move to the most-recently-seen end.
                self._nodes.pop(i)
                self._nodes.append(node)
                return None
        if len(self._nodes) < self.k:
            self._nodes.append(node)
            return None
        return self._nodes[0]  # eviction candidate (LRS head)

    def replace(self, dead: NodeInfo, fresh: NodeInfo) -> bool:
        """Evict ``dead`` (a confirmed-dead LRS peer) for ``fresh``."""
        for i, existing in enumerate(self._nodes):
            if existing.node_id == dead.node_id:
                self._nodes.pop(i)
                self._nodes.append(fresh)
                return True
        return False


class RoutingTable:
    """A node's view of the network: 256 k-buckets keyed by XOR distance."""

    def __init__(self, node_id: str, k: int = 20):
        self.node_id = node_id
        self.k = k
        self._buckets: Dict[int, KBucket] = {}

    def _bucket_for(self, key: str) -> Optional[KBucket]:
        idx = bucket_index(self.node_id, key)
        if idx < 0:
            return None  # a node never stores itself
        return self._buckets.setdefault(idx, KBucket(self.k))

    def add_node(self, node: NodeInfo) -> Optional[NodeInfo]:
        """Insert/refresh a peer. Returns an eviction candidate when the
        target bucket is full (see :meth:`KBucket.add`), else None."""
        if node.node_id == self.node_id:
            return None
        bucket = self._bucket_for(node.node_id)
        if bucket is None:
            return None
        return bucket.add(node)

    def all_nodes(self) -> List[NodeInfo]:
        out: List[NodeInfo] = []
        for bucket in self._buckets.values():
            out.extend(bucket.nodes)
        return out

    def closest(self, key: str, count: Optional[int] = None) -> List[NodeInfo]:
        """The ``count`` (default ``k``) known peers nearest ``key`` by XOR."""
        count = self.k if count is None else count
        nodes = self.all_nodes()
        nodes.sort(key=lambda n: xor_distance(n.node_id, key))
        return nodes[:count]

    def __len__(self) -> int:
        return sum(len(b) for b in self._buckets.values())
