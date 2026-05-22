"""
End-to-end tests for the dispatcher's catalog-based validation gates.

Resolution order:

  1. Synthesis-Id: routes to the runtime if active.
  2. **459 Method Violation**: method not in
     ``core/methods.json``.
  3. **460 Endpoint Violation**: path malformed or contains
     a verb token.
  4. **405 Method Not Allowed**: per-server policies.methods
     refuses the verb.
  5. Redirect: policies.methods.redirects rewrites (method, path)
     before dispatch.
  6. Registry lookup: handler resolves and runs.

Tests below cover each gate firing in isolation; tests covering the
embedded methods themselves live in ``test_methods.py`` and the
synthesis flow lives in ``test_synthesis_runtime.py``.
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
from server.config import (
    AgentsConfig,
    ServerConfig,
    ServerInfo,
    ServerPolicy,
    methods_policy_from_table,
)
from server.main import AgentRegistry, handle_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"
ORCH_ID   = "9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"


# ---------------------------------------------------------------------------
# Test server fixture (mirrors test_handshake.py).
# ---------------------------------------------------------------------------


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
                daemon=True,
            ).start()


def _send(server, target, method, *, body=None):
    headers = {"Agent-ID": target, "Accept": "application/json",
               "Host": server.host}
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


def _stage_agents(agents_dir):
    src = REPO_ROOT / "server" / "agents"
    for name in ("lauren.agent.json", "orchestrator.agent.json"):
        (agents_dir / name).write_text(
            (src / name).read_text(encoding="utf-8"), encoding="utf-8"
        )


def _decode_json(resp):
    return json.loads(resp.body_bytes.decode("utf-8"))


# ===========================================================================
# 459 Method Violation
# ===========================================================================


class MethodGrammarValidationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = ServerConfig(
            server=ServerInfo(
                server_id="t.local", operator="x", contact="",
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

    def test_unknown_verb_returns_459(self):
        resp = _send(self.server, ORCH_ID, "FROBNICATE")
        self.assertEqual(resp.status_code, 459)
        payload = _decode_json(resp)
        self.assertEqual(payload["error"]["code"], "method-violation")
        self.assertEqual(payload["error"]["method"], "FROBNICATE")

    def test_459_includes_close_match_suggestions(self):
        # PROPOSEX is one Levenshtein step from PROPOSE.
        resp = _send(self.server, ORCH_ID, "PROPOSEX")
        self.assertEqual(resp.status_code, 459)
        payload = _decode_json(resp)
        self.assertIn("PROPOSE", payload["error"].get("suggestions", []))

    def test_legacy_verb_resolved_via_default_alias_seed(self):
        # RCNS-5: default methods policy seeds the alias table with the
        # five legacy HTTP verbs mapped to their AGTP canonicals (GET ->
        # FETCH, POST -> CREATE, etc.). A caller sending wire-level GET
        # now resolves to FETCH at the dispatcher gate before the
        # catalog check. FETCH is in the catalog → passes 459; no
        # handler is registered on the test fixture → 405 method-not-
        # implemented (the next gate down). Operators wanting the
        # pre-RCNS-5 strict 459 declare ``[policies.methods.aliases]``
        # as an empty table to wipe the seed.
        resp = _send(self.server, ORCH_ID, "GET")
        self.assertEqual(resp.status_code, 405)
        payload = _decode_json(resp)
        self.assertEqual(
            payload["error"]["code"], "method-not-implemented",
        )

    def test_unknown_verb_with_no_alias_still_returns_459(self):
        # A verb that's neither in the catalog nor in the alias table
        # still surfaces the helpful 459 with close-match suggestions.
        # This is the path-to-typo case the dispatcher has always
        # served and stays intact under RCNS-5.
        resp = _send(self.server, ORCH_ID, "FROBNICATE")
        self.assertEqual(resp.status_code, 459)

    def test_embedded_verb_passes_catalog_gate(self):
        # QUERY is embedded; the gate must let it through.
        resp = _send(self.server, ORCH_ID, "QUERY", body={"intent": "ping"})
        self.assertEqual(resp.status_code, 200)


# ===========================================================================
# Per-server policy: legacy opt-in + disallow + redirect
# ===========================================================================


class MethodsPolicyDispatchTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _server(self, policy_table: dict) -> _Server:
        # Replace the registry's default policy so the dispatcher
        # exercises the configured rules. The table mirrors the
        # ``[policies.methods]`` block authored in agtp-server.toml.
        self.registry.methods_policy = methods_policy_from_table(
            policy_table, source="<test>"
        )
        config = ServerConfig(
            server=ServerInfo(
                server_id="t.local", operator="x", contact="",
            ),
            policy=ServerPolicy(),
            agents=AgentsConfig(disclosure="public"),
        )
        s = _Server(self.registry, config)
        s.start()
        time.sleep(0.05)
        return s

    def test_legacy_opt_in_admits_get(self):
        # ``legacy = ["GET"]`` opts the server into accepting GET.
        # The server has no GET handler, so the registry lookup misses
        # and we expect a 405 (method valid, no handler) rather
        # than 459 (verb not in catalog).
        srv = self._server({"allow": "*", "legacy": ["GET"]})
        try:
            resp = _send(srv, ORCH_ID, "GET")
            self.assertNotEqual(resp.status_code, 459)
            self.assertEqual(resp.status_code, 405)
        finally:
            srv.stop()

    def test_disallow_refuses_a_specific_verb(self):
        # AUDIT is in the catalog. Disallow it; the dispatcher
        # refuses with 405 method-not-allowed-by-policy.
        srv = self._server({"allow": "*", "disallow": ["AUDIT"]})
        try:
            resp = _send(srv, ORCH_ID, "AUDIT")
            self.assertEqual(resp.status_code, 405)
            payload = _decode_json(resp)
            self.assertEqual(
                payload["error"]["code"], "method-not-allowed-by-policy",
            )
        finally:
            srv.stop()

    def test_disallow_does_not_block_embedded_method(self):
        # Embedded methods (the 12 protocol primitives) bypass the
        # policy gate so a mis-authored disallow can't take a server
        # off the protocol surface.
        srv = self._server({"allow": "*", "disallow": ["QUERY"]})
        try:
            resp = _send(srv, ORCH_ID, "QUERY", body={"intent": "ping"})
            self.assertEqual(resp.status_code, 200)
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
