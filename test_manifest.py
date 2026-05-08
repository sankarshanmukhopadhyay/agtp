"""
Tests for the URI parser (Form 2), the Server Manifest, and the
TOML config loader. These are the deliverables of Prompt 1.

In-process server is reused from test_methods.py via direct imports
so test runs stay quick.
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agtp import wire
from agtp.config import (
    AgentsConfig,
    ServerConfig,
    ServerInfo,
    ServerPolicy,
    default_config,
    load as load_config,
)
from agtp.identity import (
    CONTENT_TYPE_MANIFEST_JSON,
    AgentDocument,
    from_dict,
)
from agtp.ids import AgentIDError, parse_uri, format_uri
from agtp.manifest import generate
from agtp.server import AgentRegistry, handle_connection


REPO_ROOT = Path(__file__).resolve().parent
LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"
ORCH_ID = "9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"


# ---------------------------------------------------------------------------
# URI parser tests.
# ---------------------------------------------------------------------------


class URIFormTests(unittest.TestCase):

    def test_form_1_bare_agent(self):
        p = parse_uri(f"agtp://{LAUREN_ID}")
        self.assertEqual(p.agent_id, LAUREN_ID)
        self.assertIsNone(p.host)
        self.assertFalse(p.is_server_level)

    def test_form_1a_agent_with_host(self):
        p = parse_uri(f"agtp://{LAUREN_ID}@agents.example.com:4480")
        self.assertEqual(p.agent_id, LAUREN_ID)
        self.assertEqual(p.host, "agents.example.com")
        self.assertEqual(p.port, 4480)
        self.assertFalse(p.is_server_level)

    def test_form_2_server_only(self):
        p = parse_uri("agtp://example.com")
        self.assertIsNone(p.agent_id)
        self.assertEqual(p.host, "example.com")
        self.assertIsNone(p.port)
        self.assertTrue(p.is_server_level)

    def test_form_2_with_port(self):
        p = parse_uri("agtp://example.com:4480")
        self.assertIsNone(p.agent_id)
        self.assertEqual(p.host, "example.com")
        self.assertEqual(p.port, 4480)
        self.assertTrue(p.is_server_level)
        self.assertEqual(p.effective_port, 4480)

    def test_form_2_127_dot_quad(self):
        p = parse_uri("agtp://127.0.0.1:4480")
        self.assertIsNone(p.agent_id)
        self.assertEqual(p.host, "127.0.0.1")
        self.assertEqual(p.port, 4480)
        self.assertTrue(p.is_server_level)

    def test_form_2_localhost(self):
        p = parse_uri("agtp://localhost")
        self.assertIsNone(p.agent_id)
        self.assertEqual(p.host, "localhost")
        self.assertIsNone(p.port)
        self.assertEqual(p.effective_port, 4480)

    def test_form_2_rejects_query_string(self):
        # Server URIs do not currently take query parameters.
        with self.assertRaises(AgentIDError):
            parse_uri("agtp://example.com?format=json")

    def test_form_2_rejects_underscore(self):
        with self.assertRaises(AgentIDError):
            parse_uri("agtp://has_underscore")

    def test_format_uri_form_2(self):
        self.assertEqual(format_uri(host="example.com"), "agtp://example.com")
        self.assertEqual(
            format_uri(host="example.com", port=8443),
            "agtp://example.com:8443",
        )
        # Default port omitted.
        self.assertEqual(
            format_uri(host="example.com", port=4480),
            "agtp://example.com",
        )

    def test_format_uri_requires_id_or_host(self):
        with self.assertRaises(AgentIDError):
            format_uri()


# ---------------------------------------------------------------------------
# Config loader tests.
# ---------------------------------------------------------------------------


class ConfigLoaderTests(unittest.TestCase):

    def test_default_config_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd_before = Path.cwd()
            os.chdir(tmp)
            try:
                cfg = load_config(None, host="127.0.0.1")
                self.assertTrue(cfg.is_default)
                self.assertEqual(cfg.server.issuer, "127.0.0.1")
                self.assertEqual(cfg.agents.disclosure, "public")
                self.assertTrue(cfg.policy.wildcards_accepted)
            finally:
                os.chdir(cwd_before)

    def test_loads_toml_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "agtp-server.toml"
            cfg_path.write_text(
                "[server]\n"
                'issuer = "agents.example.com"\n'
                'operator = "Example Inc."\n'
                'contact = "ops@example.com"\n'
                'amg_version = "1.0"\n'
                "\n"
                "[policy]\n"
                "wildcards_accepted = false\n"
                "anonymous_discovery = true\n"
                "scope_required_for_invocation = true\n"
                "\n"
                "[agents]\n"
                'disclosure = "limited"\n',
                encoding="utf-8",
            )
            cfg = load_config(cfg_path)
            self.assertFalse(cfg.is_default)
            self.assertEqual(cfg.server.issuer, "agents.example.com")
            self.assertEqual(cfg.server.operator, "Example Inc.")
            self.assertFalse(cfg.policy.wildcards_accepted)
            self.assertEqual(cfg.agents.disclosure, "limited")

    def test_explicit_path_must_exist(self):
        with self.assertRaises(FileNotFoundError):
            load_config(Path("/nonexistent/agtp-server.toml"))

    def test_invalid_disclosure_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "agtp-server.toml"
            cfg_path.write_text(
                "[server]\nissuer = \"x\"\n[agents]\ndisclosure = \"sneaky\"\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(cfg_path)


# ---------------------------------------------------------------------------
# Manifest generation + server-level DISCOVER dispatch tests.
# ---------------------------------------------------------------------------


def _stage_agents(agents_dir: Path) -> None:
    src = REPO_ROOT / "v1" / "server" / "agents"
    for name in ("lauren.agent.json", "orchestrator.agent.json"):
        (agents_dir / name).write_text(
            (src / name).read_text(encoding="utf-8"), encoding="utf-8"
        )


class _Server:
    def __init__(self, registry: AgentRegistry, config: ServerConfig):
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
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=handle_connection,
                args=(conn, self.registry, self.config),
                daemon=True,
            ).start()


def _send_discover(server: _Server, *, target_agent: str = ""):
    headers = {"Accept": "application/json", "Host": server.host}
    if target_agent:
        headers["Target-Agent"] = target_agent
    req = wire.AGTPRequest(method="DISCOVER", headers=headers, body_bytes=b"")
    sock = socket.create_connection((server.host, server.port), timeout=5.0)
    try:
        sock.sendall(req.serialize())
        return wire.parse_response(sock.makefile("rb"))
    finally:
        sock.close()


class ServerManifestTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = ServerConfig(
            server=ServerInfo(
                issuer="test.agtp.local",
                operator="unit-tests",
                contact="dev@local",
                amg_version="1.0",
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

    def test_manifest_has_expected_top_level_shape(self):
        m = generate(self.config, self.registry.agents)
        d = m.to_dict()
        for key in ("agtp_version", "issued_at", "server", "methods", "agents", "policy"):
            self.assertIn(key, d)

    def test_manifest_lists_all_twelve_embedded_methods(self):
        m = generate(self.config, self.registry.agents)
        names = {e["name"] for e in m.methods.embedded}
        expected = {
            "QUERY", "DISCOVER", "DESCRIBE", "SUMMARIZE", "PLAN", "EXECUTE",
            "DELEGATE", "ESCALATE", "CONFIRM", "SUSPEND", "PROPOSE", "NOTIFY",
        }
        self.assertEqual(names, expected)

    def test_manifest_includes_custom_method_when_loaded(self):
        from agtp.examples import custom_methods
        custom_methods.install()
        try:
            m = generate(self.config, self.registry.agents)
            self.assertEqual([e["name"] for e in m.methods.custom], ["RECONCILE"])
        finally:
            from agtp.methods import unregister
            unregister("RECONCILE")

    def test_manifest_lists_public_agents(self):
        m = generate(self.config, self.registry.agents)
        names = sorted(e["name"] for e in m.agents.list)
        self.assertEqual(names, ["Lauren", "Orchestrator"])
        for entry in m.agents.list:
            self.assertIn("skills_summary", entry)
            self.assertIn("methods_count", entry)

    def test_manifest_redacts_agents_when_private(self):
        cfg = ServerConfig(
            server=self.config.server,
            policy=self.config.policy,
            agents=AgentsConfig(disclosure="private"),
        )
        m = generate(cfg, self.registry.agents)
        self.assertEqual(m.agents.list, [])
        self.assertIsNotNone(m.agents.notice)

    def test_server_level_discover_returns_manifest(self):
        resp = _send_discover(self.server, target_agent="")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            CONTENT_TYPE_MANIFEST_JSON,
            wire.header(resp, "Content-Type"),
        )
        payload = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(payload["server"]["issuer"], "test.agtp.local")
        self.assertEqual(payload["methods"]["summary"]["embedded_count"], 12)
        self.assertEqual(payload["agents"]["disclosure"], "public")

    def test_per_agent_discover_still_works(self):
        # With Target-Agent set, DISCOVER goes to the agent path. That
        # path requires a body with target=<methods|agents|...>.
        headers = {
            "Accept": "application/json",
            "Host": self.server.host,
            "Target-Agent": LAUREN_ID,
            "Content-Type": "application/json",
        }
        body = json.dumps({"target": "methods"}).encode("utf-8")
        req = wire.AGTPRequest(method="DISCOVER", headers=headers, body_bytes=body)
        sock = socket.create_connection(
            (self.server.host, self.server.port), timeout=5.0
        )
        try:
            sock.sendall(req.serialize())
            resp = wire.parse_response(sock.makefile("rb"))
        finally:
            sock.close()
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(payload["target"], "methods")
        self.assertIn("embedded", payload)


if __name__ == "__main__":
    unittest.main()
