"""
AGTP-Presence M3 slice-1 tests — the DISCOVER query surface: composite
ranking, trust filtering, the ans_signature signed envelope, and the
discovery:query Authority-Scope gate.
"""

from __future__ import annotations

import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from client.core_client import send_method
from presence import client as pclient
from presence import ranking
from presence.records import PresenceRecord, Visibility
from presence.envelope import sign_result_set, verify_result_set
from presence.testing import InProcessCoordinator, make_doc
from server.signing import SigningService


def _doc(aid, name, methods, *, tier=1, score=None, owner_id=""):
    d = make_doc(aid, name, methods, tier=tier, owner_id=owner_id)
    if score is not None:
        d.trust_score = score
        d.trust_score_computed_at = "2026-07-21T00:00:00Z"
    return d


HIGH = "1" * 64
MID = "2" * 64
LOW = "3" * 64


# ---------------------------------------------------------------------------
# Ranking (unit).
# ---------------------------------------------------------------------------


class RankingUnitTests(unittest.TestCase):
    def test_tier_norm(self):
        self.assertEqual(ranking.tier_norm(1), 1.0)
        self.assertAlmostEqual(ranking.tier_norm(2), 2 / 3.0)
        self.assertAlmostEqual(ranking.tier_norm(3), 1 / 3.0)
        self.assertEqual(ranking.tier_norm(None), 0.0)

    def test_composite_weights(self):
        # 0.3*1.0 + 0.4*0.5 + 0.3*1.0 = 0.8
        s = ranking.composite_score(tier=1, behavioral=0.5, capability_match=1.0)
        self.assertAlmostEqual(s, 0.8)

    def test_capability_match_exact_vs_intent(self):
        caps = {"validate", "report", "analysis"}
        self.assertEqual(ranking.capability_match_score(caps, capability="validate"), 1.0)
        # intent tokens: 'audit' miss, 'analysis' hit -> 1/2
        self.assertEqual(
            ranking.capability_match_score(caps, intent="audit analysis"), 0.5
        )
        self.assertEqual(ranking.capability_match_score(caps), 1.0)

    def test_rank_order_and_stable_tie_break(self):
        def rec(aid, tier, score):
            return PresenceRecord(
                agent_id=aid,
                result_entry={"trust_tier": tier, "behavioral_trust_score": score,
                              "capabilities": ["validate"]},
                visibility=Visibility(),
            )
        recs = [rec("b" * 64, 2, 0.9), rec("a" * 64, 1, 0.5), rec("c" * 64, 1, 0.5)]
        out = ranking.rank_records(recs, lambda r: {"validate"}, capability="validate")
        # tier-1 0.5 pair scores equal; tie breaks on agent_id ('a' < 'c').
        # tier-1 (0.3+0.2+0.3=0.8) beats tier-2 0.9 (0.2+0.36+0.3=0.86)? -> 0.86 > 0.8
        ids = [s.record.agent_id[0] for s in out]
        self.assertEqual(ids, ["b", "a", "c"])


# ---------------------------------------------------------------------------
# ans_signature (unit).
# ---------------------------------------------------------------------------


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.svc = SigningService(private_key=Ed25519PrivateKey.generate())
        self.results = [{"agent_id": "a", "rank": 1}, {"agent_id": "b", "rank": 2}]

    def test_sign_and_verify(self):
        sig = sign_result_set(self.svc, self.results)
        self.assertEqual(sig["algorithm"], "EdDSA")
        self.assertEqual(sig["key_id"], self.svc.key_id)
        self.assertTrue(verify_result_set(self.svc.public_key, self.results, sig))

    def test_tamper_detected(self):
        sig = sign_result_set(self.svc, self.results)
        tampered = [dict(r) for r in self.results]
        tampered[0]["rank"] = 99
        self.assertFalse(verify_result_set(self.svc.public_key, tampered, sig))

    def test_unsigned_and_malformed_rejected(self):
        self.assertFalse(verify_result_set(self.svc.public_key, self.results, None))
        self.assertFalse(verify_result_set(self.svc.public_key, self.results, {}))
        self.assertFalse(
            verify_result_set(self.svc.public_key, self.results,
                              {"algorithm": "RS256", "value": "x"})
        )


# ---------------------------------------------------------------------------
# DISCOVER over the wire.
# ---------------------------------------------------------------------------


class DiscoverWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = SigningService(private_key=Ed25519PrivateKey.generate())
        cls.coord = InProcessCoordinator(
            [
                _doc(HIGH, "high", ["VALIDATE", "DESCRIBE"], tier=1, score=0.95),
                _doc(MID, "mid", ["VALIDATE"], tier=2, score=0.90),
                _doc(LOW, "low", ["VALIDATE"], tier=3, score=0.20),
            ],
            signing_service=cls.svc,
        ).start()

    @classmethod
    def tearDownClass(cls):
        cls.coord.stop()

    def _pop(self, path):
        r = send_method(
            None, self.coord.host, self.coord.port, "DISCOVER",
            path=path, use_tls=False,
        )
        return r, json.loads(r.body_bytes.decode("utf-8"))

    def test_results_ranked_by_composite_score(self):
        _, body = self._pop("/population?capability=validate")
        names = [r["name"] for r in body["results"]]
        self.assertEqual(names, ["high", "mid", "low"])
        # scores strictly descending
        scores = [r["score"] for r in body["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertIn("capability_match_score", body["results"][0])

    def test_response_is_signed_and_verifies(self):
        body = pclient.discover_population_json(
            self.coord.host, self.coord.port, capability="validate", use_tls=False
        )
        self.assertIn("ans_signature", body)
        self.assertTrue(
            pclient.verify_discover_response(body, self.svc.public_key)
        )

    def test_tampered_response_fails_verification(self):
        body = pclient.discover_population_json(
            self.coord.host, self.coord.port, capability="validate", use_tls=False
        )
        body["results"][0]["name"] = "IMPOSTOR"
        self.assertFalse(
            pclient.verify_discover_response(body, self.svc.public_key)
        )

    def test_behavioral_trust_min_filters(self):
        _, body = self._pop("/population?capability=validate&behavioral_trust_min=0.5")
        names = {r["name"] for r in body["results"]}
        self.assertEqual(names, {"high", "mid"})  # low (0.20) excluded

    def test_trust_tier_min_filters(self):
        _, body = self._pop("/population?capability=validate&trust_tier_min=2")
        names = {r["name"] for r in body["results"]}
        self.assertEqual(names, {"high", "mid"})  # tier-3 low excluded

    def test_limit_reports_total_vs_returned(self):
        _, body = self._pop("/population?capability=validate&limit=1")
        self.assertEqual(body["total_matches"], 3)   # all matches
        self.assertEqual(body["returned"], 1)        # after limit
        self.assertEqual(body["results"][0]["name"], "high")

    def test_scope_negotiate_adds_required_scope(self):
        _, body = self._pop("/population?capability=validate&scope_negotiate=true")
        top = body["results"][0]
        self.assertIn("required_scope", top)
        self.assertIn("validate:invoke", top["required_scope"])

    def test_bad_numeric_param_is_400(self):
        r, body = self._pop("/population?capability=validate&trust_tier_min=abc")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(body["error"]["code"], "population-bad-trust-tier-min")


class DiscoveryScopeGateTests(unittest.TestCase):
    def test_scope_required_when_enabled(self):
        coord = InProcessCoordinator(
            [_doc(HIGH, "high", ["VALIDATE"], tier=1)],
            require_discovery_scope=True,
        ).start()
        try:
            # No Authority-Scope → 262.
            r = send_method(
                None, coord.host, coord.port, "DISCOVER",
                path="/population?capability=validate", use_tls=False,
            )
            self.assertEqual(r.status_code, 262)
            body = json.loads(r.body_bytes.decode("utf-8"))
            self.assertEqual(body["error"]["required_scope"], "discovery:query")

            # With the scope → 200.
            r2 = send_method(
                None, coord.host, coord.port, "DISCOVER",
                path="/population?capability=validate", use_tls=False,
                extra_headers={"Authority-Scope": "discovery:query agents:read"},
            )
            self.assertEqual(r2.status_code, 200)
        finally:
            coord.stop()

    def test_scope_not_required_by_default(self):
        coord = InProcessCoordinator([_doc(HIGH, "high", ["VALIDATE"], tier=1)]).start()
        try:
            r = send_method(
                None, coord.host, coord.port, "DISCOVER",
                path="/population?capability=validate", use_tls=False,
            )
            self.assertEqual(r.status_code, 200)
        finally:
            coord.stop()


if __name__ == "__main__":
    unittest.main()
