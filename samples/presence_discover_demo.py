"""
AGTP-Presence M3 slice-1 demo — DISCOVER as a real query, runnable.

A coordinator holds a governance signing key and hosts three auditor
agents with different trust tiers and behavioral scores. A requester runs
a capability query with a behavioral-trust floor and receives a ranked,
signed result set; the demo verifies the ans_signature and shows tamper
detection.

Run::

    python -m samples.presence_discover_demo
"""

from __future__ import annotations

import json
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from client.core_client import send_method
from presence import client as pclient
from presence.testing import InProcessCoordinator, make_doc
from server.signing import SigningService


def _doc(aid, name, tier, score):
    d = make_doc(aid, name, ["VALIDATE", "REPORT", "DESCRIBE"], tier=tier)
    d.trust_score = score
    d.trust_score_computed_at = "2026-07-21T00:00:00Z"
    return d


def main() -> int:
    failures = []

    def check(label, ok):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    print("=" * 68)
    print("AGTP-Presence M3 demo - ranked, trust-filtered, signed DISCOVER")
    print("=" * 68)

    svc = SigningService(private_key=Ed25519PrivateKey.generate())
    coord = InProcessCoordinator(
        [
            _doc("1" * 64, "auditor-gold", 1, 0.97),
            _doc("2" * 64, "auditor-silver", 2, 0.88),
            _doc("3" * 64, "auditor-bronze", 3, 0.35),
        ],
        signing_service=svc,
    ).start()

    try:
        print(f"\ncoordinator signing key id: {svc.key_id}")
        print("hosted: auditor-gold (t1,0.97), auditor-silver (t2,0.88), "
              "auditor-bronze (t3,0.35)")

        # Query: audit capability, require behavioral trust >= 0.5.
        r = send_method(
            None, coord.host, coord.port, "DISCOVER",
            path="/population?capability=validate&behavioral_trust_min=0.5"
                 "&scope_negotiate=true",
            use_tls=False,
        )
        body = json.loads(r.body_bytes.decode("utf-8"))

        print("\nDISCOVER capability=validate, behavioral_trust_min=0.5:")
        for res in body["results"]:
            print(f"  #{res['rank']}  {res['name']:16} "
                  f"tier={res['trust_tier']} "
                  f"score={res['score']}  "
                  f"needs={res.get('required_scope','')}")

        names = [r["name"] for r in body["results"]]
        check("bronze (0.35) filtered out by behavioral_trust_min",
              "auditor-bronze" not in names)
        check("results ranked gold > silver",
              names == ["auditor-gold", "auditor-silver"])
        check("total_matches counts pre-limit matches", body["total_matches"] == 2)

        # Verify the signature.
        check("ans_signature present", "ans_signature" in body)
        check("ans_signature verifies against the coordinator key",
              pclient.verify_discover_response(body, svc.public_key))

        # Tamper: promote bronze into the results and re-verify.
        body["results"].append({"agent_id": "3" * 64, "name": "auditor-bronze",
                                "rank": 3, "score": 0.99})
        check("tampered result set fails verification",
              not pclient.verify_discover_response(body, svc.public_key))
    finally:
        coord.stop()

    print("\n" + "=" * 68)
    if failures:
        print(f"DEMO FAILED - {len(failures)} assertion(s) did not hold.")
        return 1
    print("DEMO PASSED - ranked + trust-filtered + signed DISCOVER held.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
