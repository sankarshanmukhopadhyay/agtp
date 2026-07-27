"""
AGTP-Presence M4 slice-2 demo — cross-scope resolution, runnable.

Three coordinators share a DHT but do NOT gossip pairwise. A provider hosts
a "settlement" agent and advertises that scope to its rendezvous point. A
requester that never peered with the provider still discovers the agent by
routing through the DHT to the scope's rendezvous point and reading back the
provider — the federation-of-scoped-overlays model, where you reach a scope
you never joined without flooding the network.

Run::

    python -m samples.presence_discover_demo_crossscope
"""

from __future__ import annotations

import sys

from dht.client import bootstrap_over_wire
from presence import client as pc
from presence import crossscope as cs
from presence.testing import InProcessCoordinator, make_doc


AGENT = "b" * 64


def main() -> int:
    failures = []

    def check(label, ok):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    print("=" * 68)
    print("AGTP-Presence M4 demo - cross-scope rendezvous resolution")
    print("=" * 68)

    rzv = InProcessCoordinator([]).start()
    provider = InProcessCoordinator(
        [make_doc(AGENT, "settler", ["VALIDATE", "SETTLE"], tier=1)]
    ).start()
    requester = InProcessCoordinator([]).start()

    try:
        print(f"\nrendezvous hub :{rzv.port}")
        print(f"provider :{provider.port} hosts 'settler' (capability: settle)")
        print(f"requester :{requester.port} - NOT peered/gossiping with the provider")

        for c in (provider, requester):
            bootstrap_over_wire(c.dht_node, [rzv.endpoint], use_tls=False, refresh=8)
        bootstrap_over_wire(rzv.dht_node, [provider.endpoint], use_tls=False, refresh=8)

        n = cs.publish_scopes(provider.dht_node, provider.endpoint, ["settle"], use_tls=False)
        print(f"\nprovider PUBLISHed 'settle' to {n} rendezvous node(s)")

        local = pc.discover_population_json(
            requester.host, requester.port, capability="settle", use_tls=False
        )
        print(f"requester's LOCAL view of 'settle': {local['total_matches']} agent(s)")
        check("requester has no local/gossip knowledge of the agent",
              local["total_matches"] == 0)

        res = cs.cross_scope_discover(requester.dht_node, "settle", use_tls=False)
        print(f"\ncross-scope resolve 'settle':")
        print(f"  rendezvous returned providers: {res['providers']}")
        print(f"  discovered: {[r['name'] for r in res['results']]}")
        check("rendezvous located the provider coordinator",
              provider.endpoint in res["providers"])
        check("cross-scope discovery found the agent without gossip",
              AGENT in [r["agent_id"] for r in res["results"]])
    finally:
        for c in (rzv, provider, requester):
            c.stop()

    print("\n" + "=" * 68)
    if failures:
        print(f"DEMO FAILED - {len(failures)} assertion(s) did not hold.")
        return 1
    print("DEMO PASSED - reached a scope we never joined, via DHT rendezvous.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
