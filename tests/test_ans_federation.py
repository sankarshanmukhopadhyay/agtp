"""
AGTP ANS — cross-ANS federation: bilateral trust records and RESOLVE
forwarding with signature verification, single-hop loop prevention, and
re-signing.
"""

from __future__ import annotations

import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ans import client as ac
from ans.federation import (
    FederationTrust,
    establish_federation,
    federation_record,
    sign_federation_record,
    verify_federation_record,
)
from presence.envelope import verify_result_set
from presence.testing import InProcessCoordinator
from server.signing import SigningService


def _svc():
    return SigningService(private_key=Ed25519PrivateKey.generate())


# ---------------------------------------------------------------------------
# Federation record + trust (unit).
# ---------------------------------------------------------------------------


class FederationRecordTests(unittest.TestCase):
    def test_bilateral_signing_and_verify(self):
        a, b = _svc(), _svc()
        rec = federation_record("a:1", "b:2", established_at="t")
        sign_federation_record(rec, a)
        sign_federation_record(rec, b)
        self.assertTrue(verify_federation_record(rec, a.public_key, a.key_id))
        self.assertTrue(verify_federation_record(rec, b.public_key, b.key_id))

    def test_record_is_canonical_regardless_of_endpoint_order(self):
        r1 = federation_record("a:1", "b:2", established_at="t")
        r2 = federation_record("b:2", "a:1", established_at="t")
        self.assertEqual(r1, r2)

    def test_tampered_record_fails_verification(self):
        a = _svc()
        rec = federation_record("a:1", "b:2", established_at="t")
        sign_federation_record(rec, a)
        rec["forwarding_limit"] = 9999  # tamper after signing
        self.assertFalse(verify_federation_record(rec, a.public_key, a.key_id))

    def test_trust_store(self):
        t = FederationTrust()
        svc = _svc()
        t.add_peer("b:2", svc.public_key_raw(), svc.key_id)
        self.assertEqual(len(t), 1)
        self.assertIsNotNone(t.get("b:2"))
        self.assertTrue(t.revoke_peer("b:2"))
        self.assertEqual(len(t), 0)


# ---------------------------------------------------------------------------
# RESOLVE federation over the wire.
# ---------------------------------------------------------------------------


class ResolveFederationTests(unittest.TestCase):
    def setUp(self):
        self.skA, self.skB = _svc(), _svc()
        self.A = InProcessCoordinator([], ans=True, signing_service=self.skA).start()
        self.B = InProcessCoordinator([], ans=True, signing_service=self.skB).start()
        ac.register_json(
            self.B.host, self.B.port, "b" * 64, "auditor.bcorp",
            {"supported_methods": ["VALIDATE"], "trust_tier": 1}, use_tls=False,
        )

    def tearDown(self):
        self.A.stop()
        self.B.stop()

    def test_miss_without_federation(self):
        self.assertEqual(
            ac.resolve(self.A.host, self.A.port, "auditor.bcorp", use_tls=False).status_code,
            404,
        )

    def test_resolve_via_federation(self):
        establish_federation(self.A.registry, self.A.endpoint,
                             self.B.registry, self.B.endpoint, established_at="t")
        body = ac.resolve_json(self.A.host, self.A.port, "auditor.bcorp", use_tls=False)
        self.assertEqual(body["binding"]["agent_id"], "b" * 64)
        self.assertEqual(body["federation_path"], [self.B.endpoint])
        # A re-signed the merged result with its own governance key.
        self.assertTrue(
            verify_result_set(self.skA.public_key, body["binding"], body["ans_signature"])
        )

    def test_unknown_name_still_404_under_federation(self):
        establish_federation(self.A.registry, self.A.endpoint,
                             self.B.registry, self.B.endpoint, established_at="t")
        self.assertEqual(
            ac.resolve(self.A.host, self.A.port, "nobody.anywhere", use_tls=False).status_code,
            404,
        )

    def test_mutual_federation_does_not_loop(self):
        # Bilateral establish already pins both sides; a miss resolvable
        # nowhere must terminate (single-hop), not loop A->B->A->...
        establish_federation(self.A.registry, self.A.endpoint,
                             self.B.registry, self.B.endpoint, established_at="t")
        r = ac.resolve(self.A.host, self.A.port, "ghost.name", use_tls=False)
        self.assertEqual(r.status_code, 404)

    def test_forged_peer_signature_is_rejected(self):
        # A pins the WRONG key for B (impersonation / untrusted key). B's
        # real, correctly-signed response won't verify against the wrong
        # pinned key, so A refuses the federated result.
        wrong = _svc()
        self.A.registry.federation_trust.add_peer(
            self.B.endpoint, wrong.public_key_raw(), wrong.key_id)
        r = ac.resolve(self.A.host, self.A.port, "auditor.bcorp", use_tls=False)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
