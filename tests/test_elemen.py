"""
Smoke tests for the elemen browser bridge.

The JS UI is not unit-tested here. What we exercise is the Python
surface that the UI calls into: discover_methods and invoke_method.
A passing run gives us confidence that auto-DISCOVER and the Try-it
form will work end-to-end against a real server.
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

# Make ``core`` / ``server`` / ``client`` etc. importable from
# the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# The bridge now lives at client/elemen/bridge.py and exposes an Api
# class. Tests instantiate Api() once per fixture and call its
# methods directly; that exercises the same code path the JS UI
# uses via window.pywebview.api.
from client.elemen.bridge import Api as ElemenApi  # noqa: E402
from server.main import AgentRegistry, handle_connection  # noqa: E402

# Backwards-compat shim: the bulk of this file calls
# ``elemen_client.fetch(...)`` etc. as module-level functions. The
# old module had standalone helpers; the new bridge wraps them in an
# Api class. Map the old names onto Api methods on a single shared
# instance so the test bodies stay unchanged.
_API = ElemenApi()


class _ElemenClientShim:
    """Adapter that makes the old standalone-function calls work
    against the new Api class."""

    def fetch(self, uri, *, fmt="json", insecure=False, insecure_skip_verify=False, **_):
        return _API.fetch(uri, fmt, "", insecure, insecure_skip_verify)

    def fetch_manifest(self, host, port, *, insecure=False, insecure_skip_verify=False, **_):
        return _API.fetch_manifest(host, port, insecure, insecure_skip_verify)

    def fetch_mcp_catalog(self, url, *, insecure_skip_verify=False, **_):
        return _API.fetch_mcp_catalog(url, insecure_skip_verify)

    def discover_methods(self, uri, *, insecure=False, insecure_skip_verify=False, **_):
        return _API.discover(uri, "", insecure, insecure_skip_verify)

    def invoke_method(
        self, uri, method, body=None, *,
        insecure=False, insecure_skip_verify=False, synthesis_id=None, **_,
    ):
        return _API.invoke(
            uri, method, body, "",
            insecure, insecure_skip_verify, synthesis_id or "",
        )


elemen_client = _ElemenClientShim()


LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"
ORCH_ID = "9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"


class _Server:
    """In-process AGTP server bound to a free port for tests."""

    def __init__(self, registry: AgentRegistry):
        self.registry = registry
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
                target=handle_connection, args=(conn, self.registry), daemon=True
            ).start()


class ElemenBridgeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents = Path(cls.tmp.name)
        src = REPO_ROOT / "server" / "agents"
        for f in ("lauren.agent.json", "orchestrator.agent.json"):
            (agents / f).write_text(
                (src / f).read_text(encoding="utf-8"), encoding="utf-8"
            )
        cls.server = _Server(AgentRegistry(agents))
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def _uri(self, agent_id: str) -> str:
        return f"agtp://{agent_id}@{self.server.host}:{self.server.port}"

    # ---- discover_methods ----

    def test_discover_returns_bucketed_shape(self):
        result = elemen_client.discover_methods(
            self._uri(LAUREN_ID), insecure=True, insecure_skip_verify=True
        )
        self.assertTrue(result["ok"], result)
        self.assertIn("embedded", result)
        self.assertIn("custom", result)
        self.assertEqual(result["custom"], [])
        self.assertEqual(
            {e["name"] for e in result["embedded"]},
            {"QUERY", "DISCOVER", "DESCRIBE", "SUMMARIZE",
             "PLAN", "EXECUTE", "CONFIRM", "NOTIFY"},
        )
        self.assertEqual(result["summary"]["embedded_count"], 8)

    def test_discover_against_orchestrator_lists_twelve(self):
        result = elemen_client.discover_methods(
            self._uri(ORCH_ID), insecure=True, insecure_skip_verify=True
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["summary"]["embedded_count"], 12)

    # ---- invoke_method ----

    def test_invoke_query_returns_200(self):
        result = elemen_client.invoke_method(
            self._uri(LAUREN_ID),
            "QUERY",
            {"intent": "what is the weather"},
            insecure=True,
            insecure_skip_verify=True,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status_code"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["method"], "QUERY")

    def test_invoke_unsupported_method_returns_405(self):
        result = elemen_client.invoke_method(
            self._uri(LAUREN_ID),
            "DELEGATE",
            {"task": "x", "sub_agent": ORCH_ID},
            insecure=True,
            insecure_skip_verify=True,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status_code"], 405)

    def test_invoke_propagates_404_when_agent_missing(self):
        bogus = "0" * 64
        result = elemen_client.invoke_method(
            f"agtp://{bogus}@{self.server.host}:{self.server.port}",
            "QUERY",
            {"intent": "hi"},
            insecure=True,
            insecure_skip_verify=True,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status_code"], 404)

    # ---- Server-URI fetch (Form 2) ----

    def test_form2_fetch_returns_manifest_kind(self):
        result = elemen_client.fetch(
            f"agtp://{self.server.host}:{self.server.port}",
            insecure=True,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kind"], "manifest")
        self.assertIsInstance(result["manifest"], dict)
        self.assertIn("server", result["manifest"])
        self.assertIn("embedded_methods", result["manifest"])
        self.assertIn("hosted_agents", result["manifest"])

    def test_fetch_manifest_helper_returns_same_shape(self):
        result = elemen_client.fetch_manifest(
            self.server.host, self.server.port, insecure=True,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kind"], "manifest")
        self.assertEqual(
            result["manifest"]["agent_disclosure"], "public",
        )

    # ---- v2 agent fetch shape ----

    def test_form1a_fetch_returns_agent_kind(self):
        result = elemen_client.fetch(
            self._uri(LAUREN_ID),
            insecure=True,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kind"], "agent")
        body = json.loads(result["body"])
        self.assertEqual(body["document_version"], "v2")
        self.assertIn("skills", body)
        self.assertIn("requires", body)

    # ---- Synthesis-Id passthrough ----

    # ---- Tab-visibility data contracts ----
    #
    # Tab visibility in the JS UI is data-driven: agent fetches
    # produce result.kind == "agent" (no Methods/APIs/Tools tabs) and
    # manifest fetches expose apis / hosted_protocols arrays that
    # control the APIs and protocol-specific tabs. These tests pin
    # the bridge contract; the JS toggles tabs from these fields.

    def test_agent_fetch_carries_no_apis_or_protocols(self):
        result = elemen_client.fetch(self._uri(LAUREN_ID), insecure=True)
        self.assertEqual(result["kind"], "agent")
        # An agent payload is a v2 Agent Document; it must not surface
        # apis or hosted_protocols (those are server concepts).
        body = json.loads(result["body"])
        self.assertNotIn("apis", body)
        self.assertNotIn("hosted_protocols", body)
        # The wire envelope itself only exposes manifest data on
        # manifest fetches; agent fetches lack the manifest field.
        self.assertNotIn("manifest", result)

    def test_manifest_fetch_exposes_apis_and_protocols_when_configured(self):
        # Re-create a server with the demo config so apis +
        # hosted_protocols flow through.
        from server.config import load as load_config
        from server.main import AgentRegistry
        cfg = load_config(REPO_ROOT / "server" / "agtp-server.toml")
        registry = AgentRegistry(REPO_ROOT / "server" / "agents")
        srv = _Server(registry)
        srv.config = cfg
        # The in-process _Server doesn't read config; route via the
        # manifest module directly to assert wire contract.
        from server.manifest import generate as gen
        manifest = gen(cfg, registry.agents)
        d = manifest.to_dict()
        self.assertIn("apis", d)
        self.assertIn("hosted_protocols", d)
        self.assertEqual(d["apis"][0]["path"], "/calendar")
        protos = [p["protocol"] for p in d["hosted_protocols"]]
        self.assertIn("mcp", protos)

    def test_manifest_omits_apis_when_unconfigured(self):
        # Fresh tempdir / default config has neither field populated;
        # the wire shape must omit them entirely so the JS hides the
        # APIs tab and the protocol tabs row.
        result = elemen_client.fetch(
            f"agtp://{self.server.host}:{self.server.port}",
            insecure=True,
        )
        self.assertEqual(result["kind"], "manifest")
        body = json.loads(result["body"])
        self.assertNotIn("apis", body)
        self.assertNotIn("hosted_protocols", body)

    # ---- MCP catalog fetcher ----

    def test_fetch_mcp_catalog_rejects_non_http_url(self):
        result = elemen_client.fetch_mcp_catalog("agtp://something")
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "parse")

    def test_fetch_mcp_catalog_handles_unreachable(self):
        # Bind a port, close it, then try to fetch from it. The
        # connection refusal becomes a structured error.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        result = elemen_client.fetch_mcp_catalog(
            f"http://127.0.0.1:{port}/tools.json"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "fetch")

    def test_fetch_mcp_catalog_parses_array(self):
        # Spin up a one-shot HTTP server that returns an MCP-style
        # tool array, then fetch through the elemen client and
        # confirm `tools` is populated.
        import http.server
        import threading
        body = json.dumps({"tools": [
            {"name": "calendar.create", "description": "create event"},
            {"name": "calendar.list",   "description": "list events"},
        ]}).encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a, **k): pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            result = elemen_client.fetch_mcp_catalog(
                f"http://127.0.0.1:{port}/tools.json"
            )
        finally:
            srv.shutdown()
            srv.server_close()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status_code"], 200)
        names = [t["name"] for t in result["tools"]]
        self.assertEqual(names, ["calendar.create", "calendar.list"])

    # ---- Compose drawer bridge (validate_compose / verb catalog) ----
    #
    # The drawer's catalog-driven validator and verb-catalog feed are
    # called from JS via window.pywebview.api. These tests pin the
    # Python-side contract: the same function the JS consumes.

    def test_validate_compose_accepts_known_verb(self):
        result = _API.validate_compose({"name": "RECONCILE"})
        self.assertTrue(result["valid"])
        self.assertEqual(result["errors"], {})
        self.assertEqual(result["completion"]["name"], "complete")

    def test_validate_compose_rejects_unknown_verb_with_suggestions(self):
        # FROBNICATE is not in the catalog; the validator surfaces a
        # close-match list under suggestions["name"].
        result = _API.validate_compose({"name": "RECONCIL"})
        self.assertFalse(result["valid"])
        self.assertIn("name", result["errors"])
        self.assertEqual(result["completion"]["name"], "error")
        # find_close_matches is Levenshtein-based; RECONCILE should
        # surface for RECONCIL.
        self.assertIn("RECONCILE", result["suggestions"].get("name", []))

    def test_validate_compose_flags_legacy_verb(self):
        result = _API.validate_compose({"name": "GET"})
        self.assertFalse(result["valid"])
        self.assertIn("legacy", result["errors"]["name"].lower())

    def test_validate_compose_path_grammar_rejects_verb_in_path(self):
        # /get/orders embeds GET — the path grammar refuses it.
        result = _API.validate_compose({
            "name": "RECONCILE",
            "path": "/get/orders",
        })
        self.assertFalse(result["valid"])
        self.assertIn("path", result["errors"])
        self.assertEqual(result["completion"]["path"], "error")

    def test_validate_compose_path_grammar_accepts_clean_path(self):
        result = _API.validate_compose({
            "name": "RECONCILE",
            "path": "/orders/{order_id}",
        })
        self.assertTrue(result["valid"])
        self.assertEqual(result["completion"]["path"], "complete")

    def test_get_verb_catalog_returns_full_catalog(self):
        catalog = _API.get_verb_catalog()
        self.assertIsInstance(catalog, list)
        self.assertGreater(len(catalog), 100)  # ~423 in core/methods.json
        names = {entry["name"] for entry in catalog}
        # Sample a few that are guaranteed to be in the catalog.
        for known in ("RECONCILE", "DISCOVER", "QUERY"):
            self.assertIn(known, names)
        # Each entry has the expected shape for the autocomplete UI.
        sample = catalog[0]
        for key in ("name", "categories", "description"):
            self.assertIn(key, sample)

    def test_invoke_via_synthesis_id_passes_header(self):
        # Establish a synthesis on the orchestrator.
        propose = elemen_client.invoke_method(
            self._uri(ORCH_ID),
            "PROPOSE",
            {
                "name": "QUERY",
                "parameters": {"intent": "string"},
                "outcome": "results",
            },
            insecure=True,
        )
        self.assertEqual(propose["status_code"], 263)
        synth_id = json.loads(propose["body"])["synthesis_id"]

        # Re-invoke under any method name with the synthesis_id; the
        # server rewrites to the synthesis target (QUERY).
        result = elemen_client.invoke_method(
            self._uri(ORCH_ID),
            "RESERVE",  # arbitrary; the server replaces it
            {"intent": "follow up"},
            insecure=True,
            synthesis_id=synth_id,
        )
        self.assertEqual(result["status_code"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["method"], "QUERY")


if __name__ == "__main__":
    unittest.main()
