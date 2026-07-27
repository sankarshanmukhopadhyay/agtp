"""
AGTP-Presence DHT — a Kademlia-style distributed hash table over the
federation of full-node coordinators (PDD §6.1 "DHT", §8 "By ID").

Nodes and keys share one 256-bit space (Canonical Agent-IDs are 256-bit
SHA-256 hashes, per AGTP-IDENTIFIERS), and closeness is XOR distance. Each
node keeps a routing table of 256 k-buckets (k=20); a by-ID lookup is an
iterative, α=3-parallel walk that converges on the k nodes closest to the
target in O(log N) hops. S/Kademlia hardening runs disjoint parallel
lookups so a handful of adversarial nodes on one path cannot eclipse the
target; content-derived node IDs (hashes) mean an attacker cannot cheaply
mint IDs clustered around a victim key.

Layout:
  * :mod:`dht.distance` — the XOR metric and bucket index.
  * :mod:`dht.routing`  — NodeInfo, KBucket, RoutingTable.
  * :mod:`dht.kademlia` — the node: handle_locate + iterative lookup + bootstrap.

The lookup transport is injected (a callable), so the same logic runs over
an in-process simulated network in tests and over the AGTP wire (the
``LOCATE`` method) in a live federation.
"""

from dht.distance import bucket_index, xor_distance
from dht.routing import KBucket, NodeInfo, RoutingTable

__all__ = ["bucket_index", "xor_distance", "KBucket", "NodeInfo", "RoutingTable"]
