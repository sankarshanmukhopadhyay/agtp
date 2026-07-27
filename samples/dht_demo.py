"""
AGTP-Presence M4 slice-1 demo — by-ID routing over a Kademlia DHT.

A federation of coordinators joins a DHT via a seed. A by-ID LOCATE for an
arbitrary Agent-ID, issued from one coordinator, walks the overlay
(α-parallel, O(log N) hops) and returns the coordinators closest to that
ID — the ones responsible for holding the agent's presence record. The demo
confirms the wire lookup returns the same closest node a global view would.

Run::

    python -m samples.dht_demo
"""

from __future__ import annotations

import hashlib
import sys

from dht.client import bootstrap_over_wire, locate_over_wire, ping
from dht.distance import xor_distance
from presence.testing import InProcessCoordinator


def nid(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def main() -> int:
    failures = []

    def check(label, ok):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    print("=" * 68)
    print("AGTP-Presence M4 demo - by-ID routing over a Kademlia DHT")
    print("=" * 68)

    N = 12
    coords = [InProcessCoordinator([]).start() for _ in range(N)]
    try:
        seed = coords[0]
        print(f"\n{N} coordinators up; seed at :{seed.port} "
              f"(node {seed.dht_node.node_id[:10]}...)")

        # PING learns the seed's node id; then everyone joins via the seed.
        self_ping = ping(seed.host, seed.port, use_tls=False)
        check("PING returns the seed's node identity",
              self_ping is not None and self_ping.node_id == seed.dht_node.node_id)

        # Join via the seed, then stabilize: re-bootstrap any node whose
        # table is short after a transient RPC loss (real nodes stabilize
        # the same way) so the demo is robust under socket load.
        endpoints = [c.endpoint for c in coords]
        for _ in range(10):
            for c in coords:
                if len(c.dht_node.table) < N - 1:
                    bootstrap_over_wire(c.dht_node, endpoints, use_tls=False, refresh=6)
            if all(len(c.dht_node.table) == N - 1 for c in coords):
                break
        sizes = [len(c.dht_node.table) for c in coords]
        print(f"routing tables populated: sizes {sizes}")
        check("every coordinator learned the federation",
              all(s >= N - 1 for s in sizes))

        # A by-ID LOCATE from one coordinator for an arbitrary Agent-ID.
        agent_id = nid("agtp://some-target-agent")
        origin = coords[N // 2]
        result = locate_over_wire(origin.dht_node, agent_id, use_tls=False)
        node_ids = [c.dht_node.node_id for c in coords]
        true_closest = sorted(node_ids, key=lambda x: xor_distance(x, agent_id))[0]

        print(f"\nLOCATE {agent_id[:12]}... from :{origin.port}")
        print(f"  closest coordinator found: {result[0].node_id[:10]}...  "
              f"(:{result[0].port})")
        print(f"  global-view closest:       {true_closest[:10]}...")
        check("wire LOCATE returns the globally-closest coordinator",
              bool(result) and result[0].node_id == true_closest)
        check("LOCATE returned a full neighbor set", len(result) >= N - 1)
    finally:
        for c in coords:
            c.stop()

    print("\n" + "=" * 68)
    if failures:
        print(f"DEMO FAILED - {len(failures)} assertion(s) did not hold.")
        return 1
    print("DEMO PASSED - by-ID routing converges across the federation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
