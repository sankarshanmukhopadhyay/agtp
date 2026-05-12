"""
§7 PROPOSE / Synthesis alignment tests (agtp-api §7).

Covers:

  * Status code surface: 263 / 463 / 261 / 400 / 262.
  * 463 reason vocabulary (out-of-scope, policy-refused,
    composition-impossible).
  * 400 issue vocabulary (invalid-json, missing-required-field,
    malformed-semantic-block).
  * 262 type vocabulary (scope-required, wildcards-required,
    anonymous-discovery-disabled).
  * Duration parsing (s/m/h/d) and compute_expiration policy
    (session / persistent default / persistent max).
  * Persistent synthesis: granted duration respects max cap.
  * ProposalStore async flow: create → poll (261) → resolve →
    poll (263 / 463).
  * Synthesis expiration: past expires_at, get() returns None.
  * Audit log: a JSONL entry appears per PROPOSE outcome.

Existing PROPOSE tests live in ``test_negotiation.py`` /
``test_synthesis_runtime.py`` / ``test_methods.py``; this module
houses tests that didn't exist pre-§7.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import status as status_codes
from core import wire
from core.identity import AgentDocument, RequiresDeclaration
from server.audit import record_propose
from server.config import (
    AgentsConfig,
    AuditConfig,
    ServerConfig,
    ServerInfo,
    ServerPolicy,
    SynthesisConfig,
)
from server.main import AgentRegistry, handle_connection
from server.proposal_store import (
    ProposalStore, hash_proposal_body, new_proposal_id,
)
from server.synthesis_duration import (
    compute_expiration, format_duration_seconds, parse_duration,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"
ORCH_ID = "9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"


# ===========================================================================
# Duration parsing and expiration computation (pure functions).
# ===========================================================================


class DurationParseTests(unittest.TestCase):

    def test_parse_seconds(self):
        self.assertEqual(parse_duration("30s"), 30.0)

    def test_parse_minutes(self):
        self.assertEqual(parse_duration("10m"), 600.0)

    def test_parse_hours(self):
        self.assertEqual(parse_duration("24h"), 86400.0)

    def test_parse_days(self):
        self.assertEqual(parse_duration("7d"), 604800.0)

    def test_parse_uppercase_unit(self):
        self.assertEqual(parse_duration("1D"), 86400.0)

    def test_parse_whitespace_tolerant(self):
        self.assertEqual(parse_duration("  3h  "), 10800.0)

    def test_parse_zero(self):
        self.assertEqual(parse_duration("0s"), 0.0)

    def test_parse_rejects_empty(self):
        with self.assertRaises(ValueError):
            parse_duration("")

    def test_parse_rejects_bad_unit(self):
        with self.assertRaises(ValueError):
            parse_duration("5y")

    def test_parse_rejects_compound(self):
        # Compound durations (1d12h) are out of scope for v00.
        with self.assertRaises(ValueError):
            parse_duration("1d12h")

    def test_parse_rejects_negative(self):
        with self.assertRaises(ValueError):
            parse_duration("-1h")

    def test_format_duration_seconds_picks_largest_exact_unit(self):
        self.assertEqual(format_duration_seconds(86400), "1d")
        self.assertEqual(format_duration_seconds(3600), "1h")
        self.assertEqual(format_duration_seconds(60), "1m")
        self.assertEqual(format_duration_seconds(90), "90s")
        self.assertEqual(format_duration_seconds(0), "0s")


class ComputeExpirationTests(unittest.TestCase):

    def _config(self, **overrides):
        defaults = dict(
            session_duration="24h",
            persistent_default_duration="7d",
            persistent_max_duration="30d",
        )
        defaults.update(overrides)
        return ServerConfig(
            server=ServerInfo(server_id="t", operator="x", contact=""),
            synthesis=SynthesisConfig(**defaults),
        )

    def test_non_persistent_uses_session_duration(self):
        cfg = self._config(session_duration="24h")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expires, granted = compute_expiration(
            config=cfg, persistent=False,
            requested_seconds=None, now=now,
        )
        self.assertEqual(granted, "1d")
        self.assertEqual(expires, now + timedelta(hours=24))

    def test_persistent_without_request_uses_default(self):
        cfg = self._config(persistent_default_duration="7d")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expires, granted = compute_expiration(
            config=cfg, persistent=True,
            requested_seconds=None, now=now,
        )
        self.assertEqual(granted, "7d")
        self.assertEqual(expires, now + timedelta(days=7))

    def test_persistent_request_below_max_granted(self):
        cfg = self._config(persistent_max_duration="30d")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Request 5 days; max is 30; granted = 5d.
        expires, granted = compute_expiration(
            config=cfg, persistent=True,
            requested_seconds=parse_duration("5d"),
            now=now,
        )
        self.assertEqual(granted, "5d")
        self.assertEqual(expires, now + timedelta(days=5))

    def test_persistent_request_above_max_capped(self):
        cfg = self._config(persistent_max_duration="30d")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Request 90 days; max is 30; granted = 30d (capped).
        expires, granted = compute_expiration(
            config=cfg, persistent=True,
            requested_seconds=parse_duration("90d"),
            now=now,
        )
        self.assertEqual(granted, "30d")
        self.assertEqual(expires, now + timedelta(days=30))

    def test_no_config_returns_none(self):
        # The (None, None) shape is the pre-§7 behavior — no
        # hard expiration. Used by tests / fixtures that build a
        # bare runtime without a server config.
        expires, granted = compute_expiration(
            config=None, persistent=False, requested_seconds=None,
        )
        self.assertIsNone(expires)
        self.assertIsNone(granted)


# ===========================================================================
# ProposalStore (in-process state machine).
# ===========================================================================


class ProposalStoreTests(unittest.TestCase):

    def test_create_returns_proposal_id(self):
        store = ProposalStore()
        pid = store.create(
            agent_id="agent-1",
            proposal_body={"name": "QUERY"},
        )
        self.assertTrue(pid.startswith("prop-"))
        record = store.lookup(pid)
        self.assertIsNotNone(record)
        self.assertEqual(record.state, "pending")
        self.assertEqual(record.agent_id, "agent-1")

    def test_resolve_accepted_transitions_state(self):
        store = ProposalStore()
        pid = store.create(
            agent_id="agent-1",
            proposal_body={"name": "QUERY"},
        )
        ok = store.resolve_accepted(pid, body={"synthesis_id": "syn-1"})
        self.assertTrue(ok)
        record = store.lookup(pid)
        self.assertEqual(record.state, "accepted")
        self.assertEqual(record.result_status, 263)
        self.assertEqual(record.result_body, {"synthesis_id": "syn-1"})

    def test_resolve_rejected_transitions_state(self):
        store = ProposalStore()
        pid = store.create(
            agent_id="agent-1",
            proposal_body={"name": "X"},
        )
        ok = store.resolve_rejected(pid, body={"error": {"code": "x"}})
        self.assertTrue(ok)
        record = store.lookup(pid)
        self.assertEqual(record.state, "rejected")
        self.assertEqual(record.result_status, 463)

    def test_resolve_twice_returns_false(self):
        # The state machine is monotonic; a resolved proposal
        # cannot be re-resolved.
        store = ProposalStore()
        pid = store.create(agent_id="a", proposal_body={})
        self.assertTrue(store.resolve_accepted(pid, body={}))
        self.assertFalse(store.resolve_accepted(pid, body={}))
        self.assertFalse(store.resolve_rejected(pid, body={}))

    def test_lookup_unknown_returns_none(self):
        store = ProposalStore()
        self.assertIsNone(store.lookup("prop-nope"))

    def test_sweep_expired_marks_overdue_pending_rejected(self):
        # max_evaluation_seconds=0 means every pending proposal
        # is immediately past deadline.
        store = ProposalStore(max_evaluation_seconds=0)
        pid = store.create(agent_id="a", proposal_body={})
        # Pause briefly so ``now >= deadline`` holds for fast clocks.
        time.sleep(0.01)
        n = store.sweep_expired()
        self.assertEqual(n, 1)
        record = store.lookup(pid)
        self.assertEqual(record.state, "rejected")

    def test_hash_proposal_body_deterministic(self):
        h1 = hash_proposal_body({"name": "Q", "outcome": "x"})
        h2 = hash_proposal_body({"outcome": "x", "name": "Q"})
        self.assertEqual(h1, h2)
        h3 = hash_proposal_body({"name": "X"})
        self.assertNotEqual(h1, h3)


# ===========================================================================
# Synthesis runtime: persistent flag and expiration eviction.
# ===========================================================================


class SynthesisExpirationTests(unittest.TestCase):

    def _runtime(self):
        from core.endpoint import EndpointSpec, SemanticBlock
        from server.synthesis import PassthroughPolicy, SynthesisRuntime
        from server.synthesis.plan import (
            CompositionStep, SynthesisPlan,
        )
        rt = SynthesisRuntime(
            policies=[PassthroughPolicy()],
            step_dispatcher=lambda *a: None,
        )

        def _spec(name):
            return EndpointSpec(
                name=name, path="/x",
                semantic=SemanticBlock(
                    intent="x.", actor="agent",
                    outcome="x.", capability="retrieval",
                    confidence=0.9, impact="informational",
                    is_idempotent=True,
                ),
            )

        plan = SynthesisPlan(
            proposed_method=_spec("Q"),
            steps=[CompositionStep(method_name="Q", parameter_source={})],
            output_aggregation="last",
        )
        return rt, plan

    def test_instantiate_with_future_expiration_lookups_succeed(self):
        rt, plan = self._runtime()
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        sid = rt.instantiate(plan, expires_at=future, persistent=False)
        self.assertIsNotNone(rt.get(sid))
        self.assertEqual(rt.expires_at(sid), future)
        self.assertFalse(rt.is_persistent(sid))

    def test_instantiate_persistent_flag(self):
        rt, plan = self._runtime()
        future = datetime.now(tz=timezone.utc) + timedelta(days=7)
        sid = rt.instantiate(plan, expires_at=future, persistent=True)
        self.assertTrue(rt.is_persistent(sid))

    def test_get_returns_none_when_past_expires(self):
        rt, plan = self._runtime()
        past = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
        sid = rt.instantiate(plan, expires_at=past, persistent=False)
        # Lazy sweep on lookup evicts the expired entry.
        self.assertIsNone(rt.get(sid))
        # Subsequent lookups stay None.
        self.assertIsNone(rt.get(sid))

    def test_sweep_expired_removes_expired_entries(self):
        rt, plan = self._runtime()
        past = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        sid_past = rt.instantiate(plan, expires_at=past, persistent=False)
        sid_future = rt.instantiate(plan, expires_at=future, persistent=False)
        expired = rt.sweep_expired()
        self.assertIn(sid_past, expired)
        self.assertNotIn(sid_future, expired)


# ===========================================================================
# Audit log writes one JSONL entry per PROPOSE outcome.
# ===========================================================================


class _BareAgent:
    def __init__(self, agent_id):
        self.agent_id = agent_id


class AuditLogTests(unittest.TestCase):

    def _state(self, audit_path: str):
        class _State:
            config = ServerConfig(
                server=ServerInfo(server_id="t", operator="x", contact=""),
                audit=AuditConfig(path=audit_path),
            )
        return _State()

    def test_record_propose_writes_jsonl_to_file(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "audit.log"
            state = self._state(str(log_path))
            record_propose(
                state,
                agent_doc=_BareAgent("agent-abc"),
                proposal_body={"name": "QUERY"},
                decision="accepted",
                synthesis_id="syn-xyz",
                granted_duration="24h",
            )
            self.assertTrue(log_path.exists())
            line = log_path.read_text().strip()
            entry = json.loads(line)
            self.assertEqual(entry["agent_id"], "agent-abc")
            self.assertEqual(entry["decision"], "accepted")
            self.assertEqual(entry["synthesis_id"], "syn-xyz")
            self.assertEqual(entry["granted_duration"], "24h")
            self.assertIn("timestamp", entry)
            self.assertIn("proposal_hash", entry)

    def test_record_propose_writes_jsonl_per_outcome(self):
        # Sequence of decisions produces one line each, in order.
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "audit.log"
            state = self._state(str(log_path))
            for decision in ("accepted", "rejected", "pending", "malformed"):
                record_propose(
                    state,
                    agent_doc=_BareAgent("a"),
                    proposal_body={"x": 1},
                    decision=decision,
                )
            lines = log_path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 4)
            decisions = [json.loads(l)["decision"] for l in lines]
            self.assertEqual(
                decisions, ["accepted", "rejected", "pending", "malformed"],
            )

    def test_record_propose_disabled_when_path_none(self):
        # ``[audit] path = "none"`` disables logging entirely.
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "audit.log"

            class _State:
                config = ServerConfig(
                    server=ServerInfo(
                        server_id="t", operator="x", contact="",
                    ),
                    audit=AuditConfig(path="none"),
                )

            record_propose(
                _State(),
                agent_doc=_BareAgent("a"),
                proposal_body={},
                decision="accepted",
            )
            self.assertFalse(log_path.exists())


# ===========================================================================
# §7 status-code helpers: shape verification.
# ===========================================================================


class StatusHelperShapeTests(unittest.TestCase):

    def test_proposal_approved_shape(self):
        resp = status_codes.proposal_approved(
            synthesis_id="syn-1",
            endpoint={"method": "QUERY"},
            persistent=True,
            expires_at="2026-05-11T00:00:00Z",
            granted_duration="7d",
        )
        self.assertEqual(resp.status_code, 263)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["synthesis_id"], "syn-1")
        self.assertEqual(body["persistent"], True)
        self.assertEqual(body["expires_at"], "2026-05-11T00:00:00Z")
        self.assertEqual(body["granted_duration"], "7d")
        self.assertEqual(body["endpoint"], {"method": "QUERY"})

    def test_proposal_rejected_with_reason_and_counter(self):
        resp = status_codes.proposal_rejected(
            reason="out-of-scope",
            explanation="nothing close",
            counter_proposal={"name": "QUERY"},
        )
        self.assertEqual(resp.status_code, 463)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "proposal-rejected")
        self.assertEqual(body["error"]["reason"], "out-of-scope")
        self.assertEqual(body["error"]["counter_proposal"], {"name": "QUERY"})

    def test_proposal_rejected_rejects_unknown_reason(self):
        with self.assertRaises(ValueError):
            status_codes.proposal_rejected(
                reason="banana", explanation="x",
            )

    def test_negotiation_in_progress_shape(self):
        resp = status_codes.negotiation_in_progress(
            proposal_id="prop-abc",
            polling_path="/proposals/prop-abc",
            evaluation_started_at="2026-05-10T00:00:00Z",
            max_evaluation_duration="10m",
        )
        self.assertEqual(resp.status_code, 261)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["proposal_id"], "prop-abc")
        self.assertEqual(body["polling_path"], "/proposals/prop-abc")
        self.assertEqual(body["max_evaluation_duration"], "10m")

    def test_authorization_required_shape(self):
        resp = status_codes.authorization_required(
            type="scope-required",
            explanation="needs bookings:write",
            details={"missing_scopes": ["bookings:write"]},
        )
        self.assertEqual(resp.status_code, 262)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "authorization-required")
        self.assertEqual(body["error"]["type"], "scope-required")
        self.assertEqual(
            body["error"]["details"]["missing_scopes"], ["bookings:write"],
        )

    def test_authorization_required_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            status_codes.authorization_required(
                type="banana", explanation="x",
            )

    def test_bad_request_for_propose_shape(self):
        resp = status_codes.bad_request_for_propose(
            issue="invalid-json",
            explanation="body wasn't JSON",
        )
        self.assertEqual(resp.status_code, 400)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "bad-request")
        self.assertEqual(body["error"]["issue"], "invalid-json")

    def test_anonymous_discovery_disabled_helper(self):
        resp = status_codes.anonymous_discovery_disabled()
        self.assertEqual(resp.status_code, 262)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(
            body["error"]["type"], "anonymous-discovery-disabled",
        )

    def test_wildcards_refused_routes_through_262(self):
        # The legacy ``wildcards_refused`` helper now produces 262.
        resp = status_codes.wildcards_refused(agent_id="abc")
        self.assertEqual(resp.status_code, 262)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["type"], "wildcards-required")

    def test_insufficient_scope_routes_through_262(self):
        # The legacy ``insufficient_scope`` helper now produces 262.
        resp = status_codes.insufficient_scope(
            "BOOK", "/room", ["bookings:write"],
        )
        self.assertEqual(resp.status_code, 262)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["type"], "scope-required")


# ===========================================================================
# Async PROPOSE end-to-end via an in-process server.
# ===========================================================================


def _stage_agents(agents_dir: Path) -> None:
    src = REPO_ROOT / "server" / "agents"
    for name in ("lauren.agent.json", "orchestrator.agent.json"):
        (agents_dir / name).write_text(
            (src / name).read_text(encoding="utf-8"), encoding="utf-8"
        )


class _Server:
    def __init__(self, registry, config):
        self.registry = registry
        self.config = config
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.host, self.port = self.sock.getsockname()
        self.sock.listen(32)
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

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


def _send_propose(server, body, agent_id=ORCH_ID):
    sock = socket.create_connection(
        (server.host, server.port), timeout=5.0,
    )
    body_bytes = json.dumps(body).encode("utf-8")
    headers = {
        "Host": f"{server.host}:{server.port}",
        "Agent-ID": agent_id,
        "Content-Type": "application/json",
        "Content-Length": str(len(body_bytes)),
    }
    req = wire.AGTPRequest(
        method="PROPOSE", headers=headers, body_bytes=body_bytes,
    )
    try:
        sock.sendall(req.serialize())
        return wire.parse_response(sock.makefile("rb"))
    finally:
        sock.close()


def _send_query(server, path, agent_id=ORCH_ID, body=None):
    sock = socket.create_connection(
        (server.host, server.port), timeout=5.0,
    )
    body_bytes = (
        json.dumps(body).encode("utf-8") if body is not None else b""
    )
    headers = {
        "Host": f"{server.host}:{server.port}",
        "Agent-ID": agent_id,
        "Content-Type": "application/json",
        "Content-Length": str(len(body_bytes)),
    }
    req = wire.AGTPRequest(
        method="QUERY", headers=headers, body_bytes=body_bytes, path=path,
    )
    try:
        sock.sendall(req.serialize())
        return wire.parse_response(sock.makefile("rb"))
    finally:
        sock.close()


def _poll_proposal(server, proposal_id, agent_id=ORCH_ID):
    """Helper: send ``QUERY /proposals`` with ``{proposal_id: ...}``."""
    return _send_query(
        server, "/proposals", agent_id=agent_id,
        body={"proposal_id": proposal_id},
    )


class AsyncProposeEndToEndTests(unittest.TestCase):
    """End-to-end coverage of the 261 → poll → 263/463 flow."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = ServerConfig(
            server=ServerInfo(server_id="t.local", operator="x", contact=""),
            policy=ServerPolicy(),
            agents=AgentsConfig(disclosure="public"),
            synthesis=SynthesisConfig(async_evaluation_enabled=True),
        )
        cls.registry.config = cls.config
        # The async-poll built-in (QUERY /proposals) lives in the
        # endpoint registry; production boot calls register_builtins
        # via run(). Mirror that here so the test fixture has the
        # full surface available.
        cls.registry.register_builtins()
        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_async_propose_returns_261_with_proposal_id(self):
        resp = _send_propose(
            self.server,
            {"name": "QUERY", "parameters": {"intent": "string"},
             "outcome": "results"},
        )
        self.assertEqual(resp.status_code, 261)
        payload = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertTrue(payload["proposal_id"].startswith("prop-"))
        self.assertEqual(payload["polling_path"], "/proposals")

    def test_async_propose_then_poll_pending(self):
        resp = _send_propose(
            self.server,
            {"name": "QUERY", "parameters": {"intent": "string"},
             "outcome": "results"},
        )
        pid = json.loads(resp.body_bytes.decode("utf-8"))["proposal_id"]
        poll = _poll_proposal(self.server, pid)
        self.assertEqual(poll.status_code, 261)
        body = json.loads(poll.body_bytes.decode("utf-8"))
        self.assertEqual(body["state"], "pending")
        self.assertEqual(body["proposal_id"], pid)

    def test_async_propose_then_resolve_then_poll_accepted(self):
        resp = _send_propose(
            self.server,
            {"name": "QUERY", "parameters": {"intent": "string"},
             "outcome": "results"},
        )
        pid = json.loads(resp.body_bytes.decode("utf-8"))["proposal_id"]
        # Resolve via direct ProposalStore access (simulating an
        # external evaluation pipeline completing).
        self.registry.proposal_store.resolve_accepted(
            pid, body={"synthesis_id": "syn-test", "endpoint": {}},
        )
        poll = _poll_proposal(self.server, pid)
        self.assertEqual(poll.status_code, 263)
        body = json.loads(poll.body_bytes.decode("utf-8"))
        self.assertEqual(body["state"], "accepted")
        self.assertEqual(body["synthesis_id"], "syn-test")

    def test_async_propose_then_resolve_then_poll_rejected(self):
        resp = _send_propose(
            self.server,
            {"name": "QUERY", "parameters": {"intent": "string"},
             "outcome": "results"},
        )
        pid = json.loads(resp.body_bytes.decode("utf-8"))["proposal_id"]
        self.registry.proposal_store.resolve_rejected(
            pid,
            body={
                "error": {
                    "code": "proposal-rejected",
                    "reason": "policy-refused",
                    "explanation": "test",
                }
            },
        )
        poll = _poll_proposal(self.server, pid)
        self.assertEqual(poll.status_code, 463)
        body = json.loads(poll.body_bytes.decode("utf-8"))
        self.assertEqual(body["state"], "rejected")
        self.assertEqual(body["error"]["reason"], "policy-refused")

    def test_poll_unknown_proposal_returns_404(self):
        poll = _poll_proposal(self.server, "prop-nope")
        self.assertEqual(poll.status_code, 404)


# ===========================================================================
# Sync PROPOSE: 400 issue vocabulary and 463 reason vocabulary.
# ===========================================================================


class SyncProposeEdgeCases(unittest.TestCase):
    """Edge cases of the synchronous (default) PROPOSE flow."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = ServerConfig(
            server=ServerInfo(server_id="t.local", operator="x", contact=""),
            policy=ServerPolicy(),
            agents=AgentsConfig(disclosure="public"),
        )
        cls.registry.config = cls.config
        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_invalid_json_returns_400(self):
        sock = socket.create_connection(
            (self.server.host, self.server.port), timeout=5.0,
        )
        body_bytes = b"this is not json"
        headers = {
            "Host": f"{self.server.host}:{self.server.port}",
            "Agent-ID": ORCH_ID,
            "Content-Type": "application/json",
            "Content-Length": str(len(body_bytes)),
        }
        req = wire.AGTPRequest(
            method="PROPOSE", headers=headers, body_bytes=body_bytes,
        )
        try:
            sock.sendall(req.serialize())
            resp = wire.parse_response(sock.makefile("rb"))
        finally:
            sock.close()
        self.assertEqual(resp.status_code, 400)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["issue"], "invalid-json")

    def test_malformed_semantic_block_returns_400(self):
        resp = _send_propose(
            self.server,
            {"name": "QUERY", "semantic": "not-a-dict",
             "parameters": {"intent": "string"}, "outcome": "x"},
        )
        self.assertEqual(resp.status_code, 400)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["issue"], "malformed-semantic-block")

    def test_malformed_input_schema_returns_400(self):
        resp = _send_propose(
            self.server,
            {"name": "QUERY", "input_schema": "not-an-object",
             "outcome": "x"},
        )
        self.assertEqual(resp.status_code, 400)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["issue"], "malformed-schema")

    def test_synthesis_disabled_returns_463_policy_refused(self):
        cfg = ServerConfig(
            server=ServerInfo(
                server_id="t.local", operator="x", contact="",
            ),
            policy=ServerPolicy(synthesis_enabled=False),
            agents=AgentsConfig(disclosure="public"),
        )
        # Spin up a fresh server with synthesis disabled.
        tmp = tempfile.TemporaryDirectory()
        try:
            agents_dir = Path(tmp.name)
            _stage_agents(agents_dir)
            registry = AgentRegistry(agents_dir)
            registry.config = cfg
            srv = _Server(registry, cfg)
            srv.start()
            time.sleep(0.05)
            try:
                resp = _send_propose(
                    srv,
                    {"name": "QUERY",
                     "parameters": {"intent": "string"},
                     "outcome": "results"},
                )
                self.assertEqual(resp.status_code, 463)
                body = json.loads(resp.body_bytes.decode("utf-8"))
                self.assertEqual(body["error"]["reason"], "policy-refused")
            finally:
                srv.stop()
        finally:
            tmp.cleanup()

    def test_persistent_synthesis_grants_default_duration(self):
        resp = _send_propose(
            self.server,
            {"name": "QUERY",
             "parameters": {"intent": "string"},
             "outcome": "results",
             "persistent": True},
        )
        self.assertEqual(resp.status_code, 263)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertTrue(body["persistent"])
        # Default persistent duration is 7d.
        self.assertEqual(body["granted_duration"], "7d")
        self.assertIsNotNone(body["expires_at"])

    def test_persistent_synthesis_caps_at_max(self):
        resp = _send_propose(
            self.server,
            {"name": "QUERY",
             "parameters": {"intent": "string"},
             "outcome": "results",
             "persistent": True,
             "requested_duration": "365d"},  # over max (30d)
        )
        self.assertEqual(resp.status_code, 263)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["granted_duration"], "30d")

    def test_persistent_synthesis_grants_requested_when_below_max(self):
        resp = _send_propose(
            self.server,
            {"name": "QUERY",
             "parameters": {"intent": "string"},
             "outcome": "results",
             "persistent": True,
             "requested_duration": "5d"},
        )
        self.assertEqual(resp.status_code, 263)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["granted_duration"], "5d")


# ===========================================================================
# Anonymous discovery gate (262).
# ===========================================================================


class AnonymousDiscoveryGateTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        # anonymous_discovery = False
        cls.config = ServerConfig(
            server=ServerInfo(server_id="t.local", operator="x", contact=""),
            policy=ServerPolicy(anonymous_discovery=False),
            agents=AgentsConfig(disclosure="public"),
        )
        cls.registry.config = cls.config
        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_unauthenticated_manifest_fetch_returns_262(self):
        sock = socket.create_connection(
            (self.server.host, self.server.port), timeout=5.0,
        )
        headers = {
            "Host": f"{self.server.host}:{self.server.port}",
        }
        req = wire.AGTPRequest(
            method="DISCOVER", headers=headers, body_bytes=b"",
        )
        try:
            sock.sendall(req.serialize())
            resp = wire.parse_response(sock.makefile("rb"))
        finally:
            sock.close()
        self.assertEqual(resp.status_code, 262)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(
            body["error"]["type"], "anonymous-discovery-disabled",
        )


if __name__ == "__main__":
    unittest.main()
