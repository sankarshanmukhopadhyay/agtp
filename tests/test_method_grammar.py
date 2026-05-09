"""
Tests for the Method-Grammar header runtime pathway.

When an unrecognized method name arrives carrying ``Method-Grammar:
AMG/1.0``, the server validates the name against AMG's three
name-targeted passes (lexical, reserved, stoplist), checks that the
agent and server policies admit wildcard verbs, and returns one of:

  * 200 OK with a structured "amg-conformant; PROPOSE this method"
    body (the happy path),
  * 459 Grammar Violation (name failed AMG validation),
  * 403 Wildcards Refused (agent or server declined the open
    vocabulary),
  * 501 Not Implemented (Method-Grammar header absent or malformed).

Tests cover regression cases (existing behavior unchanged), positive
cases (the runtime pathway works), grammar-rejection cases, authority
cases, and the deprecated ``AGIS/1.0`` header value.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wire
from server.config import AgentsConfig, ServerConfig, ServerInfo, ServerPolicy
from server.main import AgentRegistry, handle_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"
ORCH_ID   = "9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"


# ---------------------------------------------------------------------------
# Test server fixture (mirrors the pattern from test_handshake.py).
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


def _send(server: _Server, target: str, method: str, *,
          method_grammar: str = "", body=None):
    headers = {
        "Target-Agent": target,
        "Accept": "application/json",
        "Host": server.host,
    }
    if method_grammar:
        headers["Method-Grammar"] = method_grammar
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


def _decode_json(resp):
    return json.loads(resp.body_bytes.decode("utf-8"))


# ---------------------------------------------------------------------------
# Regression tests — existing behavior unchanged.
# ---------------------------------------------------------------------------


class MethodGrammarRegressionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = ServerConfig(
            server=ServerInfo(
                issuer="t.local", operator="x", contact="", amg_version="1.0"
            ),
            policy=ServerPolicy(),
            agents=AgentsConfig(disclosure="public"),
        )
        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_method_grammar_known_method_dispatches_normally(self):
        # QUERY is embedded; presence of the Method-Grammar header
        # must not affect dispatch of a known method.
        resp = _send(
            self.server, ORCH_ID, "QUERY",
            method_grammar="AMG/1.0",
            body={"intent": "hello"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_method_grammar_unknown_no_header_returns_501(self):
        resp = _send(self.server, ORCH_ID, "FAKEMETHOD")
        self.assertEqual(resp.status_code, 501)
        payload = _decode_json(resp)
        self.assertEqual(payload["error"]["code"], "method-not-implemented")

    def test_method_grammar_unknown_malformed_header_returns_501(self):
        # ``garbage/9.9`` is not a recognized grammar; treat as if absent.
        resp = _send(
            self.server, ORCH_ID, "FAKEMETHOD",
            method_grammar="garbage/9.9",
        )
        self.assertEqual(resp.status_code, 501)


# ---------------------------------------------------------------------------
# Positive cases — the runtime pathway works.
# ---------------------------------------------------------------------------


class MethodGrammarPositiveTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = ServerConfig(
            server=ServerInfo(
                issuer="t.local", operator="x", contact="", amg_version="1.0"
            ),
            policy=ServerPolicy(),  # wildcards_accepted=True, negotiable=True
            agents=AgentsConfig(disclosure="public"),
        )
        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_method_grammar_amg_header_returns_invitation(self):
        # ORCH has wildcards=true; server allows wildcards & negotiation.
        resp = _send(
            self.server, ORCH_ID, "RECONCILE",
            method_grammar="AMG/1.0",
        )
        self.assertEqual(resp.status_code, 200)
        payload = _decode_json(resp)
        self.assertEqual(payload["method"], "RECONCILE")
        self.assertEqual(payload["status"], "amg-conformant")
        self.assertFalse(payload["executable"])
        self.assertEqual(payload["next_action"], "PROPOSE")
        self.assertTrue(payload["negotiable"])
        self.assertIn("PROPOSE", payload["explanation"])
        # No Warning header for AMG/1.0.
        self.assertEqual(wire.header(resp, "Warning"), "")

    def test_method_grammar_agis_header_emits_deprecation_warning(self):
        # AGIS/1.0 still works (legacy draft compat) but produces a
        # 299 Warning header advising the client to upgrade.
        resp = _send(
            self.server, ORCH_ID, "RECONCILE",
            method_grammar="AGIS/1.0",
        )
        self.assertEqual(resp.status_code, 200)
        warn = wire.header(resp, "Warning")
        self.assertIn("AGIS/1.0", warn)
        self.assertIn("deprecated", warn.lower())
        self.assertIn("AMG/1.0", warn)

    def test_method_grammar_header_value_is_case_insensitive(self):
        resp = _send(
            self.server, ORCH_ID, "RECONCILE",
            method_grammar="amg/1.0",
        )
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# Grammar-rejection cases — 459 with the failed pass.
# ---------------------------------------------------------------------------


class MethodGrammarRejectionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = ServerConfig(
            server=ServerInfo(
                issuer="t.local", operator="x", contact="", amg_version="1.0"
            ),
            policy=ServerPolicy(),
            agents=AgentsConfig(disclosure="public"),
        )
        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_method_grammar_lowercase_returns_459_lexical(self):
        # "status" fails lexical (lowercase). The wire actually
        # uppercases the method on parse; to exercise the lexical
        # pass we need a digit/punctuation case instead.
        resp = _send(
            self.server, ORCH_ID, "AB",  # too short
            method_grammar="AMG/1.0",
        )
        self.assertEqual(resp.status_code, 459)
        payload = _decode_json(resp)
        self.assertEqual(payload["error"]["code"], "grammar-violation")
        self.assertEqual(payload["error"]["pass_name"], "lexical")
        self.assertEqual(payload["error"]["amg_code"], "malformed-name")

    def test_method_grammar_http_method_returns_459_reserved(self):
        resp = _send(
            self.server, ORCH_ID, "GET",
            method_grammar="AMG/1.0",
        )
        self.assertEqual(resp.status_code, 459)
        payload = _decode_json(resp)
        self.assertEqual(payload["error"]["pass_name"], "reserved")
        self.assertEqual(payload["error"]["amg_code"], "reserved-http-method")

    def test_method_grammar_stoplist_returns_459_stoplist(self):
        resp = _send(
            self.server, ORCH_ID, "STATUS",
            method_grammar="AMG/1.0",
        )
        self.assertEqual(resp.status_code, 459)
        payload = _decode_json(resp)
        self.assertEqual(payload["error"]["pass_name"], "stoplist")
        self.assertEqual(payload["error"]["amg_code"], "non-action-intent")
        self.assertEqual(payload["error"]["method"], "STATUS")


# ---------------------------------------------------------------------------
# Authority cases — 403 wildcards-refused for agent or server policy.
# ---------------------------------------------------------------------------


class MethodGrammarAuthorityTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _server(
        self,
        *,
        wildcards_accepted: bool = True,
        negotiable: bool = True,
    ) -> _Server:
        cfg = ServerConfig(
            server=ServerInfo(
                issuer="t.local", operator="x", contact="", amg_version="1.0"
            ),
            policy=ServerPolicy(
                wildcards_accepted=wildcards_accepted,
                negotiable=negotiable,
            ),
            agents=AgentsConfig(disclosure="public"),
        )
        s = _Server(self.registry, cfg)
        s.start()
        time.sleep(0.05)
        return s

    def test_method_grammar_agent_without_wildcards_gets_403(self):
        # Lauren has wildcards=false; Method-Grammar pathway is gated.
        srv = self._server()
        try:
            resp = _send(
                srv, LAUREN_ID, "RECONCILE",
                method_grammar="AMG/1.0",
            )
            self.assertEqual(resp.status_code, 403)
            payload = _decode_json(resp)
            self.assertEqual(payload["error"]["code"], "wildcards-refused")
            self.assertIn("wildcards", payload["error"]["explanation"].lower())
        finally:
            srv.stop()

    def test_method_grammar_server_refusing_wildcards_gets_403(self):
        # Orchestrator has wildcards=true; server policy says no.
        srv = self._server(wildcards_accepted=False)
        try:
            resp = _send(
                srv, ORCH_ID, "RECONCILE",
                method_grammar="AMG/1.0",
            )
            self.assertEqual(resp.status_code, 403)
            payload = _decode_json(resp)
            self.assertEqual(payload["error"]["code"], "wildcards-refused")
            self.assertIn(
                "wildcards_accepted",
                payload["error"]["explanation"].lower(),
            )
        finally:
            srv.stop()

    def test_method_grammar_negotiable_false_changes_invitation(self):
        srv = self._server(negotiable=False)
        try:
            resp = _send(
                srv, ORCH_ID, "RECONCILE",
                method_grammar="AMG/1.0",
            )
            self.assertEqual(resp.status_code, 200)
            payload = _decode_json(resp)
            self.assertFalse(payload["negotiable"])
            self.assertEqual(
                payload["next_action"], "no_negotiation_available"
            )
            self.assertNotIn(
                "Issue a PROPOSE", payload["explanation"]
            )
        finally:
            srv.stop()


# ---------------------------------------------------------------------------
# validate_name_only: direct unit tests.
# ---------------------------------------------------------------------------


class ValidateNameOnlyTests(unittest.TestCase):

    def test_valid_name_returns_none(self):
        from client.amg.validator import validate_name_only
        self.assertIsNone(validate_name_only("RECONCILE"))

    def test_empty_name_fails_lexical(self):
        from client.amg.validator import validate_name_only
        err = validate_name_only("")
        self.assertIsNotNone(err)
        self.assertEqual(err.pass_name, "lexical")

    def test_lowercase_fails_lexical(self):
        from client.amg.validator import validate_name_only
        err = validate_name_only("reconcile")
        self.assertEqual(err.pass_name, "lexical")
        self.assertEqual(err.code, "malformed-name")

    def test_http_method_fails_reserved(self):
        from client.amg.validator import validate_name_only
        err = validate_name_only("GET")
        self.assertEqual(err.pass_name, "reserved")
        self.assertEqual(err.code, "reserved-http-method")

    def test_embedded_method_fails_reserved(self):
        from client.amg.validator import validate_name_only
        err = validate_name_only("QUERY")
        self.assertEqual(err.pass_name, "reserved")
        self.assertEqual(err.code, "reserved-embedded-method")

    def test_stoplist_fails_stoplist(self):
        from client.amg.validator import validate_name_only
        err = validate_name_only("STATUS")
        self.assertEqual(err.pass_name, "stoplist")
        self.assertEqual(err.code, "non-action-intent")

    def test_client_and_server_validate_name_only_agree(self):
        # Drift gate covers structural parity; this confirms behavioral
        # parity for the new entry point.
        from client.amg.validator import validate_name_only as client_fn
        from server.amg.validator import validate_name_only as server_fn
        for name in ("RECONCILE", "reconcile", "GET", "QUERY", "STATUS", ""):
            self.assertEqual(
                client_fn(name) is None,
                server_fn(name) is None,
                msg=f"divergence on {name!r}",
            )


if __name__ == "__main__":
    unittest.main()
