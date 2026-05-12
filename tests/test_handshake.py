"""
Tests for the matching handshake (Prompt 3) and soft-deny / wildcards
enforcement on the server.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wire
from server.config import AgentsConfig, ServerConfig, ServerInfo, ServerPolicy
from core.handshake import MatchOutcome, format_outcome, match, match_from_manifest_dict
from core.identity import AgentDocument, RequiresDeclaration, from_dict
from server.manifest import generate
from server.main import AgentRegistry, handle_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"
ORCH_ID = "9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"


def _read_agent(name: str) -> AgentDocument:
    return from_dict(json.loads(
        (REPO_ROOT / "server" / "agents" / name).read_text(encoding="utf-8")
    ))


# ---------------------------------------------------------------------------
# match() outcomes.
# ---------------------------------------------------------------------------


class MatchHandshakeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.lauren = _read_agent("lauren.agent.json")
        cls.orch = _read_agent("orchestrator.agent.json")
        cls.config = ServerConfig(
            server=ServerInfo(
                server_id="t.local", operator="x", contact=""
            ),
            policy=ServerPolicy(),
            agents=AgentsConfig(disclosure="public"),
        )
        cls.registry_agents = {cls.lauren.agent_id: cls.lauren}

    def test_full_match_when_server_exposes_everything_lauren_needs(self):
        manifest = generate(self.config, self.registry_agents)
        outcome = match(self.lauren, manifest)
        self.assertEqual(outcome.kind, "full")
        self.assertEqual(set(outcome.matched), set(self.lauren.requires.methods))
        self.assertEqual(outcome.missing, [])

    def test_partial_match_with_synthetic_extra_method(self):
        # Build an agent that needs a method nobody implements.
        agent = AgentDocument(
            agtp_version="1.0",
            agent_id="0" * 64,
            name="TestAgent",
            principal="Tester",
            principal_id="t-1",
            description="",
            status="active",
            skills=["test"],
            requires=RequiresDeclaration(
                methods=["QUERY", "INVENTNEW"],
                scopes=[],
                wildcards=False,
            ),
            scopes_accepted=[],
            issued_at="2026-05-07T00:00:00Z",
            issuer="t",
        )
        manifest = generate(self.config, self.registry_agents)
        outcome = match(agent, manifest)
        self.assertEqual(outcome.kind, "partial")
        self.assertEqual(outcome.matched, ["QUERY"])
        self.assertEqual(outcome.missing, ["INVENTNEW"])
        self.assertTrue(outcome.is_actionable)

    def test_none_match_when_no_methods_overlap(self):
        agent = AgentDocument(
            agtp_version="1.0", agent_id="1" * 64, name="X", principal="x",
            principal_id="x", description="", status="active", skills=[],
            requires=RequiresDeclaration(
                methods=["UNKNOWN1", "UNKNOWN2"], scopes=[], wildcards=False
            ),
            scopes_accepted=[],
            issued_at="2026-05-07T00:00:00Z", issuer="t",
        )
        manifest = generate(self.config, self.registry_agents)
        outcome = match(agent, manifest)
        self.assertEqual(outcome.kind, "none")
        self.assertEqual(outcome.matched, [])
        self.assertFalse(outcome.is_actionable)

    def test_wildcards_full_when_server_accepts_them(self):
        # Orchestrator declares wildcards=True; default config accepts them.
        manifest = generate(self.config, self.registry_agents)
        outcome = match(self.orch, manifest)
        self.assertEqual(outcome.kind, "full")
        # Wildcards expand the matched set to the whole universe.
        self.assertGreaterEqual(len(outcome.matched), 12)

    def test_wildcards_falls_back_to_explicit_when_server_refuses(self):
        cfg = ServerConfig(
            server=self.config.server,
            policy=ServerPolicy(wildcards_accepted=False),
            agents=self.config.agents,
        )
        manifest = generate(cfg, self.registry_agents)
        outcome = match(self.orch, manifest)
        # With wildcards refused, the agent's explicit methods drive the
        # outcome; orchestrator declares all 12 explicitly so it's still
        # full, but the agent_wants/server_accepts fields should reflect
        # the policy mismatch.
        self.assertEqual(outcome.kind, "full")
        self.assertTrue(outcome.agent_wants_wildcards)
        self.assertFalse(outcome.server_accepts_wildcards)
        # The format_outcome helper warns about the policy mismatch.
        self.assertIn("policy refuses", format_outcome(outcome))


# ---------------------------------------------------------------------------
# Soft-deny / wildcards enforcement on the live server.
# ---------------------------------------------------------------------------


class _Server:
    def __init__(self, registry, config, soft_deny=True):
        self.registry = registry
        self.config = config
        self.soft_deny = soft_deny
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
        try: self.sock.close()
        except OSError: pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout: continue
            except OSError: return
            threading.Thread(
                target=handle_connection,
                args=(conn, self.registry, self.config),
                kwargs={"soft_deny_enabled": self.soft_deny},
                daemon=True,
            ).start()


def _send(server: _Server, target: str, method: str, body=None):
    headers = {
        "Agent-ID": target,
        "Accept": "application/json",
        "Host": server.host,
    }
    if body:
        body_bytes = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    else:
        body_bytes = b""
    req = wire.AGTPRequest(method=method, headers=headers, body_bytes=body_bytes)
    sock = socket.create_connection((server.host, server.port), timeout=5.0)
    try:
        sock.sendall(req.serialize())
        return wire.parse_response(sock.makefile("rb"))
    finally:
        sock.close()


def _stage_agents(agents_dir: Path) -> None:
    src = REPO_ROOT / "server" / "agents"
    for name in ("lauren.agent.json", "orchestrator.agent.json"):
        (agents_dir / name).write_text(
            (src / name).read_text(encoding="utf-8"), encoding="utf-8"
        )


class SoftDenyEnforcementTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)

        cls.config_default = ServerConfig(
            server=ServerInfo(
                server_id="t.local", operator="x", contact=""
            ),
            policy=ServerPolicy(wildcards_accepted=True),
            agents=AgentsConfig(disclosure="public"),
        )
        cls.server = _Server(cls.registry, cls.config_default)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_soft_deny_403_for_undeclared_cognitive_method(self):
        # Lauren does not declare RECONCILE (it's a custom method) and
        # has wildcards=false; soft-deny fires before the dispatcher.
        # Load RECONCILE so it's a known method on the server.
        from server.examples import custom_methods
        custom_methods.install()
        try:
            resp = _send(
                self.server, LAUREN_ID, "RECONCILE",
                body={"account_id": "x", "period": "Q1"},
            )
            self.assertEqual(resp.status_code, 403)
            payload = json.loads(resp.body_bytes.decode("utf-8"))
            self.assertEqual(
                payload["error"]["code"], "method-not-permitted-for-agent"
            )
            self.assertEqual(payload["error"]["method"], "RECONCILE")
        finally:
            from server.methods import unregister
            unregister("RECONCILE")

    def test_mechanics_exempt_from_soft_deny(self):
        # Lauren declares CONFIRM and NOTIFY but not DELEGATE. DELEGATE
        # is a mechanic and exempt from soft-deny; the handler-local
        # check_capability returns 405, not 452.
        resp = _send(
            self.server, LAUREN_ID, "DELEGATE",
            body={"task": "x", "sub_agent": ORCH_ID},
        )
        self.assertEqual(resp.status_code, 405)
        payload = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "method-not-in-requires")

    def test_unknown_method_passes_soft_deny_to_dispatch_459(self):
        # FAKEMETHOD bypasses soft-deny (the gate exempts unknown
        # methods so the response surfaces "verb unrecognized"
        # rather than "agent doesn't declare X"). With the new
        # catalog-based validator the dispatcher then returns 459
        # Method Grammar Violation; older revisions returned 501.
        resp = _send(self.server, LAUREN_ID, "FAKEMETHOD", body={"x": 1})
        self.assertEqual(resp.status_code, 459)

    def test_no_soft_deny_flag_disables_403(self):
        cfg = self.config_default
        srv = _Server(self.registry, cfg, soft_deny=False)
        srv.start()
        time.sleep(0.05)
        try:
            from server.examples import custom_methods
            custom_methods.install()
            try:
                resp = _send(
                    srv, LAUREN_ID, "RECONCILE",
                    body={"account_id": "x", "period": "Q1"},
                )
                # Without soft-deny the handler-local check fires (405).
                self.assertEqual(resp.status_code, 405)
            finally:
                from server.methods import unregister
                unregister("RECONCILE")
        finally:
            srv.stop()


class WildcardsEnforcementTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _server(self, *, wildcards_accepted: bool) -> _Server:
        cfg = ServerConfig(
            server=ServerInfo(
                server_id="t.local", operator="x", contact=""
            ),
            policy=ServerPolicy(wildcards_accepted=wildcards_accepted),
            agents=AgentsConfig(disclosure="public"),
        )
        s = _Server(self.registry, cfg)
        s.start()
        time.sleep(0.05)
        return s

    def test_wildcard_agent_can_invoke_any_embedded_method_when_accepted(self):
        srv = self._server(wildcards_accepted=True)
        try:
            resp = _send(srv, ORCH_ID, "QUERY", body={"intent": "hi"})
            self.assertEqual(resp.status_code, 200)
        finally:
            srv.stop()

    def test_262_when_wildcard_agent_invokes_custom_method_without_accept(self):
        # §7: wildcards-refused consolidates under 262 Authorization
        # Required (error.type='wildcards-required'). Pre-§7 wire
        # status was 403.
        from server.examples import custom_methods
        custom_methods.install()
        srv = self._server(wildcards_accepted=False)
        try:
            resp = _send(
                srv, ORCH_ID, "RECONCILE",
                body={"account_id": "x", "period": "Q1"},
            )
            self.assertEqual(resp.status_code, 262)
            payload = json.loads(resp.body_bytes.decode("utf-8"))
            self.assertEqual(payload["error"]["code"], "authorization-required")
            self.assertEqual(payload["error"]["type"], "wildcards-required")
        finally:
            srv.stop()
            from server.methods import unregister
            unregister("RECONCILE")

    def test_embedded_method_proceeds_under_wildcards_refused(self):
        # When wildcards_accepted=False, embedded methods still flow:
        # they only fall under wildcards-refused when non-embedded.
        srv = self._server(wildcards_accepted=False)
        try:
            resp = _send(srv, ORCH_ID, "QUERY", body={"intent": "ok"})
            self.assertEqual(resp.status_code, 200)
        finally:
            srv.stop()


# ---------------------------------------------------------------------------
# --match-check end-to-end.
# ---------------------------------------------------------------------------


class MatchCheckCLITests(unittest.TestCase):

    def test_match_check_against_live_server(self):
        # Spin up `python -m server` on a free port and run the
        # client with --match-check. This exercises the full pipeline:
        # DESCRIBE + manifest DISCOVER + match.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        agents_dir = REPO_ROOT / "server" / "agents"
        proc = subprocess.Popen(
            [
                PYTHON, "-m", "server", str(port),
                "--host", "127.0.0.1",
                "--agents-dir", str(agents_dir),
            ],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Wait for listen.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            proc.terminate()
            self.fail("server did not come up")

        try:
            out = subprocess.run(
                [
                    PYTHON, "-m", "client",
                    f"agtp://{LAUREN_ID}@127.0.0.1:{port}",
                    "--match-check",
                    "--insecure",
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("Match: FULL", out.stdout)
            self.assertIn("Matched", out.stdout)
            self.assertIn("Server has", out.stdout)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try: stream.close()
                    except OSError: pass


if __name__ == "__main__":
    unittest.main()
