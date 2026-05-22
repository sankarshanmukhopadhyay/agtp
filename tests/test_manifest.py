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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wire
from server.config import (
    AgentsConfig,
    ServerConfig,
    ServerInfo,
    ServerPolicy,
    default_config,
    load as load_config,
)
from core.identity import (
    CONTENT_TYPE_MANIFEST_JSON,
    AgentDocument,
    from_dict,
)
from core.ids import AgentIDError, parse_uri, format_uri
from server.manifest import APIEndpoint, HostedProtocol, generate
from server.main import AgentRegistry, handle_connection


REPO_ROOT = Path(__file__).resolve().parent.parent
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
        # §11: format_uri never emits port. Canonical Form 2 URIs are
        # port-less; the default 4480 is implicit (mirroring how HTTPS
        # omits :443 from canonical URIs).
        self.assertEqual(format_uri(host="example.com"), "agtp://example.com")
        # Even when a non-default port is requested, format_uri drops
        # it from the canonical output.
        self.assertEqual(
            format_uri(host="example.com", port=8443),
            "agtp://example.com",
        )
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
                self.assertEqual(cfg.server.server_id, "127.0.0.1")
                self.assertEqual(cfg.agents.disclosure, "public")
                self.assertTrue(cfg.policy.wildcards_accepted)
            finally:
                os.chdir(cwd_before)

    def test_loads_toml_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "agtp-server.toml"
            cfg_path.write_text(
                "[server]\n"
                'server_id = "agents.example.com"\n'
                'operator = "Example Inc."\n'
                'contact = "ops@example.com"\n'
                "\n"
                "[policies]\n"
                "wildcards_accepted = false\n"
                "anonymous_discovery = true\n"
                "scope_required_for_invocation = true\n"
                "synthesis_enabled = true\n"
                "max_synthesis_depth = 10\n"
                "\n"
                "[agents]\n"
                'disclosure = "limited"\n',
                encoding="utf-8",
            )
            cfg = load_config(cfg_path)
            self.assertFalse(cfg.is_default)
            self.assertEqual(cfg.server.server_id, "agents.example.com")
            self.assertEqual(cfg.server.operator, "Example Inc.")
            self.assertFalse(cfg.policy.wildcards_accepted)
            self.assertTrue(cfg.policy.synthesis_enabled)
            self.assertEqual(cfg.policy.max_synthesis_depth, 10)
            self.assertEqual(cfg.agents.disclosure, "limited")

    def test_explicit_path_must_exist(self):
        with self.assertRaises(FileNotFoundError):
            load_config(Path("/nonexistent/agtp-server.toml"))

    def test_invalid_disclosure_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "agtp-server.toml"
            cfg_path.write_text(
                "[server]\nserver_id = \"x\"\n[agents]\ndisclosure = \"sneaky\"\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(cfg_path)


# ---------------------------------------------------------------------------
# Manifest generation + server-level DISCOVER dispatch tests.
# ---------------------------------------------------------------------------


def _stage_agents(agents_dir: Path) -> None:
    src = REPO_ROOT / "server" / "agents"
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
        headers["Agent-ID"] = target_agent
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
                server_id="test.agtp.local",
                operator="unit-tests",
                contact="dev@local",
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
        for key in (
            "agtp_version", "agtp_api_version", "document_version",
            "server", "embedded_methods",
            "agent_disclosure", "hosted_agents", "policies",
        ):
            self.assertIn(key, d)
        # Server identity lives under the server block per agtp-api §7.
        self.assertIn("server_id", d["server"])
        self.assertIn("issued", d["server"])
        self.assertIn("updated", d["server"])

    def test_manifest_lists_all_embedded_methods(self):
        m = generate(self.config, self.registry.agents)
        names = {e["name"] for e in m.embedded_methods}
        # 12 original protocol primitives + Phase 6 INSPECT.
        expected = {
            "QUERY", "DISCOVER", "DESCRIBE", "SUMMARIZE", "PLAN", "EXECUTE",
            "DELEGATE", "ESCALATE", "CONFIRM", "SUSPEND", "PROPOSE", "NOTIFY",
            "INSPECT",
        }
        self.assertEqual(names, expected)

    def test_manifest_includes_custom_method_when_loaded(self):
        from server.examples import custom_methods
        custom_methods.install()
        try:
            m = generate(self.config, self.registry.agents)
            self.assertEqual([e["name"] for e in m.custom_methods], ["RECONCILE"])
        finally:
            from server.methods import unregister
            unregister("RECONCILE")

    def test_manifest_lists_public_agents(self):
        m = generate(self.config, self.registry.agents)
        names = sorted(e["name"] for e in m.hosted_agents)
        self.assertEqual(names, ["Lauren", "Orchestrator"])
        for entry in m.hosted_agents:
            self.assertIn("skills_summary", entry)
            self.assertIn("methods_count", entry)

    def test_manifest_redacts_agents_when_private(self):
        cfg = ServerConfig(
            server=self.config.server,
            policy=self.config.policy,
            agents=AgentsConfig(disclosure="private"),
        )
        m = generate(cfg, self.registry.agents)
        self.assertEqual(m.hosted_agents, [])
        self.assertIsNotNone(m.agent_disclosure_notice)

    def test_server_level_discover_returns_manifest(self):
        resp = _send_discover(self.server, target_agent="")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            CONTENT_TYPE_MANIFEST_JSON,
            wire.header(resp, "Content-Type"),
        )
        # X-AGTP-Document-Type pins the document kind for renderers
        # that dispatch on the header (e.g., elemen) before parsing
        # the body. See core/identity.py for the catalog.
        self.assertEqual(
            wire.header(resp, "X-AGTP-Document-Type"),
            "agtp.server.manifest",
        )
        payload = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(payload["server"]["server_id"], "test.agtp.local")
        # 12 original primitives + Phase 6 INSPECT.
        self.assertEqual(len(payload["embedded_methods"]), 13)
        self.assertEqual(payload["agent_disclosure"], "public")
        # Policy block has the five operational toggles.
        self.assertIn("policies", payload)
        for key in (
            "wildcards_accepted", "anonymous_discovery",
            "scope_required_for_invocation",
            "synthesis_enabled", "max_synthesis_depth",
        ):
            self.assertIn(key, payload["policies"])

    def test_per_agent_discover_still_works(self):
        # With Agent-ID set, DISCOVER goes to the agent path. That
        # path requires a body with target=<methods|agents|...>.
        headers = {
            "Accept": "application/json",
            "Host": self.server.host,
            "Agent-ID": LAUREN_ID,
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


class APIsAndProtocolsTests(unittest.TestCase):
    """Coverage for the apis and hosted_protocols manifest fields."""

    def setUp(self):
        self.config = ServerConfig(
            server=ServerInfo(
                server_id="t.local", operator="x", contact=""
            ),
            policy=ServerPolicy(),
            agents=AgentsConfig(disclosure="public"),
        )

    def test_empty_apis_and_protocols_omitted_from_json(self):
        m = generate(self.config, {})
        d = json.loads(m.to_json())
        self.assertNotIn("apis", d)
        self.assertNotIn("hosted_protocols", d)

    def test_populated_apis_round_trip(self):
        apis = [
            APIEndpoint(
                path="/calendar",
                methods=["QUERY", "PROPOSE"],
                description="Calendar resource.",
            ),
            APIEndpoint(path="/notes", methods=["QUERY"]),
        ]
        m = generate(self.config, {}, apis=apis)
        d = json.loads(m.to_json())
        self.assertIn("apis", d)
        self.assertEqual(d["apis"][0]["path"], "/calendar")
        self.assertEqual(d["apis"][0]["methods"], ["QUERY", "PROPOSE"])
        self.assertIn("description", d["apis"][0])
        # Description-less entries omit the field.
        self.assertNotIn("description", d["apis"][1])

    def test_populated_hosted_protocols_round_trip(self):
        protos = [
            HostedProtocol(
                protocol="mcp",
                version="0.1",
                endpoint="https://mcp.example.com",
                catalog="https://mcp.example.com/.well-known/mcp/tools.json",
            ),
            HostedProtocol(
                protocol="openapi",
                version="3.1",
                endpoint="https://api.example.com",
            ),
        ]
        m = generate(self.config, {}, hosted_protocols=protos)
        d = json.loads(m.to_json())
        self.assertEqual(len(d["hosted_protocols"]), 2)
        self.assertEqual(d["hosted_protocols"][0]["protocol"], "mcp")
        self.assertIn("catalog", d["hosted_protocols"][0])
        self.assertNotIn("catalog", d["hosted_protocols"][1])

    def test_api_endpoint_validates_path_and_methods(self):
        with self.assertRaises(ValueError):
            APIEndpoint(path="no-leading-slash", methods=["QUERY"])
        with self.assertRaises(ValueError):
            APIEndpoint(path="/x", methods=[])

    def test_hosted_protocol_validates_required_fields(self):
        with self.assertRaises(ValueError):
            HostedProtocol(protocol="", version="1", endpoint="x")
        with self.assertRaises(ValueError):
            HostedProtocol(protocol="mcp", version="1", endpoint="")

    def test_demo_config_loads_apis_and_protocols(self):
        # Round-trip the bundled demo config to confirm config.py wiring.
        from server.config import load as load_config
        demo_path = REPO_ROOT / "server" / "agtp-server.toml"
        cfg = load_config(demo_path)
        self.assertGreaterEqual(len(cfg.apis), 1)
        self.assertGreaterEqual(len(cfg.hosted_protocols), 1)
        self.assertEqual(cfg.apis[0].path, "/calendar")
        protocols = [p.protocol for p in cfg.hosted_protocols]
        # OpenAPI is the demo's bridged-protocol example. MCP is not
        # in the demo because the demo server does not host an MCP
        # backend; agtp-mcp/gateway.py is the canonical MCP host
        # and advertises its own surface via the identity doc.
        self.assertIn("openapi", protocols)


class MethodNotPermittedVocabTests(unittest.TestCase):
    """
    The soft-deny refusal now rides 403 Forbidden but carries the
    ``method-not-permitted-for-agent`` error code so existing
    framing — "the principal has not authorized this method" —
    survives the spec migration.
    """

    def test_method_not_permitted_for_agent_returns_403(self):
        from core.status import (
            FORBIDDEN,
            method_not_permitted_for_agent,
            method_outside_need,
        )
        self.assertEqual(FORBIDDEN, (403, "Forbidden"))
        # Backward-compat alias resolves to the same callable.
        self.assertIs(method_outside_need, method_not_permitted_for_agent)

        resp = method_not_permitted_for_agent("EXECUTE", "abc123")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.status_text, "Forbidden")
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(
            body["error"]["code"], "method-not-permitted-for-agent"
        )
        # Body text frames the refusal as a permission decision.
        self.assertIn("permissions", body["error"]["explanation"].lower())
        self.assertIn(
            "principal has not authorized",
            body["error"]["explanation"].lower(),
        )


class EndpointsInManifestTests(unittest.TestCase):
    """Phase-2 coverage: the manifest exposes the endpoint registry's
    contents under ``endpoints``. Embedded methods continue to ride
    under top-level ``embedded_methods``."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)
        cls.config = ServerConfig(
            server=ServerInfo(
                server_id="test.agtp.local",
                operator="unit-tests",
                contact="dev@local",
            ),
            policy=ServerPolicy(),
            agents=AgentsConfig(disclosure="public"),
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _populated_registry(self):
        from core.endpoint import (
            EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
        )
        from server.endpoint_registry import EndpointRegistry
        reg = EndpointRegistry()
        spec = EndpointSpec(
            name="BOOK", path="/room",
            description="Books a room.",
            namespace="reservations",
            semantic=SemanticBlock(
                intent="Reserve a room for the named guest.",
                actor="agent",
                outcome="A confirmed reservation_id is returned.",
                capability="transaction",
                confidence=0.85,
                impact="irreversible",
                is_idempotent=False,
            ),
            required_params=[
                ParamSpec(name="guest_id", type="string",
                          description="guest id"),
            ],
            output=[
                ParamSpec(name="reservation_id", type="string",
                          description="server-assigned"),
            ],
            errors=["room_unavailable"],
            handler=HandlerBinding(
                type="registered_function",
                reference="samples.handlers.book_room",
            ),
            required_scopes=["bookings:write"],
        )
        reg.register(spec)
        return reg

    def test_manifest_includes_endpoints_array_when_registry_populated(self):
        reg = self._populated_registry()
        m = generate(
            self.config, self.registry.agents,
            endpoint_registry=reg,
        )
        d = m.to_dict()
        self.assertIn("endpoints", d)
        self.assertEqual(len(d["endpoints"]), 1)
        entry = d["endpoints"][0]
        # Canonical AGTP-API manifest shape: input_schema /
        # output_schema as JSON Schema docs, handler as
        # {type: ...} object.
        for key in (
            "method", "path", "description",
            "input_schema", "output_schema",
            "errors", "semantic", "handler", "required_scopes",
        ):
            self.assertIn(key, entry)
        # Historical alias keys + the parameter-list shape +
        # ``handler_type`` flat string are NOT leaked into the
        # manifest.
        for leaked in (
            "name", "required_params", "optional_params",
            "category", "error_codes",
            "input", "output", "handler_type",
        ):
            self.assertNotIn(leaked, entry)
        # handler is an object, not a flat string.
        self.assertEqual(entry["handler"], {"type": "registered_function"})

    def test_manifest_omits_endpoints_when_registry_empty(self):
        # Empty registry → no ``endpoints`` key on the wire (terse).
        m = generate(self.config, self.registry.agents)
        d = m.to_dict()
        self.assertNotIn("endpoints", d)

    def test_embedded_methods_still_listed_alongside_endpoints(self):
        # Phase 2 doesn't take embedded primitives off the wire when
        # endpoints are registered. Top-level ``embedded_methods``
        # keeps its full set (13 after Phase 6 added INSPECT).
        reg = self._populated_registry()
        m = generate(
            self.config, self.registry.agents,
            endpoint_registry=reg,
        )
        d = m.to_dict()
        self.assertEqual(len(d["embedded_methods"]), 13)

    def test_endpoint_section_surfaces_handler_block_with_type(self):
        # ``handler: {type: ...}`` exposed; ``handler.reference``
        # (Python dotted path) stays private to the server.
        reg = self._populated_registry()
        m = generate(
            self.config, self.registry.agents,
            endpoint_registry=reg,
        )
        entry = m.to_dict()["endpoints"][0]
        self.assertEqual(entry["handler"], {"type": "registered_function"})
        self.assertNotIn("handler_type", entry)
        # Reference must NOT leak.
        for key, value in entry["handler"].items():
            self.assertNotIn("samples", str(value))
        self.assertEqual(entry["required_scopes"], ["bookings:write"])

    def test_composition_endpoint_surfaces_handler_object_with_composition_type(self):
        # Phase-3 composition-bound endpoints surface
        # ``handler_type: "composition"``; the recipe name (the
        # binding's reference) stays private to the server.
        from core.endpoint import (
            EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
        )
        from server.endpoint_registry import EndpointRegistry
        reg = EndpointRegistry()
        spec = EndpointSpec(
            name="AUDIT", path="/reviews/{subject_id}",
            description="Audit a subject.",
            namespace="reviews",
            semantic=SemanticBlock(
                intent="Audit the named subject and return findings.",
                actor="agent",
                outcome="A summary is returned.",
                capability="analysis",
                confidence=0.80,
                impact="informational",
                is_idempotent=True,
            ),
            required_params=[
                ParamSpec(name="subject", type="string",
                          description="entity to audit"),
            ],
            output=[
                ParamSpec(name="summary", type="string",
                          description="audit summary"),
            ],
            errors=["composition_failed"],
            handler=HandlerBinding(
                type="composition",
                reference="audit-via-query-and-summarize",
            ),
        )
        reg.register(spec)
        m = generate(
            self.config, self.registry.agents,
            endpoint_registry=reg,
        )
        entry = m.to_dict()["endpoints"][0]
        self.assertEqual(entry["handler"], {"type": "composition"})
        # The recipe name itself does NOT ride on the wire.
        def _walk(value):
            if isinstance(value, str):
                self.assertNotIn(
                    "audit-via-query-and-summarize", value,
                    f"recipe reference leaked through value {value!r}",
                )
            elif isinstance(value, dict):
                for v in value.values():
                    _walk(v)
            elif isinstance(value, list):
                for v in value:
                    _walk(v)
        _walk(entry)


if __name__ == "__main__":
    unittest.main()
