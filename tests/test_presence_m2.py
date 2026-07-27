"""
AGTP-Presence M2 tests — TTL aging, the visibility model, PROBE-404
indistinguishability, and the presence-visibility cert extension.

The two M2 success criteria (PDD §15) are asserted directly:
  * a withdrawn/expired agent disappears within one TTL window
    (:class:`TTLAgingTests`);
  * an out-of-scope PROBE returns a 404 byte-indistinguishable from
    nonexistence (:meth:`VisibilityWireTests.test_probe_404_indistinguishable`).
"""

from __future__ import annotations

import datetime as dt
import json
import socket
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.identity import AgentDocument, RequiresDeclaration
from presence import client as pclient
from presence.records import PresenceRecord, Visibility
from presence.store import PresenceStore
from presence.visibility import (
    ANONYMOUS,
    RequesterContext,
    audience_allows,
    bound_visibility,
    is_visible,
    shape_entry,
)
from server.agent_cert_ext import (
    add_presence_visibility,
    parse_extensions,
    visibility_envelope_from_cert,
)
from server.config import AgentsConfig, ServerConfig, ServerInfo, ServerPolicy
from server.main import AgentRegistry, handle_connection


def _doc(agent_id, name, methods, *, tier=1, owner_id="") -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0", agent_id=agent_id, name=name, principal="Chris",
        principal_id="chris", description="", status="active", skills=[name],
        requires=RequiresDeclaration(methods=methods), scopes_accepted=[],
        issued_at="now", issuer="self", trust_tier=tier, owner_id=owner_id,
    )


# Distinct 64-hex agent ids.
SUBJECT = "a" * 64
REQ_HI = "1" * 64      # tier 1, acme.tld
REQ_LO = "2" * 64      # tier 3, other.tld
REQ_DOM = "3" * 64     # tier 3, acme.tld


# ---------------------------------------------------------------------------
# TTL aging.
# ---------------------------------------------------------------------------


class TTLAgingTests(unittest.TestCase):
    def _store(self):
        store = PresenceStore()
        doc = _doc(SUBJECT, "subject", ["VALIDATE"])
        store.announce(store.build_record(doc, ttl_seconds=60))
        return store

    def test_record_present_within_ttl(self):
        store = self._store()
        rec = store.probe(SUBJECT)
        now = rec.announced_at_epoch + 30  # half a TTL window
        self.assertIsNotNone(store.probe(SUBJECT, now=now))
        self.assertEqual(store.count(now=now), 1)

    def test_record_gone_after_ttl(self):
        store = self._store()
        base = store.probe(SUBJECT).announced_at_epoch
        now = base + 61  # one TTL window later
        self.assertIsNone(store.probe(SUBJECT, now=now))
        self.assertEqual(store.query_population(now=now), [])
        self.assertEqual(store.count(now=now), 0)

    def test_sweep_reports_evictions(self):
        store = self._store()
        base = store.probe(SUBJECT).announced_at_epoch
        self.assertEqual(store.sweep_expired(now=base + 61), 1)
        self.assertEqual(store.sweep_expired(now=base + 61), 0)

    def test_ttl_zero_never_expires(self):
        store = PresenceStore()
        doc = _doc(SUBJECT, "subject", ["VALIDATE"])
        store.announce(store.build_record(doc, ttl_seconds=0))
        self.assertIsNotNone(store.probe(SUBJECT, now=time.time() + 10_000))


# ---------------------------------------------------------------------------
# Visibility model (unit).
# ---------------------------------------------------------------------------


class AudienceExpressionTests(unittest.TestCase):
    def setUp(self):
        self.r = RequesterContext(
            agent_id="x", tier=2, owner_domain="acme.tld",
            capabilities=frozenset({"booking"}),
        )

    def test_and_or_not(self):
        self.assertTrue(audience_allows("tier:2 AND capability:booking", self.r))
        self.assertTrue(audience_allows("tier:5 OR capability:booking", self.r))
        self.assertFalse(audience_allows("tier:1", self.r))  # 2 !<= 1
        self.assertTrue(audience_allows("NOT owner-domain:evil.tld", self.r))
        self.assertTrue(audience_allows("(tier:9 OR agent-id:x) AND capability:booking", self.r))

    def test_empty_means_everyone(self):
        self.assertTrue(audience_allows("", self.r))
        self.assertTrue(audience_allows("   ", ANONYMOUS))

    def test_malformed_and_unknown_fail_closed(self):
        self.assertFalse(audience_allows("tier:", self.r))
        self.assertFalse(audience_allows("color:blue", self.r))
        self.assertFalse(audience_allows("tier:2 AND", self.r))
        self.assertFalse(audience_allows("(tier:2", self.r))


class PresenceModeTests(unittest.TestCase):
    def _rec(self, mode, disc="capabilities", aud="", tier=1, owner="auditor.tld"):
        entry = {
            "agent_id": SUBJECT, "manifest_uri": f"agtp://{SUBJECT}",
            "name": "s", "supported_methods": ["VALIDATE"],
            "capabilities": ["validate"], "trust_tier": tier,
            "verification_path": "self-signed",
        }
        return PresenceRecord(
            agent_id=SUBJECT, result_entry=entry,
            visibility=Visibility(mode, disc, aud), owner_domain=owner,
        )

    def test_public_visible_to_anonymous(self):
        self.assertTrue(is_visible(self._rec("public"), ANONYMOUS))

    def test_invisible_hidden_from_everyone(self):
        r = RequesterContext(agent_id="y", tier=1, owner_domain="auditor.tld")
        self.assertFalse(is_visible(self._rec("invisible"), r))

    def test_tier_scoped(self):
        hi = RequesterContext(agent_id="h", tier=1)
        lo = RequesterContext(agent_id="l", tier=3)
        self.assertTrue(is_visible(self._rec("tier-scoped", tier=1), hi))
        self.assertFalse(is_visible(self._rec("tier-scoped", tier=1), lo))
        self.assertFalse(is_visible(self._rec("tier-scoped", tier=1), ANONYMOUS))

    def test_owner_domain(self):
        same = RequesterContext(agent_id="s", owner_domain="auditor.tld")
        diff = RequesterContext(agent_id="d", owner_domain="other.tld")
        self.assertTrue(is_visible(self._rec("owner-domain"), same))
        self.assertFalse(is_visible(self._rec("owner-domain"), diff))

    def test_explicit_only(self):
        allowed = RequesterContext(agent_id="friend")
        denied = RequesterContext(agent_id="stranger")
        rec = self._rec("explicit-only", aud="agent-id:friend")
        self.assertTrue(is_visible(rec, allowed))
        self.assertFalse(is_visible(rec, denied))

    def test_disclosure_shaping(self):
        r = RequesterContext(agent_id="x", tier=1)
        ident = shape_entry(self._rec("public", disc="identity-only"), r)
        self.assertNotIn("capabilities", ident)
        self.assertNotIn("supported_methods", ident)
        self.assertIn("name", ident)
        exist = shape_entry(self._rec("public", disc="existence-only"), r)
        self.assertEqual(set(exist), {"agent_id"})

    def test_bound_visibility_reduces_only(self):
        env = Visibility("tier-scoped", "capabilities", "tier:2")
        eff = bound_visibility(env, Visibility("public", "full", ""))
        self.assertEqual(eff.presence_mode, "tier-scoped")   # more restrictive wins
        self.assertEqual(eff.disclosure_mode, "capabilities")
        eff2 = bound_visibility(env, Visibility("invisible", "existence-only", ""))
        self.assertEqual(eff2.presence_mode, "invisible")     # runtime may reduce
        self.assertEqual(eff2.disclosure_mode, "existence-only")


class CertExtensionTests(unittest.TestCase):
    def _cert(self, **kw):
        key = Ed25519PrivateKey.generate()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "a")])
        b = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime(2026, 1, 1))
            .not_valid_after(dt.datetime(2027, 1, 1))
        )
        if kw:
            b = add_presence_visibility(b, **kw)
        return b.sign(key, None)

    def test_round_trip(self):
        cert = self._cert(
            presence_mode="tier-scoped", disclosure_mode="identity-only",
            audience_scope="tier:2 AND capability:booking",
        )
        self.assertEqual(
            parse_extensions(cert).presence_visibility,
            "tier-scoped|identity-only|tier:2 AND capability:booking",
        )
        env = visibility_envelope_from_cert(cert)
        self.assertEqual(env.presence_mode, "tier-scoped")
        self.assertEqual(env.disclosure_mode, "identity-only")
        self.assertEqual(env.audience_scope, "tier:2 AND capability:booking")

    def test_absent_extension_yields_none(self):
        self.assertIsNone(visibility_envelope_from_cert(self._cert()))


# ---------------------------------------------------------------------------
# Visibility over the wire.
# ---------------------------------------------------------------------------


class _Coordinator:
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
        self.registry.presence_store = PresenceStore()
        self.registry.presence_relay_endpoint = f"{self.host}:{self.port}"
        for d in self.registry.agents.values():
            self.registry.presence_store.announce(
                self.registry.presence_store.build_record(
                    d, relay_endpoint=self.registry.presence_relay_endpoint
                )
            )
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


class VisibilityWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.coord = _Coordinator([
            _doc(SUBJECT, "subject", ["VALIDATE", "DESCRIBE"], tier=1, owner_id="acme.tld"),
            _doc(REQ_HI, "req-hi", ["QUERY"], tier=1, owner_id="acme.tld"),
            _doc(REQ_LO, "req-lo", ["QUERY"], tier=3, owner_id="other.tld"),
            _doc(REQ_DOM, "req-dom", ["QUERY"], tier=3, owner_id="acme.tld"),
        ])
        cls.coord.start()

    @classmethod
    def tearDownClass(cls):
        cls.coord.stop()

    def _reannounce(self, visibility):
        # Set SUBJECT's posture via the ANNOUNCE wire method (no cert →
        # no envelope, so the requested posture stands).
        resp = pclient.announce(
            self.coord.host, self.coord.port, SUBJECT,
            visibility=visibility, use_tls=False,
        )
        self.assertEqual(resp.status_code, 200)

    def _pop(self, as_agent):
        return pclient.discover_population_json(
            self.coord.host, self.coord.port, capability="validate",
            as_agent=as_agent, use_tls=False,
        )

    def setUp(self):
        # Reset SUBJECT to public before each test (order-independent).
        self._reannounce({"presence_mode": "public", "disclosure_mode": "capabilities"})

    def test_public_visible_to_all(self):
        self.assertEqual(self._pop(REQ_LO)["total_matches"], 1)
        # anonymous too
        body = pclient.discover_population_json(
            self.coord.host, self.coord.port, capability="validate", use_tls=False
        )
        self.assertEqual(body["total_matches"], 1)

    def test_tier_scoped_hides_from_lower_trust(self):
        self._reannounce({"presence_mode": "tier-scoped", "disclosure_mode": "capabilities"})
        self.assertEqual(self._pop(REQ_HI)["total_matches"], 1)   # tier 1 sees tier-1 subject
        self.assertEqual(self._pop(REQ_LO)["total_matches"], 0)   # tier 3 does not

    def test_owner_domain_scoping(self):
        self._reannounce({"presence_mode": "owner-domain", "disclosure_mode": "capabilities"})
        self.assertEqual(self._pop(REQ_DOM)["total_matches"], 1)  # same acme.tld
        self.assertEqual(self._pop(REQ_LO)["total_matches"], 0)   # other.tld

    def test_audience_expression(self):
        self._reannounce({
            "presence_mode": "public", "disclosure_mode": "capabilities",
            "audience_scope": "tier:1",
        })
        self.assertEqual(self._pop(REQ_HI)["total_matches"], 1)   # tier 1 passes tier:1
        self.assertEqual(self._pop(REQ_LO)["total_matches"], 0)   # tier 3 fails

    def test_disclosure_identity_only_over_wire(self):
        self._reannounce({"presence_mode": "public", "disclosure_mode": "identity-only"})
        result = self._pop(REQ_LO)["results"][0]
        self.assertIn("name", result)
        self.assertNotIn("capabilities", result)
        self.assertNotIn("supported_methods", result)

    def test_invisible_absent_from_population(self):
        self._reannounce({"presence_mode": "invisible", "disclosure_mode": "existence-only"})
        self.assertEqual(self._pop(REQ_HI)["total_matches"], 0)

    def test_probe_404_indistinguishable(self):
        # SUBJECT present but invisible to REQ_LO → 404. Then withdraw
        # SUBJECT entirely and probe the same id → 404. The two responses
        # MUST be byte-identical: an out-of-scope probe is
        # indistinguishable from nonexistence.
        self._reannounce({"presence_mode": "explicit-only", "disclosure_mode": "existence-only",
                          "audience_scope": "agent-id:" + REQ_HI})
        resp_invisible = pclient.probe(
            self.coord.host, self.coord.port, SUBJECT, as_agent=REQ_LO, use_tls=False
        )
        self.assertEqual(resp_invisible.status_code, 404)

        pclient.withdraw(self.coord.host, self.coord.port, SUBJECT, use_tls=False)
        resp_absent = pclient.probe(
            self.coord.host, self.coord.port, SUBJECT, as_agent=REQ_LO, use_tls=False
        )
        self.assertEqual(resp_absent.status_code, 404)
        # Byte-identical bodies for the same id: no existence side channel.
        self.assertEqual(resp_invisible.body_bytes, resp_absent.body_bytes)

    def test_explicit_only_allows_named_agent(self):
        self._reannounce({"presence_mode": "explicit-only", "disclosure_mode": "capabilities",
                          "audience_scope": "agent-id:" + REQ_HI})
        self.assertEqual(self._pop(REQ_HI)["total_matches"], 1)
        self.assertEqual(self._pop(REQ_LO)["total_matches"], 0)


class TTLWireTests(unittest.TestCase):
    def test_short_ttl_agent_disappears(self):
        coord = _Coordinator([_doc(SUBJECT, "subject", ["VALIDATE"], tier=1)])
        coord.start()
        try:
            # Re-announce with a 1s TTL, then let it age out.
            pclient.announce(
                coord.host, coord.port, SUBJECT, ttl_seconds=1, use_tls=False
            )
            body = pclient.discover_population_json(
                coord.host, coord.port, capability="validate", use_tls=False
            )
            self.assertEqual(body["total_matches"], 1)
            time.sleep(1.2)
            gone = pclient.discover_population_json(
                coord.host, coord.port, capability="validate", use_tls=False
            )
            self.assertEqual(gone["total_matches"], 0)
        finally:
            coord.stop()


if __name__ == "__main__":
    unittest.main()
