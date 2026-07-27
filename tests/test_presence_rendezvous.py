"""
AGTP-Presence M4 slice-2 tests — cross-scope rendezvous resolution: the
rendezvous index, PUBLISH / DISCOVER /providers over the wire, and
end-to-end cross-scope discovery that needs no gossip peering.
"""

from __future__ import annotations

import json
import unittest

from client.core_client import send_method
from dht.client import bootstrap_over_wire
from presence import client as pc
from presence import crossscope as cs
from presence.rendezvous import RendezvousIndex
from presence.testing import InProcessCoordinator, make_doc


# ---------------------------------------------------------------------------
# RendezvousIndex (unit).
# ---------------------------------------------------------------------------


class RendezvousIndexTests(unittest.TestCase):
    def test_register_and_read(self):
        idx = RendezvousIndex()
        idx.register_provider("scope1", "host:1", label="capability:settle")
        idx.register_provider("scope1", "host:2")
        self.assertEqual(idx.providers("scope1"), ["host:1", "host:2"])
        self.assertEqual(idx.label("scope1"), "capability:settle")
        self.assertEqual(idx.providers("unknown"), [])

    def test_idempotent_provider(self):
        idx = RendezvousIndex()
        idx.register_provider("s", "host:1")
        idx.register_provider("s", "host:1")
        self.assertEqual(idx.providers("s"), ["host:1"])

    def test_ttl_expiry(self):
        idx = RendezvousIndex(ttl=60)
        idx.register_provider("s", "host:1", now=0.0)
        self.assertEqual(idx.providers("s", now=30), ["host:1"])
        self.assertEqual(idx.providers("s", now=61), [])
        self.assertEqual(idx.scope_count(), 0)  # emptied scope pruned


# ---------------------------------------------------------------------------
# PUBLISH / DISCOVER /providers over the wire.
# ---------------------------------------------------------------------------


class RendezvousWireTests(unittest.TestCase):
    def test_publish_then_read_providers(self):
        coord = InProcessCoordinator([]).start()
        try:
            key = cs.scope_key_for_capability("settle")
            body = json.dumps({
                "scope_key": key, "endpoint": "10.0.0.1:4480", "label": "capability:settle",
            }).encode("utf-8")
            r = send_method(None, coord.host, coord.port, "PUBLISH",
                            body=body, body_content_type="application/json", use_tls=False)
            self.assertEqual(r.status_code, 200)

            import urllib.parse
            q = urllib.parse.quote(key, safe="")
            r2 = send_method(None, coord.host, coord.port, "DISCOVER",
                             path=f"/providers?scope_key={q}", use_tls=False)
            body2 = json.loads(r2.body_bytes.decode())
            self.assertIn("10.0.0.1:4480", body2["providers"])
            self.assertEqual(body2["label"], "capability:settle")
        finally:
            coord.stop()

    def test_publish_missing_fields_is_400(self):
        coord = InProcessCoordinator([]).start()
        try:
            r = send_method(None, coord.host, coord.port, "PUBLISH",
                            body=b"{}", body_content_type="application/json", use_tls=False)
            self.assertEqual(r.status_code, 400)
        finally:
            coord.stop()


class ScopeKeyTests(unittest.TestCase):
    def test_scope_key_is_deterministic(self):
        self.assertEqual(
            cs.scope_key_for_capability("settle"),
            cs.scope_key_for_capability("SETTLE"),  # capability lowercased
        )
        self.assertNotEqual(
            cs.scope_key_for_capability("settle"),
            cs.scope_key_for_capability("audit"),
        )


# ---------------------------------------------------------------------------
# End-to-end cross-scope discovery (no gossip peering).
# ---------------------------------------------------------------------------


class CrossScopeTests(unittest.TestCase):
    def test_requester_finds_agents_in_a_scope_it_never_joined(self):
        rzv = InProcessCoordinator([]).start()
        provider = InProcessCoordinator(
            [make_doc("b" * 64, "settler", ["VALIDATE", "SETTLE"], tier=1)]
        ).start()
        requester = InProcessCoordinator([]).start()
        try:
            # All three share the DHT (join via the rendezvous hub).
            for c in (provider, requester):
                bootstrap_over_wire(c.dht_node, [rzv.endpoint], use_tls=False, refresh=8)
            bootstrap_over_wire(rzv.dht_node, [provider.endpoint], use_tls=False, refresh=8)

            # The provider advertises its scope; the requester never peers
            # with it (no gossip).
            published = cs.publish_scopes(
                provider.dht_node, provider.endpoint, ["settle"], use_tls=False
            )
            self.assertGreaterEqual(published, 1)

            # Sanity: the requester has no local knowledge of the agent.
            local = pc.discover_population_json(
                requester.host, requester.port, capability="settle", use_tls=False
            )
            self.assertEqual(local["total_matches"], 0)

            # Cross-scope resolution finds it via the rendezvous point.
            res = cs.cross_scope_discover(requester.dht_node, "settle", use_tls=False)
            self.assertIn(provider.endpoint, res["providers"])
            self.assertIn("b" * 64, [r["agent_id"] for r in res["results"]])
        finally:
            for c in (rzv, provider, requester):
                c.stop()

    def test_cross_scope_with_no_provider_is_empty(self):
        rzv = InProcessCoordinator([]).start()
        requester = InProcessCoordinator([]).start()
        try:
            bootstrap_over_wire(requester.dht_node, [rzv.endpoint], use_tls=False, refresh=8)
            bootstrap_over_wire(rzv.dht_node, [requester.endpoint], use_tls=False, refresh=8)
            res = cs.cross_scope_discover(requester.dht_node, "nonexistent", use_tls=False)
            self.assertEqual(res["results"], [])
        finally:
            for c in (rzv, requester):
                c.stop()


if __name__ == "__main__":
    unittest.main()
