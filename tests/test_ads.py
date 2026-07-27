"""
AGTP M5 — ADS (Anticipatory Discovery Service) prototype: opt-in signal
capture, co-occurrence prediction, and privacy-respecting preloading over
the discovery substrate.
"""

from __future__ import annotations

import unittest

from ads.service import AnticipatoryDiscoveryService
from ads.signals import SignalStore
from dht.client import bootstrap_over_wire
from presence import client as pc
from presence import crossscope as cs
from presence.testing import InProcessCoordinator, make_doc


AGENT = "a" * 64
SETTLER = "5" * 64


# ---------------------------------------------------------------------------
# Signals + prediction (unit).
# ---------------------------------------------------------------------------


class SignalStoreTests(unittest.TestCase):
    def test_opt_in_required(self):
        s = SignalStore()
        self.assertFalse(s.observe_capability(AGENT, "audit"))  # not subscribed
        s.subscribe(AGENT)
        self.assertTrue(s.observe_capability(AGENT, "audit"))

    def test_transitions_and_frequency(self):
        s = SignalStore()
        s.subscribe(AGENT)
        for a, b in [("audit", "settle"), ("audit", "settle"), ("audit", "report")]:
            s.observe_capability(AGENT, a)
            s.observe_capability(AGENT, b)
        self.assertEqual(s.last_capability(AGENT), "report")
        self.assertEqual(s.transitions_from(AGENT, "audit")["settle"], 2)

    def test_feed_audit(self):
        s = SignalStore()
        s.subscribe(AGENT)
        self.assertTrue(s.feed_audit(AGENT, {"method": "VALIDATE"}))
        self.assertEqual(s.frequency(AGENT)["validate"], 1)

    def test_unsubscribe_forgets(self):
        s = SignalStore()
        s.subscribe(AGENT)
        s.observe_capability(AGENT, "audit")
        s.unsubscribe(AGENT)
        self.assertFalse(s.is_subscribed(AGENT))
        self.assertEqual(s.frequency(AGENT), {})


class PredictionTests(unittest.TestCase):
    def _svc(self):
        a = AnticipatoryDiscoveryService()
        a.subscribe(AGENT)
        for x, y in [("audit", "settle"), ("audit", "settle"), ("audit", "settle"),
                     ("audit", "report"), ("review", "audit")]:
            a.observe(AGENT, x)
            a.observe(AGENT, y)
        return a

    def test_predicts_cooccurring_capability(self):
        a = self._svc()
        pred = dict(a.predict(AGENT, context="audit"))
        self.assertIn("settle", pred)
        # settle (3 transitions) outranks report (1 transition)
        self.assertGreater(pred["settle"], pred.get("report", 0))

    def test_excludes_current_capability(self):
        a = self._svc()
        caps = [c for c, _ in a.predict(AGENT, context="audit")]
        self.assertNotIn("audit", caps)

    def test_unsubscribed_predicts_nothing(self):
        a = AnticipatoryDiscoveryService()
        self.assertEqual(a.predict("z" * 64), [])


# ---------------------------------------------------------------------------
# Preloading over the substrate (integration).
# ---------------------------------------------------------------------------


class PreloadTests(unittest.TestCase):
    def _federation(self):
        rzv = InProcessCoordinator([]).start()
        provider = InProcessCoordinator(
            [make_doc(SETTLER, "settler", ["VALIDATE", "SETTLE"], tier=1)]
        ).start()
        home = InProcessCoordinator([]).start()  # the ADS's local coordinator
        for c in (provider, home):
            bootstrap_over_wire(c.dht_node, [rzv.endpoint], use_tls=False, refresh=8)
        bootstrap_over_wire(rzv.dht_node, [provider.endpoint], use_tls=False, refresh=8)
        cs.publish_scopes(provider.dht_node, provider.endpoint, ["settle"], use_tls=False)
        return rzv, provider, home

    def _primed_ads(self):
        a = AnticipatoryDiscoveryService(use_tls=False)
        a.subscribe(AGENT)
        for x, y in [("audit", "settle")] * 3:
            a.observe(AGENT, x)
            a.observe(AGENT, y)
        a.observe(AGENT, "audit")  # current context -> predicts settle
        return a

    def test_preload_caches_predicted_capability(self):
        rzv, provider, home = self._federation()
        try:
            ads = self._primed_ads()
            preloaded = ads.preload(AGENT, home.dht_node, top=3)
            self.assertIn("settle", preloaded)
            cached = ads.get_preloaded(AGENT, "settle")
            self.assertIsNotNone(cached)
            self.assertIn(SETTLER, [r["agent_id"] for r in cached["results"]])
        finally:
            for c in (rzv, provider, home):
                c.stop()

    def test_preload_respects_visibility(self):
        # The settlement agent goes invisible; ADS preloading (which queries
        # as the subscriber, out-of-domain) must surface nothing — a
        # prediction never reveals an agent the subscriber couldn't see.
        rzv, provider, home = self._federation()
        try:
            pc.announce(
                provider.host, provider.port, SETTLER,
                visibility={"presence_mode": "invisible", "disclosure_mode": "existence-only"},
                use_tls=False,
            )
            ads = self._primed_ads()
            ads.preload(AGENT, home.dht_node, top=3)
            cached = ads.get_preloaded(AGENT, "settle")
            self.assertEqual(cached["results"], [])
        finally:
            for c in (rzv, provider, home):
                c.stop()

    def test_preload_only_for_subscribers(self):
        rzv, provider, home = self._federation()
        try:
            ads = AnticipatoryDiscoveryService(use_tls=False)  # nobody subscribed
            self.assertEqual(ads.preload(AGENT, home.dht_node), [])
        finally:
            for c in (rzv, provider, home):
                c.stop()


if __name__ == "__main__":
    unittest.main()
