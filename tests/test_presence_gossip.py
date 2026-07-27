"""
AGTP-Presence M2 slice-2 tests — gossip anti-entropy between full-node
coordinators, and multi-overlay membership query filters.
"""

from __future__ import annotations

import unittest

from presence import client as pclient
from presence import gossip
from presence.records import PresenceRecord
from presence.store import PresenceStore
from presence.testing import InProcessCoordinator, make_doc


ALPHA = "a" * 64
BETA = "b" * 64


# ---------------------------------------------------------------------------
# Gossip — store level (deterministic, ttl=0 so nothing ages out mid-test).
# ---------------------------------------------------------------------------


class GossipStoreTests(unittest.TestCase):
    def _stores(self):
        a, b = PresenceStore(), PresenceStore()
        a.announce(a.build_record(make_doc(ALPHA, "alpha", ["VALIDATE"]), ttl_seconds=0))
        b.announce(b.build_record(make_doc(BETA, "beta", ["SUMMARIZE"]), ttl_seconds=0))
        return a, b

    def test_one_round_converges_both_sides(self):
        a, b = self._stores()
        # A gossips to B: B merges A's push in apply_replicate; A merges
        # the delta B returns.
        req = gossip.build_replicate_request(a)
        reply = gossip.apply_replicate(b, req)
        for raw in reply["records"]:
            a.merge_record(PresenceRecord.from_gossip_dict(raw))
        self.assertEqual(a.count(), 2)
        self.assertEqual(b.count(), 2)
        self.assertIsNotNone(a.probe(BETA))
        self.assertIsNotNone(b.probe(ALPHA))

    def test_conflict_keeps_most_recent_announce(self):
        s = PresenceStore()
        newer = s.build_record(make_doc(ALPHA, "alpha-v2", ["VALIDATE"]), ttl_seconds=0)
        newer.announced_at_epoch = 2000.0
        s.announce(newer)
        older = s.build_record(make_doc(ALPHA, "alpha-v1", ["VALIDATE"]), ttl_seconds=0)
        older.announced_at_epoch = 1000.0
        self.assertFalse(s.merge_record(older))  # older loses
        self.assertEqual(s.probe(ALPHA).result_entry["name"], "alpha-v2")
        # A strictly-newer record wins.
        newest = s.build_record(make_doc(ALPHA, "alpha-v3", ["VALIDATE"]), ttl_seconds=0)
        newest.announced_at_epoch = 3000.0
        self.assertTrue(s.merge_record(newest))
        self.assertEqual(s.probe(ALPHA).result_entry["name"], "alpha-v3")

    def test_records_peer_needs_is_the_delta(self):
        a, _ = self._stores()
        # Peer that already has ALPHA at the same epoch needs nothing.
        dig = a.digest()
        self.assertEqual(a.records_peer_needs(dig), [])
        # Peer with an empty digest needs everything.
        self.assertEqual(len(a.records_peer_needs({})), 1)


# ---------------------------------------------------------------------------
# Gossip — over the wire, two live coordinators.
# ---------------------------------------------------------------------------


class GossipWireTests(unittest.TestCase):
    def test_agent_announced_at_A_is_discoverable_at_B(self):
        coord_a = InProcessCoordinator([make_doc(ALPHA, "alpha", ["VALIDATE"])]).start()
        coord_b = InProcessCoordinator([make_doc(BETA, "beta", ["SUMMARIZE"])]).start()
        try:
            # Before gossip: each coordinator only knows its own agent.
            a_before = pclient.discover_population_json(
                coord_a.host, coord_a.port, capability="summarize", use_tls=False
            )
            self.assertEqual(a_before["total_matches"], 0)

            # One round from A to B reconciles both.
            pushed, pulled = gossip.gossip_once(
                coord_a.store, coord_b.host, coord_b.port, use_tls=False
            )
            self.assertGreaterEqual(pushed, 1)
            self.assertGreaterEqual(pulled, 1)

            # A now discovers B's agent (beta / summarize)...
            a_after = pclient.discover_population_json(
                coord_a.host, coord_a.port, capability="summarize", use_tls=False
            )
            self.assertEqual(a_after["total_matches"], 1)
            self.assertEqual(a_after["results"][0]["agent_id"], BETA)

            # ...and B discovers A's agent (alpha / validate).
            b_after = pclient.discover_population_json(
                coord_b.host, coord_b.port, capability="validate", use_tls=False
            )
            self.assertEqual(b_after["total_matches"], 1)
            self.assertEqual(b_after["results"][0]["agent_id"], ALPHA)
        finally:
            coord_a.stop()
            coord_b.stop()

    def test_gossip_round_selects_and_contacts_peers(self):
        coord_b = InProcessCoordinator([make_doc(BETA, "beta", ["SUMMARIZE"])]).start()
        coord_a = InProcessCoordinator(
            [make_doc(ALPHA, "alpha", ["VALIDATE"])], peers=[coord_b.endpoint]
        ).start()
        try:
            contacted = gossip.gossip_round(
                coord_a.store, coord_a.registry.presence_peers,
                fanout=3, use_tls=False,
            )
            self.assertEqual(contacted, 1)
            self.assertIsNotNone(coord_a.store.probe(BETA))
        finally:
            coord_a.stop()
            coord_b.stop()


# ---------------------------------------------------------------------------
# Multi-overlay membership filters.
# ---------------------------------------------------------------------------


class MultiOverlayTests(unittest.TestCase):
    def _store(self):
        s = PresenceStore()
        s.announce(s.build_record(
            make_doc("1" * 64, "t1-acme", ["VALIDATE"], tier=1, owner_id="acme.tld")))
        s.announce(s.build_record(
            make_doc("2" * 64, "t3-acme", ["VALIDATE"], tier=3, owner_id="acme.tld")))
        s.announce(s.build_record(
            make_doc("3" * 64, "t1-other", ["VALIDATE"], tier=1, owner_id="other.tld")))
        return s

    def test_tier_filter(self):
        s = self._store()
        self.assertEqual(len(s.query_population(tier=1)), 2)
        self.assertEqual(len(s.query_population(tier=3)), 1)

    def test_owner_domain_filter(self):
        s = self._store()
        self.assertEqual(len(s.query_population(owner_domain="acme.tld")), 2)
        self.assertEqual(len(s.query_population(owner_domain="other.tld")), 1)

    def test_composed_filters_and(self):
        s = self._store()
        res = s.query_population(capability="validate", tier=1, owner_domain="acme.tld")
        self.assertEqual([r.result_entry["name"] for r in res], ["t1-acme"])

    def test_multi_overlay_over_wire(self):
        coord = InProcessCoordinator([
            make_doc("1" * 64, "t1-acme", ["VALIDATE"], tier=1, owner_id="acme.tld"),
            make_doc("2" * 64, "t3-acme", ["VALIDATE"], tier=3, owner_id="acme.tld"),
        ]).start()
        try:
            body = pclient.discover_population_json(
                coord.host, coord.port, capability="validate", use_tls=False,
            )
            self.assertEqual(body["total_matches"], 2)
            # A tier filter rides the query string alongside capability.
            import json
            from client.core_client import send_method
            r = send_method(
                None, coord.host, coord.port, "DISCOVER",
                path="/population?capability=validate&tier=1", use_tls=False,
            )
            filtered = json.loads(r.body_bytes.decode("utf-8"))
            self.assertEqual(filtered["total_matches"], 1)
            self.assertEqual(filtered["query"]["tier"], 1)
        finally:
            coord.stop()


if __name__ == "__main__":
    unittest.main()
