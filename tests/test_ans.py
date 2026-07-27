"""
AGTP ANS (M3 slice-2) tests — name resolution, ANS-brokered signed
DISCOVER, and lifecycle-driven registration / urgent deregistration.
"""

from __future__ import annotations

import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ans import client as ac
from ans.registration import make_lifecycle_hook, manifest_summary
from ans.store import NameStore
from client.core_client import send_method
from presence import client as pc
from presence.testing import InProcessCoordinator, make_doc
from server.signing import SigningService


AID = "a" * 64
BID = "b" * 64


# ---------------------------------------------------------------------------
# NameStore (unit).
# ---------------------------------------------------------------------------


class NameStoreTests(unittest.TestCase):
    def test_register_resolve_roundtrip(self):
        s = NameStore()
        s.register("auditor.acme", AID, {"trust_tier": 1})
        b = s.resolve("auditor.acme")
        self.assertIsNotNone(b)
        self.assertEqual(b.agent_id, AID)
        # case-insensitive
        self.assertIsNotNone(s.resolve("Auditor.ACME"))

    def test_resolve_by_id_and_dedup(self):
        s = NameStore()
        s.register("name.one", AID)
        self.assertEqual(s.resolve_by_id(AID).name, "name.one")

    def test_deregister_by_id_and_name(self):
        s = NameStore()
        s.register("n1", AID)
        s.register("n2", BID)
        self.assertTrue(s.deregister(agent_id=AID))
        self.assertIsNone(s.resolve("n1"))
        self.assertTrue(s.deregister(name="n2"))
        self.assertEqual(s.count(), 0)
        self.assertFalse(s.deregister(agent_id="zzz"))

    def test_register_is_idempotent_preserves_registered_at(self):
        s = NameStore()
        b1 = s.register("n", AID, now=100.0)
        b2 = s.register("n", AID, {"trust_tier": 2}, now=200.0)
        self.assertEqual(b2.registered_at, 100.0)
        self.assertEqual(b2.refreshed_at, 200.0)

    def test_stale_bindings(self):
        s = NameStore()
        s.register("n", AID, now=0.0)
        self.assertEqual(len(s.stale_bindings(max_age=60, now=30)), 0)
        self.assertEqual(len(s.stale_bindings(max_age=60, now=61)), 1)


# ---------------------------------------------------------------------------
# ANS over the wire.
# ---------------------------------------------------------------------------


class AnsWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = SigningService(private_key=Ed25519PrivateKey.generate())
        cls.ans = InProcessCoordinator([], ans=True, signing_service=cls.svc).start()
        ac.register_json(
            cls.ans.host, cls.ans.port, AID, "auditor.acme",
            {"supported_methods": ["VALIDATE", "REPORT"], "trust_tier": 1,
             "trust_score": 0.9, "owner_id": "acme.tld"},
            use_tls=False,
        )

    @classmethod
    def tearDownClass(cls):
        cls.ans.stop()

    def test_resolve_returns_signed_binding(self):
        body = ac.resolve_json(self.ans.host, self.ans.port, "auditor.acme", use_tls=False)
        self.assertEqual(body["binding"]["agent_id"], AID)
        self.assertIn("ans_signature", body)

    def test_resolve_unknown_is_404(self):
        r = ac.resolve(self.ans.host, self.ans.port, "nobody.here", use_tls=False)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(json.loads(r.body_bytes.decode())["error"]["code"], "name-not-found")

    def test_brokered_discover_is_ranked_and_signed(self):
        body = pc.discover_population_json(
            self.ans.host, self.ans.port, capability="validate", use_tls=False
        )
        self.assertEqual(body["total_matches"], 1)
        self.assertEqual(body["results"][0]["agent_id"], AID)
        self.assertIn("ans_signature", body)
        self.assertTrue(pc.verify_discover_response(body, self.svc.public_key))

    def test_deregister_removes_from_resolve_and_discover(self):
        # Use a throwaway agent so class-level fixtures stay intact.
        ac.register_json(
            self.ans.host, self.ans.port, BID, "temp.acme",
            {"supported_methods": ["SUMMARIZE"], "trust_tier": 2}, use_tls=False,
        )
        self.assertEqual(
            ac.resolve(self.ans.host, self.ans.port, "temp.acme", use_tls=False).status_code, 200
        )
        ac.deregister_json(self.ans.host, self.ans.port, BID, use_tls=False)
        self.assertEqual(
            ac.resolve(self.ans.host, self.ans.port, "temp.acme", use_tls=False).status_code, 404
        )
        gone = pc.discover_population_json(
            self.ans.host, self.ans.port, capability="summarize", use_tls=False
        )
        self.assertEqual(gone["total_matches"], 0)


# ---------------------------------------------------------------------------
# Lifecycle-driven registration.
# ---------------------------------------------------------------------------


class LifecycleRegistrationTests(unittest.TestCase):
    def test_hook_registers_on_active_deregisters_on_revoked(self):
        ans = InProcessCoordinator([], ans=True).start()
        try:
            hook = make_lifecycle_hook([ans.endpoint], use_tls=False)
            doc = make_doc(AID, "auditor.acme", ["VALIDATE"], tier=1)
            doc.trust_score = 0.9
            doc.trust_score_computed_at = "2026-07-21T00:00:00Z"

            # ACTIVATE -> REGISTER
            hook(doc, "activate", "active")
            self.assertEqual(
                ac.resolve(ans.host, ans.port, "auditor.acme", use_tls=False).status_code, 200
            )
            # REVOKE -> retired -> DEREGISTER (urgent, synchronous)
            hook(doc, "revoke", "retired")
            self.assertEqual(
                ac.resolve(ans.host, ans.port, "auditor.acme", use_tls=False).status_code, 404
            )
        finally:
            ans.stop()

    def test_manifest_summary_shape(self):
        doc = make_doc(AID, "auditor", ["VALIDATE", "REPORT"], tier=1, owner_id="acme.tld")
        doc.trust_score = 0.88
        doc.trust_score_computed_at = "2026-07-21T00:00:00Z"
        m = manifest_summary(doc)
        self.assertEqual(m["name"], "auditor")
        self.assertEqual(m["trust_tier"], 1)
        self.assertEqual(m["behavioral_trust_score"], 0.88)
        self.assertEqual(m["owner_id"], "acme.tld")

    def test_lifecycle_transition_invokes_hooks(self):
        # A recording hook installed on a coordinator that hosts a wildcard
        # agent; a wire DEACTIVATE (a real agent-lifecycle verb) must invoke
        # it — proving the transition -> hooks wiring, independent of ANS.
        doc = make_doc(AID, "worker", ["VALIDATE", "DEACTIVATE"], tier=1)
        doc.requires.wildcards = True
        doc.status = "active"
        coord = InProcessCoordinator([doc]).start()
        calls = []
        coord.registry.lifecycle_hooks.append(
            lambda d, ev, st: calls.append((ev, st))
        )
        try:
            r = send_method(
                AID, coord.host, coord.port, "DEACTIVATE",
                body=json.dumps({"reason": "test"}).encode("utf-8"),
                body_content_type="application/json", use_tls=False,
            )
            self.assertEqual(r.status_code, 200)
            self.assertIn(("deactivate", "suspended"), calls)
        finally:
            coord.stop()


if __name__ == "__main__":
    unittest.main()
