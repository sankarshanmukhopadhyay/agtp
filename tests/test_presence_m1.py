"""
AGTP-Presence M1 tests.

Two layers:

  * Unit — the store, capability derivation, and scope tuples in-process.
  * Integration — a live coordinator (an AGTP server with a PresenceStore
    attached, exactly as ``run(presence=True)`` wires it) exercised over
    the wire with the ``presence.client`` helpers. This is the Appendix B
    demo path as an assertion: announce → converge → discover by
    capability → probe → route by Agent-ID, with the invariant that no
    agent-held network address ever appears in a discovery payload.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core import wire
from core.identity import AgentDocument, RequiresDeclaration
from presence import client as pclient
from presence.scopes import (
    agent_may_claim,
    default_scopes,
    derive_capabilities,
    overlay_id,
    scope_tuple,
)
from presence.store import PresenceStore
from server.config import AgentsConfig, ServerConfig, ServerInfo, ServerPolicy
from server.main import AgentRegistry, handle_connection


def _doc(agent_id: str, name: str, methods, tier: int = 1) -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0",
        agent_id=agent_id,
        name=name,
        principal="Chris",
        principal_id="chris",
        description="",
        status="active",
        skills=[name],
        requires=RequiresDeclaration(methods=methods),
        scopes_accepted=[],
        issued_at="now",
        issuer="self",
        trust_tier=tier,
    )


SUMMARIZER_ID = "a" * 64
AUDITOR_ID = "b" * 64


# ---------------------------------------------------------------------------
# Unit — capability derivation and scope tuples.
# ---------------------------------------------------------------------------


class CapabilityDerivationTests(unittest.TestCase):
    def test_capabilities_derive_from_bound_methods(self):
        doc = _doc(AUDITOR_ID, "auditor", ["VALIDATE", "REPORT"])
        caps = derive_capabilities(doc)
        # method-name tokens present
        self.assertIn("validate", caps)
        self.assertIn("report", caps)

    def test_unbound_capability_cannot_be_claimed(self):
        doc = _doc(AUDITOR_ID, "auditor", ["VALIDATE", "REPORT"])
        self.assertTrue(agent_may_claim(doc, "validate"))
        # 'summarize' is not a bound method → not claimable (the M3
        # index-poisoning defense, exercised here at M1).
        self.assertFalse(agent_may_claim(doc, "summarize"))

    def test_wildcard_agent_derives_full_vocabulary(self):
        doc = _doc(SUMMARIZER_ID, "orchestrator", [])
        doc.requires.wildcards = True
        caps = derive_capabilities(doc)
        self.assertIn("validate", caps)
        self.assertIn("summarize", caps)

    def test_scope_tuple_canonical_ordering(self):
        # Same logical scope always renders identically regardless of
        # kwarg order, so overlay_id is stable.
        a = scope_tuple(capability="booking", tier=2, region="us")
        b = scope_tuple(region="us", tier=2, capability="booking")
        self.assertEqual(a, b)
        self.assertEqual(a, "{tier: 2, capability: booking, region: us}")
        self.assertEqual(overlay_id(a), overlay_id(b))

    def test_default_scopes_include_tier_and_capabilities(self):
        doc = _doc(AUDITOR_ID, "auditor", ["VALIDATE"])
        scopes = default_scopes(doc)
        self.assertIn("{tier: 1}", scopes)
        self.assertTrue(any("capability: validate" in s for s in scopes))


class PresenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = PresenceStore()
        self.summarizer = _doc(SUMMARIZER_ID, "summarizer", ["QUERY", "SUMMARIZE"])
        self.auditor = _doc(AUDITOR_ID, "auditor", ["VALIDATE", "REPORT"])
        self.store.announce(self.store.build_record(self.summarizer, relay_endpoint="c:1"))
        self.store.announce(self.store.build_record(self.auditor, relay_endpoint="c:1"))

    def test_population_query_by_capability(self):
        res = self.store.query_population(capability="validate")
        self.assertEqual([r.agent_id for r in res], [AUDITOR_ID])

    def test_population_query_unfiltered_returns_all(self):
        res = self.store.query_population()
        self.assertEqual({r.agent_id for r in res}, {SUMMARIZER_ID, AUDITOR_ID})

    def test_result_entry_has_no_network_address(self):
        res = self.store.query_population(capability="validate")
        blob = json.dumps(res[0].to_result_entry())
        self.assertNotIn("c:1", blob)  # the relay endpoint must not leak
        self.assertNotIn("attachment", blob)

    def test_announce_is_idempotent(self):
        self.store.announce(self.store.build_record(self.auditor, relay_endpoint="c:1"))
        self.assertEqual(self.store.count(), 2)

    def test_withdraw_removes_from_population(self):
        self.assertTrue(self.store.withdraw(AUDITOR_ID))
        self.assertEqual(self.store.query_population(capability="validate"), [])
        self.assertFalse(self.store.withdraw(AUDITOR_ID))  # already gone


# ---------------------------------------------------------------------------
# Integration — a live coordinator over the wire (Appendix B path).
# ---------------------------------------------------------------------------


class _Coordinator:
    """A loopback AGTP server in coordinator mode, mirroring run(presence=True)."""

    def __init__(self, docs):
        self._tmp = TemporaryDirectory()
        self.registry = AgentRegistry(Path(self._tmp.name))
        for d in docs:
            self.registry.agents[d.agent_id] = d

        self.config = ServerConfig(
            server=ServerInfo(server_id="coord.local", operator="x", contact=""),
            policy=ServerPolicy(wildcards_accepted=True),
            agents=AgentsConfig(disclosure="public"),
        )

        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.host, self.port = self.sock.getsockname()
        self.sock.listen(32)
        self.sock.settimeout(0.2)

        # Coordinator mode: attach the store, then self-announce every
        # hosted agent (exactly what run(presence=True) does at boot).
        self.registry.presence_store = PresenceStore()
        self.registry.presence_relay_endpoint = f"{self.host}:{self.port}"
        for d in self.registry.agents.values():
            rec = self.registry.presence_store.build_record(
                d, relay_endpoint=self.registry.presence_relay_endpoint
            )
            self.registry.presence_store.announce(rec)

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        time.sleep(0.05)

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self._tmp.cleanup()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=handle_connection,
                args=(conn, self.registry, self.config),
                daemon=True,
            ).start()


class CoordinatorWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.coord = _Coordinator([
            _doc(SUMMARIZER_ID, "summarizer", ["QUERY", "SUMMARIZE", "DESCRIBE"]),
            _doc(AUDITOR_ID, "auditor", ["VALIDATE", "REPORT", "DESCRIBE"]),
        ])
        cls.coord.start()

    @classmethod
    def tearDownClass(cls):
        cls.coord.stop()

    def _pop(self, **kw):
        return pclient.discover_population_json(
            self.coord.host, self.coord.port, use_tls=False, **kw
        )

    def test_boot_self_announce_populates(self):
        body = self._pop()
        self.assertEqual(body["total_matches"], 2)

    def test_discover_population_by_capability_returns_one(self):
        body = self._pop(capability="validate")
        self.assertEqual(body["total_matches"], 1)
        result = body["results"][0]
        self.assertEqual(result["agent_id"], AUDITOR_ID)
        self.assertEqual(result["manifest_uri"], f"agtp://{AUDITOR_ID}")
        self.assertEqual(result["rank"], 1)

    def test_population_payload_has_zero_network_literals(self):
        # The core principals-not-hosts invariant: no agent-held address,
        # and not even the coordinator's own endpoint, rides in the
        # discovery payload. Callers route by Agent-ID.
        resp = pclient.discover_population(
            self.coord.host, self.coord.port, capability="validate", use_tls=False
        )
        blob = resp.body_bytes.decode("utf-8")
        self.assertNotIn(str(self.coord.port), blob)
        self.assertNotIn(self.coord.host, blob)
        self.assertNotIn("attachment", blob)

    def test_probe_present_then_absent_after_withdraw(self):
        present = pclient.probe_json(
            self.coord.host, self.coord.port, SUMMARIZER_ID, use_tls=False
        )
        self.assertTrue(present["present"])
        self.assertEqual(present["posture"]["presence_mode"], "public")

        # WITHDRAW, then PROBE returns 404 (present flips to absence).
        pclient.withdraw_json(
            self.coord.host, self.coord.port, SUMMARIZER_ID, use_tls=False
        )
        resp = pclient.probe(
            self.coord.host, self.coord.port, SUMMARIZER_ID, use_tls=False
        )
        self.assertEqual(resp.status_code, 404)

        # Re-announce so other tests (order-independent) still see it.
        pclient.announce(
            self.coord.host, self.coord.port, SUMMARIZER_ID, use_tls=False
        )

    def test_announce_foreign_agent_is_rejected(self):
        resp = pclient.announce(
            self.coord.host, self.coord.port, "c" * 64, use_tls=False
        )
        self.assertEqual(resp.status_code, 422)
        payload = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "announce-agent-not-hosted")

    def test_describe_routes_by_agent_id_through_coordinator(self):
        # The coordinator hosts the agents, so DESCRIBE by Agent-ID
        # resolves and returns the agent document — routing by identity,
        # not by a client-known endpoint.
        from client.core_client import send_method

        resp = send_method(
            AUDITOR_ID, self.coord.host, self.coord.port, "DESCRIBE",
            use_tls=False,
        )
        self.assertEqual(resp.status_code, 200)
        doc = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(doc["agent_id"], AUDITOR_ID)


if __name__ == "__main__":
    unittest.main()
