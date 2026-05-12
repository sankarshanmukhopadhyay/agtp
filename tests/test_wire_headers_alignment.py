"""
§10 wire format and header model tests (agtp §10).

Covers:

  * **Agent-ID rename** — canonical header name is ``Agent-ID``.
    The pre-§10 ``Target-Agent`` still works via back-compat
    fallback but emits a DeprecationWarning.
  * **Authority-Scope** — claimed scopes are validated against the
    agent's declared ``requires.scopes``. Invalid claims return
    262 ``scope-claim-invalid``; valid claims pass through to
    the handler.
  * **Session-ID / Task-ID** pass-through into EndpointContext for
    handler-level use.
  * **Task-ID echo** in the response (so callers can correlate
    multi-request traces).
  * **Delegation-Chain** rejected with 501 Not Implemented (the
    header is reserved for v01).
  * **Server-ID** mandatory in every response.
  * **Attribution-Record** optional, opt-in via
    ``[audit] attribution_records_enabled``.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wire
from core.identity import AgentDocument, RequiresDeclaration
from server.config import (
    AgentsConfig, AuditConfig, ServerConfig, ServerInfo, ServerPolicy,
)
from server.main import AgentRegistry, handle_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCH_ID = "9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"


# ===========================================================================
# wire.read_agent_id — back-compat fallback.
# ===========================================================================


class WireReadAgentIdTests(unittest.TestCase):

    def _request(self, headers: dict) -> wire.AGTPRequest:
        return wire.AGTPRequest(
            method="DESCRIBE",
            headers=headers,
            body_bytes=b"",
            path="/",
        )

    def test_agent_id_header_returned(self):
        req = self._request({"Agent-ID": "abc"})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(wire.read_agent_id(req), "abc")
        # No deprecation warning when the canonical name is used.
        dep = [w for w in caught
               if issubclass(w.category, DeprecationWarning)]
        self.assertEqual(dep, [])

    def test_target_agent_fallback_with_warning(self):
        req = self._request({"Target-Agent": "abc"})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(wire.read_agent_id(req), "abc")
        dep = [w for w in caught
               if issubclass(w.category, DeprecationWarning)
               and "Target-Agent" in str(w.message)]
        self.assertEqual(len(dep), 1)

    def test_agent_id_wins_when_both_present(self):
        req = self._request({"Agent-ID": "new", "Target-Agent": "legacy"})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(wire.read_agent_id(req), "new")
        # Agent-ID is set, so the legacy fallback never fires; no
        # deprecation warning emitted.
        dep = [w for w in caught
               if issubclass(w.category, DeprecationWarning)]
        self.assertEqual(dep, [])

    def test_returns_default_when_neither_present(self):
        req = self._request({})
        self.assertEqual(wire.read_agent_id(req), "")
        self.assertEqual(wire.read_agent_id(req, default="anon"), "anon")


# ===========================================================================
# End-to-end server fixture.
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

    def start(self): self._thread.start()

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


def _send(server, method, headers, body=None, path=None):
    sock = socket.create_connection((server.host, server.port), timeout=5.0)
    body_bytes = (
        json.dumps(body).encode("utf-8") if body is not None else b""
    )
    full_headers = {
        "Host": f"{server.host}:{server.port}",
        "Content-Type": "application/json",
        "Content-Length": str(len(body_bytes)),
    }
    full_headers.update(headers or {})
    req = wire.AGTPRequest(
        method=method,
        headers=full_headers,
        body_bytes=body_bytes,
        path=path or "/",
    )
    try:
        sock.sendall(req.serialize())
        return wire.parse_response(sock.makefile("rb"))
    finally:
        sock.close()


def _config(
    *, server_id="srv.local", audit=None, anonymous_discovery=True,
) -> ServerConfig:
    return ServerConfig(
        server=ServerInfo(server_id=server_id, operator="x", contact=""),
        policy=ServerPolicy(anonymous_discovery=anonymous_discovery),
        agents=AgentsConfig(disclosure="public"),
        audit=audit or AuditConfig(),
    )


# ===========================================================================
# End-to-end: Agent-ID rename + back-compat.
# ===========================================================================


class AgentIdHeaderEndToEndTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = _config()
        cls.registry.config = cls.config
        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_agent_id_header_routes_to_agent(self):
        resp = _send(self.server, "DESCRIBE", {"Agent-ID": ORCH_ID})
        self.assertEqual(resp.status_code, 200)

    def test_target_agent_back_compat_still_routes(self):
        resp = _send(self.server, "DESCRIBE", {"Target-Agent": ORCH_ID})
        # The legacy header continues to resolve the agent.
        self.assertEqual(resp.status_code, 200)


# ===========================================================================
# Server-ID mandatory response header.
# ===========================================================================


class ServerIdResponseHeaderTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = _config(server_id="srv.test.local")
        cls.registry.config = cls.config
        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_server_id_on_success(self):
        resp = _send(self.server, "DESCRIBE", {"Agent-ID": ORCH_ID})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            wire.header(resp, "Server-ID"), "srv.test.local",
        )

    def test_server_id_on_manifest(self):
        # Target-less DISCOVER → manifest. Response carries Server-ID.
        resp = _send(self.server, "DISCOVER", {})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            wire.header(resp, "Server-ID"), "srv.test.local",
        )

    def test_server_id_on_error_path(self):
        # Unknown agent — 404 from _select_target — still carries Server-ID.
        resp = _send(self.server, "DESCRIBE", {"Agent-ID": "nope"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(
            wire.header(resp, "Server-ID"), "srv.test.local",
        )


# ===========================================================================
# Task-ID echo.
# ===========================================================================


class TaskIdEchoTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = _config()
        cls.registry.config = cls.config
        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_task_id_echoed_in_response(self):
        resp = _send(self.server, "DESCRIBE", {
            "Agent-ID": ORCH_ID,
            "Task-ID": "task-abc-123",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(wire.header(resp, "Task-ID"), "task-abc-123")

    def test_no_task_id_means_no_echo(self):
        resp = _send(self.server, "DESCRIBE", {"Agent-ID": ORCH_ID})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(wire.header(resp, "Task-ID"), "")


# ===========================================================================
# Delegation-Chain rejected with 501.
# ===========================================================================


class DelegationChainRejectionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = _config()
        cls.registry.config = cls.config
        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_delegation_chain_returns_501(self):
        resp = _send(self.server, "DESCRIBE", {
            "Agent-ID": ORCH_ID,
            "Delegation-Chain": "agent-a -> agent-b",
        })
        self.assertEqual(resp.status_code, 501)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "delegation-not-supported")

    def test_delegation_chain_response_carries_server_id(self):
        # Even the 501-reject path carries the mandatory Server-ID
        # header — _finalize_response runs for every outbound response.
        resp = _send(self.server, "DESCRIBE", {
            "Agent-ID": ORCH_ID,
            "Delegation-Chain": "x",
        })
        self.assertEqual(resp.status_code, 501)
        self.assertTrue(wire.header(resp, "Server-ID"))


# ===========================================================================
# Authority-Scope claim validation.
# ===========================================================================


class AuthorityScopeValidationTests(unittest.TestCase):
    """The orchestrator agent declares specific scopes; Authority-Scope
    claims must be a subset of those."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = _config()
        cls.registry.config = cls.config
        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_no_authority_scope_header_passes(self):
        # Baseline: a request without Authority-Scope just dispatches.
        resp = _send(self.server, "DESCRIBE", {"Agent-ID": ORCH_ID})
        self.assertEqual(resp.status_code, 200)

    def test_authority_scope_unknown_returns_262_scope_claim_invalid(self):
        # Orchestrator doesn't declare ``world-domination`` so the
        # claim must be refused.
        resp = _send(self.server, "DESCRIBE", {
            "Agent-ID": ORCH_ID,
            "Authority-Scope": "world-domination, another-fake",
        })
        self.assertEqual(resp.status_code, 262)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "authorization-required")
        self.assertEqual(body["error"]["type"], "scope-required")
        details = body["error"]["details"]
        self.assertEqual(details["code"], "scope-claim-invalid")
        self.assertEqual(
            sorted(details["invalid"]),
            ["another-fake", "world-domination"],
        )


# ===========================================================================
# Attribution-Record opt-in.
# ===========================================================================


class AttributionRecordTests(unittest.TestCase):

    def _make_server(self, *, audit):
        tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(tmp.name)
        _stage_agents(agents_dir)
        registry = AgentRegistry(agents_dir)
        config = _config(audit=audit)
        registry.config = config
        srv = _Server(registry, config)
        srv.start()
        time.sleep(0.05)
        return tmp, srv

    def test_absent_when_disabled(self):
        tmp, srv = self._make_server(
            audit=AuditConfig(attribution_records_enabled=False),
        )
        try:
            resp = _send(srv, "DESCRIBE", {"Agent-ID": ORCH_ID})
            self.assertEqual(wire.header(resp, "Attribution-Record"), "")
        finally:
            srv.stop()
            tmp.cleanup()

    def test_present_when_enabled(self):
        tmp, srv = self._make_server(
            audit=AuditConfig(attribution_records_enabled=True),
        )
        try:
            resp = _send(srv, "DESCRIBE", {"Agent-ID": ORCH_ID})
            record = wire.header(resp, "Attribution-Record")
            self.assertTrue(record)
            parsed = json.loads(record)
            self.assertEqual(parsed["status"], resp.status_code)
            self.assertIn("server_id", parsed)
            self.assertIn("issued_at", parsed)
            # v00 placeholder: signature is "placeholder" until §5
            # JWS infrastructure lands.
            self.assertEqual(parsed["signature"], "placeholder")
        finally:
            srv.stop()
            tmp.cleanup()


# ===========================================================================
# EndpointContext: Session-ID / Task-ID pass-through.
# ===========================================================================


class EndpointContextPassThroughTests(unittest.TestCase):
    """The dispatcher populates EndpointContext from §10 headers so
    handlers don't have to re-parse."""

    def test_authority_scope_session_task_populated(self):
        from agtp.handlers import EndpointContext
        # Smoke-construct the context via the dispatcher's builder
        # directly (the same path the server uses at request time).
        from server.methods import _build_endpoint_context
        from core.endpoint import (
            EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
        )

        agent = AgentDocument(
            agtp_version="1.0", agent_id="a", name="A", principal="p",
            principal_id="p", description="", status="active",
            skills=[],
            requires=RequiresDeclaration(
                methods=["QUERY"], scopes=["sc1", "sc2"], wildcards=False,
            ),
            scopes_accepted=[],
            issued_at="2026-05-09T00:00:00Z", issuer="test",
        )
        spec = EndpointSpec(
            name="QUERY", path="/x",
            semantic=SemanticBlock(
                intent="x" * 25, actor="agent", outcome="y" * 25,
                capability="retrieval", confidence=0.9,
                impact="informational", is_idempotent=True,
            ),
            required_params=[
                ParamSpec(name="intent", type="string", description="d"),
            ],
        )
        req = wire.AGTPRequest(
            method="QUERY",
            headers={
                "Agent-ID": "a",
                "Authority-Scope": "sc1, sc2",
                "Session-ID": "sess-1",
                "Task-ID": "task-1",
            },
            body_bytes=b"",
            path="/x",
        )
        ctx = _build_endpoint_context(req, spec, {"intent": "x"}, agent, None)
        self.assertEqual(ctx.authority_scope, ["sc1", "sc2"])
        self.assertEqual(ctx.session_id, "sess-1")
        self.assertEqual(ctx.task_id, "task-1")

    def test_missing_optional_headers_yield_defaults(self):
        from server.methods import _build_endpoint_context
        from core.endpoint import (
            EndpointSpec, ParamSpec, SemanticBlock,
        )

        agent = AgentDocument(
            agtp_version="1.0", agent_id="a", name="A", principal="p",
            principal_id="p", description="", status="active",
            skills=[],
            requires=RequiresDeclaration(
                methods=["QUERY"], scopes=[], wildcards=False,
            ),
            scopes_accepted=[],
            issued_at="2026-05-09T00:00:00Z", issuer="test",
        )
        spec = EndpointSpec(
            name="QUERY", path="/x",
            semantic=SemanticBlock(
                intent="x" * 25, actor="agent", outcome="y" * 25,
                capability="retrieval", confidence=0.9,
                impact="informational", is_idempotent=True,
            ),
            required_params=[
                ParamSpec(name="intent", type="string", description="d"),
            ],
        )
        req = wire.AGTPRequest(
            method="QUERY", headers={"Agent-ID": "a"},
            body_bytes=b"", path="/x",
        )
        ctx = _build_endpoint_context(req, spec, {"intent": "x"}, agent, None)
        self.assertEqual(ctx.authority_scope, [])
        self.assertIsNone(ctx.session_id)
        self.assertIsNone(ctx.task_id)


if __name__ == "__main__":
    unittest.main()
