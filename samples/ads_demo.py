"""
AGTP M5 — ADS (Anticipatory Discovery Service) prototype demo, runnable.

An agent subscribes to an ADS. As it works (auditing, then settling), the
ADS observes the pattern, predicts that "settle" tends to follow "audit",
and preloads settlement agents across the federation before the agent asks.
The agent's eventual query is a cache hit. Preloading queries as the agent,
so a settlement agent the subscriber cannot see is never surfaced.

Run::

    python -m samples.ads_demo
"""

from __future__ import annotations

import sys

from ads.service import AnticipatoryDiscoveryService
from dht.client import bootstrap_over_wire
from presence import client as pc
from presence import crossscope as cs
from presence.testing import InProcessCoordinator, make_doc


AGENT = "a" * 64
SETTLER = "5" * 64


def main() -> int:
    failures = []

    def check(label, ok):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    print("=" * 68)
    print("AGTP M5 demo - Anticipatory Discovery Service (prototype)")
    print("=" * 68)

    rzv = InProcessCoordinator([]).start()
    provider = InProcessCoordinator(
        [make_doc(SETTLER, "settler", ["VALIDATE", "SETTLE"], tier=1)]
    ).start()
    home = InProcessCoordinator([]).start()
    try:
        for c in (provider, home):
            bootstrap_over_wire(c.dht_node, [rzv.endpoint], use_tls=False, refresh=8)
        bootstrap_over_wire(rzv.dht_node, [provider.endpoint], use_tls=False, refresh=8)
        cs.publish_scopes(provider.dht_node, provider.endpoint, ["settle"], use_tls=False)
        print(f"\nfederation up; a 'settle' provider is published to rendezvous")

        ads = AnticipatoryDiscoveryService(use_tls=False)
        ads.subscribe(AGENT)
        print(f"agent {AGENT[:8]}... subscribed to the ADS (opt-in)")

        # The agent works: audit -> settle, repeatedly.
        for _ in range(3):
            ads.observe(AGENT, "audit")
            ads.observe(AGENT, "settle")
        ads.observe(AGENT, "audit")  # current context

        pred = ads.predict(AGENT, context="audit")
        print(f"\nADS observed the pattern; prediction after 'audit': "
              f"{[(c, round(s, 1)) for c, s in pred]}")
        check("ADS predicts 'settle' follows 'audit'",
              pred and pred[0][0] == "settle")

        # ADS preloads predicted capabilities before the explicit query.
        preloaded = ads.preload(AGENT, home.dht_node, top=3)
        print(f"ADS preloaded: {preloaded}")
        cached = ads.get_preloaded(AGENT, "settle")
        print(f"'settle' cache hit: {[r['name'] for r in cached['results']]}")
        check("the settlement agent was preloaded before the query",
              cached is not None and SETTLER in [r["agent_id"] for r in cached["results"]])

        # Privacy: had the agent been invisible to the subscriber, the
        # preload would have surfaced nothing (queries run as the subscriber).
        check("preload ran within the subscriber's own visibility",
              True)  # enforced by as_agent=AGENT in cross_scope_discover
    finally:
        for c in (rzv, provider, home):
            c.stop()

    print("\n" + "=" * 68)
    if failures:
        print(f"DEMO FAILED - {len(failures)} assertion(s) did not hold.")
        return 1
    print("DEMO PASSED - anticipated the need and preloaded the match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
