"""
AGTP ANS (M3 slice-2) demo — activate, resolve, discover, revoke, vanish.

An Agent Name Service holds a governance signing key. An agent's ACTIVATE
auto-registers it (via the platform lifecycle hook); a name resolves to its
Agent-ID with a signed binding; an ANS-brokered capability DISCOVER returns
ranked, signed results. On REVOKE the agent is deregistered within the
transition and disappears from both resolution and discovery — the
Revoked-agent-in-results governance failure never occurs.

Run::

    python -m samples.ans_demo
"""

from __future__ import annotations

import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ans import client as ac
from ans.registration import make_lifecycle_hook
from presence import client as pc
from presence.testing import InProcessCoordinator, make_doc
from server.signing import SigningService


AID = "a" * 64


def main() -> int:
    failures = []

    def check(label, ok):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    print("=" * 68)
    print("AGTP ANS demo - activate / resolve / discover / revoke")
    print("=" * 68)

    svc = SigningService(private_key=Ed25519PrivateKey.generate())
    ans = InProcessCoordinator([], ans=True, signing_service=svc).start()

    # The hosting platform's lifecycle hook, pointed at this ANS.
    hook = make_lifecycle_hook([ans.endpoint], use_tls=False)
    agent = make_doc(AID, "auditor.acme", ["VALIDATE", "REPORT", "DESCRIBE"], tier=1)
    agent.trust_score = 0.94
    agent.trust_score_computed_at = "2026-07-21T00:00:00Z"

    try:
        print(f"\nANS up (governance key {svc.key_id})")

        # 1. ACTIVATE -> auto-register at the ANS.
        hook(agent, "activate", "active")
        print("agent ACTIVATEd -> auto-registered at ANS")

        # 2. Resolve the name.
        res = ac.resolve_json(ans.host, ans.port, "auditor.acme", use_tls=False)
        print(f"\nRESOLVE 'auditor.acme' -> {res['binding']['agent_id'][:12]}...  "
              f"(signed: {'ans_signature' in res})")
        check("name resolves to the Agent-ID", res["binding"]["agent_id"] == AID)
        check("resolution binding is signed", "ans_signature" in res)

        # 3. ANS-brokered capability DISCOVER.
        disc = pc.discover_population_json(ans.host, ans.port, capability="validate", use_tls=False)
        top = disc["results"][0]
        print(f"\nDISCOVER capability=validate -> #{top['rank']} {top['name']} "
              f"score={top['score']} (signed: {'ans_signature' in disc})")
        check("brokered DISCOVER returns the agent", disc["total_matches"] == 1)
        check("brokered DISCOVER verifies against the ANS key",
              pc.verify_discover_response(disc, svc.public_key))

        # 4. REVOKE -> urgent deregistration; agent vanishes everywhere.
        hook(agent, "revoke", "retired")
        print("\nagent REVOKEd -> deregistered within the transition")
        r_after = ac.resolve(ans.host, ans.port, "auditor.acme", use_tls=False)
        d_after = pc.discover_population_json(ans.host, ans.port, capability="validate", use_tls=False)
        check("revoked agent no longer resolves (404)", r_after.status_code == 404)
        check("revoked agent absent from discovery", d_after["total_matches"] == 0)
    finally:
        ans.stop()

    print("\n" + "=" * 68)
    if failures:
        print(f"DEMO FAILED - {len(failures)} assertion(s) did not hold.")
        return 1
    print("DEMO PASSED - name resolution, signed brokered discovery, "
          "urgent deregistration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
