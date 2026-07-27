"""
AGTP ANS — cross-ANS federation demo, runnable.

Two independent naming authorities (ANS A and ANS B), each with its own
governance key. A name registered only in B's authority is unresolvable at A
until the two federate. Federation is bilateral and signed; once
established, A resolves B's name by forwarding — verifying B's signature
against the pinned key, then re-signing the result with its own key and
reporting the federation path.

Run::

    python -m samples.ans_federation_demo
"""

from __future__ import annotations

import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ans import client as ac
from ans.federation import establish_federation
from presence.envelope import verify_result_set
from presence.testing import InProcessCoordinator
from server.signing import SigningService


AGENT = "b" * 64


def main() -> int:
    failures = []

    def check(label, ok):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    print("=" * 68)
    print("AGTP ANS demo - cross-ANS federation")
    print("=" * 68)

    skA = SigningService(private_key=Ed25519PrivateKey.generate())
    skB = SigningService(private_key=Ed25519PrivateKey.generate())
    A = InProcessCoordinator([], ans=True, signing_service=skA).start()
    B = InProcessCoordinator([], ans=True, signing_service=skB).start()

    try:
        print(f"\nANS A :{A.port} (key {skA.key_id})")
        print(f"ANS B :{B.port} (key {skB.key_id})")

        ac.register_json(B.host, B.port, AGENT, "auditor.bcorp",
                         {"supported_methods": ["VALIDATE"], "trust_tier": 1},
                         use_tls=False)
        print("\n'auditor.bcorp' registered in B's authority")

        before = ac.resolve(A.host, A.port, "auditor.bcorp", use_tls=False)
        print(f"A resolves 'auditor.bcorp' before federation -> {before.status_code}")
        check("name in B is unresolvable at A before federation",
              before.status_code == 404)

        rec = establish_federation(A.registry, A.endpoint, B.registry, B.endpoint,
                                   established_at="2026-07-22T00:00:00Z")
        print(f"\nbilateral federation established; record signed by "
              f"{sorted(rec['signatures'])}")
        check("federation record carries both parties' signatures",
              set(rec["signatures"]) == {skA.key_id, skB.key_id})

        after = ac.resolve_json(A.host, A.port, "auditor.bcorp", use_tls=False)
        print(f"\nA resolves 'auditor.bcorp' after federation:")
        print(f"  agent_id: {after['binding']['agent_id'][:12]}...")
        print(f"  federation_path: {after.get('federation_path')}")
        check("A resolves B's name via federation",
              after["binding"]["agent_id"] == AGENT)
        check("federation_path records the peer authority",
              after.get("federation_path") == [B.endpoint])
        check("A re-signed the federated result with its own key",
              verify_result_set(skA.public_key, after["binding"], after["ans_signature"]))
    finally:
        A.stop()
        B.stop()

    print("\n" + "=" * 68)
    if failures:
        print(f"DEMO FAILED - {len(failures)} assertion(s) did not hold.")
        return 1
    print("DEMO PASSED - resolved a name across naming authorities via federation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
