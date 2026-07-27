"""
AGTP-Presence M2 slice-2 demo — gossip between two full nodes, runnable.

Two independent coordinators, each hosting one agent. Before gossip,
neither can discover the other's agent. After a single anti-entropy round,
both views converge: the agent announced at coordinator A is discoverable
at coordinator B and vice versa — with no central registry.

Run::

    python -m samples.presence_gossip_demo

Exit 0 means the two coordinators converged.
"""

from __future__ import annotations

import sys

from presence import client as pclient
from presence import gossip
from presence.testing import InProcessCoordinator, make_doc


ALPHA = "a" * 64
BETA = "b" * 64


def main() -> int:
    failures = []

    def check(label, ok):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    print("=" * 68)
    print("AGTP-Presence M2 demo - gossip between two full nodes")
    print("=" * 68)

    coord_a = InProcessCoordinator([make_doc(ALPHA, "alpha", ["VALIDATE"])]).start()
    coord_b = InProcessCoordinator([make_doc(BETA, "beta", ["SUMMARIZE"])]).start()

    def a_sees(cap):
        return pclient.discover_population_json(
            coord_a.host, coord_a.port, capability=cap, use_tls=False
        )["total_matches"]

    def b_sees(cap):
        return pclient.discover_population_json(
            coord_b.host, coord_b.port, capability=cap, use_tls=False
        )["total_matches"]

    try:
        print(f"\ncoordinator A on :{coord_a.port} hosts alpha (validate)")
        print(f"coordinator B on :{coord_b.port} hosts beta (summarize)")

        print("\nbefore gossip:")
        print(f"  A can discover 'summarize' (beta): {a_sees('summarize')} match(es)")
        print(f"  B can discover 'validate' (alpha):  {b_sees('validate')} match(es)")
        check("before gossip, A cannot see beta", a_sees("summarize") == 0)
        check("before gossip, B cannot see alpha", b_sees("validate") == 0)

        pushed, pulled = gossip.gossip_once(
            coord_a.store, coord_b.host, coord_b.port, use_tls=False
        )
        print(f"\none gossip round A<->B: pushed {pushed}, pulled {pulled}")

        print("\nafter gossip:")
        print(f"  A can discover 'summarize' (beta): {a_sees('summarize')} match(es)")
        print(f"  B can discover 'validate' (alpha):  {b_sees('validate')} match(es)")
        check("after gossip, A discovers beta", a_sees("summarize") == 1)
        check("after gossip, B discovers alpha", b_sees("validate") == 1)
    finally:
        coord_a.stop()
        coord_b.stop()

    print("\n" + "=" * 68)
    if failures:
        print(f"DEMO FAILED - {len(failures)} assertion(s) did not hold.")
        return 1
    print("DEMO PASSED - two coordinators converged via gossip, no central registry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
