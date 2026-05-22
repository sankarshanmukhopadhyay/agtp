"""
Tests for the AGTP 12-method set.

For every method:
  * a valid invocation returns 200 with the expected response shape;
  * missing required parameters return 422 (skipped when a method has none);
  * invocation against an agent that does not declare the method
    returns 405.

Plus a small batch of cross-cutting tests:
  * unknown methods return 459 (Method Violation);
  * the registry exposes complete semantic metadata for every entry;
  * DESCRIBE content-negotiates JSON / YAML / HTML.

Run:
    python -m unittest test_methods
    python test_methods.py
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
from typing import Optional

# Add repo root so `import agtp` works when running directly out of the tree.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wire
from core.identity import (
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_YAML,
)
from server.methods import REGISTRY
from server.main import AgentRegistry, handle_connection


LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"
ORCH_ID = "9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"
MINIMAL_ID = "d786a710500073dffa858aa83a6696afb2ffd2ca3a497f0d5538ad953f3b11ec"


# Sample valid parameter sets, one per method. Used for both the 200
# happy-path test and (after deletion) the 422 missing-param test.
VALID_PARAMS = {
    "QUERY":     {"intent": "what is the capital of France"},
    "DISCOVER":  {"target": "methods"},
    "DESCRIBE":  {},
    "SUMMARIZE": {"source": "Long input text repeated several times to summarize."},
    "PLAN":      {"goal": "ship the v0.2 release"},
    "EXECUTE":   {"plan_id": "plan-abc-123"},
    "DELEGATE":  {"task": "do the thing", "sub_agent": LAUREN_ID},
    "ESCALATE":  {"decision_point": "approve high-cost action"},
    "CONFIRM":   {"attestation_target": "esc-fake-001"},
    "SUSPEND":   {},
    # PROPOSE under the v2 negotiation policy: a name that maps onto an
    # existing embedded method gets accepted with a synthesis.
    "PROPOSE":   {
        "name": "QUERY",
        "parameters": {"intent": "string"},
        "outcome": "results",
    },
    "NOTIFY":    {"event": "demo.tick"},
}


# ---------------------------------------------------------------------------
# Test server harness.
# ---------------------------------------------------------------------------


class _TestServer:
    """Minimal in-process AGTP server bound to a free port on localhost."""

    def __init__(self, registry: AgentRegistry):
        self.registry = registry
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.host, self.port = self.sock.getsockname()
        self.sock.listen(32)
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            t = threading.Thread(
                target=handle_connection,
                args=(conn, self.registry),
                daemon=True,
            )
            t.start()


def _send(
    server: _TestServer,
    target_agent: str,
    method_name: str,
    body: Optional[dict] = None,
    accept: str = "application/json",
) -> wire.AGTPResponse:
    body_bytes = b"" if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Agent-ID": target_agent,
        "Accept": accept,
        "Host": server.host,
    }
    if body_bytes:
        headers["Content-Type"] = "application/json"

    req = wire.AGTPRequest(method=method_name, headers=headers, body_bytes=body_bytes)

    sock = socket.create_connection((server.host, server.port), timeout=5.0)
    try:
        sock.sendall(req.serialize())
        return wire.parse_response(sock.makefile("rb"))
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _decode_json(response: wire.AGTPResponse) -> dict:
    return json.loads(response.body_bytes.decode("utf-8"))


# ---------------------------------------------------------------------------
# Test fixtures.
# ---------------------------------------------------------------------------


def _write_test_agents(agents_dir: Path) -> None:
    """Create three test agents: full-12, lauren-8, minimal-0 capabilities."""
    repo_root = Path(__file__).resolve().parent.parent
    src_dir = repo_root / "server" / "agents"
    for name in ("lauren.agent.json", "orchestrator.agent.json"):
        (agents_dir / name).write_text(
            (src_dir / name).read_text(encoding="utf-8"), encoding="utf-8"
        )

    minimal = {
        "agtp_version": "1.0",
        "agent_id": MINIMAL_ID,
        "name": "Minimal",
        "principal": "Test Suite",
        "principal_id": "principal-test-001",
        "description": "Empty-capabilities agent used to exercise the 405 path.",
        "status": "active",
        "capabilities": [],
        "scopes_accepted": [],
        "issued_at": "2026-05-07T00:00:00Z",
        "issuer": "agtp.io",
    }
    (agents_dir / "minimal.agent.json").write_text(
        json.dumps(minimal, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Test cases.
# ---------------------------------------------------------------------------


class MethodSetTests(unittest.TestCase):
    """One TestCase covering all twelve methods plus cross-cutting checks."""

    server: _TestServer
    tmp_dir: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp_dir = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp_dir.name)
        _write_test_agents(agents_dir)

        registry = AgentRegistry(agents_dir)
        assert LAUREN_ID in registry.agents, "Lauren did not load"
        assert ORCH_ID in registry.agents, "Orchestrator did not load"
        assert MINIMAL_ID in registry.agents, "Minimal did not load"

        cls.server = _TestServer(registry)
        cls.server.start()
        time.sleep(0.05)  # give the accept loop a beat to enter

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.tmp_dir.cleanup()

    # ---- registry / semantic metadata ----

    def test_registry_has_all_embedded_methods(self) -> None:
        # 12 original protocol primitives + Phase 6 INSPECT + Phase 8
        # ACTIVATE / DEACTIVATE / REVOKE / REINSTATE / DEPRECATE.
        expected = {
            "QUERY", "DISCOVER", "DESCRIBE", "SUMMARIZE", "PLAN", "EXECUTE",
            "DELEGATE", "ESCALATE", "CONFIRM", "SUSPEND", "PROPOSE", "NOTIFY",
            "INSPECT",
            "ACTIVATE", "DEACTIVATE", "REVOKE", "REINSTATE", "DEPRECATE",
        }
        from core.methods import EMBEDDED_VERBS
        embedded = {
            name for name in REGISTRY
            if name in EMBEDDED_VERBS
        }
        self.assertEqual(embedded, expected)

    def test_registry_entries_are_semantic_complete(self) -> None:
        cognitive = {
            "QUERY", "DISCOVER", "DESCRIBE", "SUMMARIZE", "PLAN", "EXECUTE",
            "INSPECT",  # Phase 6: audit read surface
        }
        mechanics = {"DELEGATE", "ESCALATE", "CONFIRM", "SUSPEND", "PROPOSE", "NOTIFY"}
        for name, spec in REGISTRY.items():
            self.assertEqual(spec.name, name, f"{name}: name field mismatch")
            self.assertTrue(name.isupper() and " " not in name, name)
            if name in cognitive:
                self.assertEqual(spec.category, "cognitive", name)
            else:
                self.assertEqual(spec.category, "mechanics", name)
            self.assertEqual(spec.semantic_class, "action-intent", name)
            self.assertIsInstance(spec.idempotent, bool, name)
            self.assertIsInstance(spec.state_modifying, bool, name)
            self.assertIsInstance(spec.required_params, list, name)
            self.assertIsInstance(spec.optional_params, list, name)
            self.assertTrue(spec.error_codes, f"{name}: must declare error codes")
            self.assertTrue(spec.description, f"{name}: must have a description")
            self.assertIsNotNone(spec.handler, f"{name}: must have a handler")

    # ---- happy paths ----

    def test_all_methods_succeed_against_orchestrator(self) -> None:
        # §7: PROPOSE accept returns 263 Proposal Approved (not 200).
        # Every other method still uses 200 for happy path.
        for name, params in VALID_PARAMS.items():
            with self.subTest(method=name):
                resp = _send(self.server, ORCH_ID, name, body=params)
                expected = 263 if name == "PROPOSE" else 200
                self.assertEqual(
                    resp.status_code,
                    expected,
                    f"{name} returned {resp.status_code}: "
                    f"{resp.body_bytes!r}",
                )

    def test_response_shape_carries_method_field(self) -> None:
        for name, params in VALID_PARAMS.items():
            if name == "DESCRIBE":
                # DESCRIBE returns the AgentDocument, which uses
                # agtp_version / agent_id rather than a 'method' field.
                continue
            if name == "PROPOSE":
                # PROPOSE default is 422 negotiation-refused; payload
                # tested separately.
                continue
            with self.subTest(method=name):
                resp = _send(self.server, ORCH_ID, name, body=params)
                payload = _decode_json(resp)
                self.assertEqual(payload.get("method"), name)
                self.assertEqual(payload.get("agent_id"), ORCH_ID)

    # ---- 422 missing required params ----

    def test_missing_required_params_return_422(self) -> None:
        for name, params in VALID_PARAMS.items():
            spec = REGISTRY[name]
            if not spec.required_params:
                continue
            if name == "PROPOSE":
                # §7: PROPOSE returns 400 bad-request with
                # issue="missing-required-field" instead of 422
                # missing-required-params (the rest of the registry
                # still uses the legacy require_params path).
                continue
            with self.subTest(method=name):
                # Send a body that lacks every required key.
                empty_body: dict = {}
                resp = _send(self.server, ORCH_ID, name, body=empty_body)
                self.assertEqual(
                    resp.status_code,
                    422,
                    f"{name} expected 422, got {resp.status_code}",
                )
                payload = _decode_json(resp)
                self.assertIn("error", payload)
                self.assertEqual(
                    payload["error"]["code"], "missing-required-params"
                )
                self.assertEqual(
                    set(payload["error"]["missing"]),
                    set(spec.required_params),
                )

    def test_propose_missing_name_returns_400(self) -> None:
        # §7: PROPOSE body without a ``name`` field is a malformed
        # request, not a negotiation refusal.
        resp = _send(self.server, ORCH_ID, "PROPOSE", body={})
        self.assertEqual(resp.status_code, 400)
        payload = _decode_json(resp)
        self.assertEqual(payload["error"]["code"], "bad-request")
        self.assertEqual(payload["error"]["issue"], "missing-required-field")

    # ---- soft-deny / capability check for an undeclared agent ----
    #
    # With v2 soft-deny in front of dispatch:
    #   * cognitive non-exempt methods (QUERY, SUMMARIZE, PLAN, EXECUTE)
    #     hit the soft-deny gate and return 403 method-not-permitted-for-agent.
    #   * DISCOVER, DESCRIBE, and the mechanics (DELEGATE, ESCALATE,
    #     CONFIRM, SUSPEND, PROPOSE, NOTIFY) are exempt from soft-deny;
    #     their handler-local capability check returns 405
    #     method-not-in-requires.

    def test_methods_against_minimal_agent_return_refusal(self) -> None:
        from server.main import SOFT_DENY_EXEMPT_METHODS

        for name, params in VALID_PARAMS.items():
            with self.subTest(method=name):
                resp = _send(self.server, MINIMAL_ID, name, body=params)
                payload = _decode_json(resp)
                if name in SOFT_DENY_EXEMPT_METHODS:
                    self.assertEqual(resp.status_code, 405)
                    self.assertEqual(
                        payload["error"]["code"], "method-not-in-requires"
                    )
                else:
                    self.assertEqual(resp.status_code, 403)
                    self.assertEqual(
                        payload["error"]["code"],
                        "method-not-permitted-for-agent",
                    )

    # ---- 459 unknown method ----

    def test_unknown_method_returns_459(self) -> None:
        # FAKEMETHOD is not in the curated AGTP verb list. The
        # dispatcher refuses with 459 Method Violation
        # before reaching the registry. Earlier revisions returned
        # 501; the new spec separates "name not in catalog" (459)
        # from "valid name but no handler" (405).
        resp = _send(self.server, ORCH_ID, "FAKEMETHOD", body={})
        self.assertEqual(resp.status_code, 459)
        payload = _decode_json(resp)
        self.assertEqual(payload["error"]["code"], "method-violation")
        self.assertEqual(payload["error"]["method"], "FAKEMETHOD")

    # ---- DESCRIBE content negotiation ----

    def test_describe_returns_json_by_default(self) -> None:
        resp = _send(self.server, LAUREN_ID, "DESCRIBE", accept=CONTENT_TYPE_JSON)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            CONTENT_TYPE_JSON, wire.header(resp, "Content-Type")
        )
        # X-AGTP-Document-Type pins the document kind so a header-first
        # renderer (elemen) can pick the agent view without parsing.
        # See core/identity.py for the catalog.
        self.assertEqual(
            wire.header(resp, "X-AGTP-Document-Type"),
            "agtp.agent.document",
        )
        payload = _decode_json(resp)
        self.assertEqual(payload["agent_id"], LAUREN_ID)

    def test_describe_returns_yaml_when_requested(self) -> None:
        resp = _send(self.server, LAUREN_ID, "DESCRIBE", accept=CONTENT_TYPE_YAML)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("yaml", wire.header(resp, "Content-Type"))
        text = resp.body_bytes.decode("utf-8")
        self.assertIn(f"agent_id: {LAUREN_ID}", text)

    def test_describe_returns_html_when_requested(self) -> None:
        resp = _send(self.server, LAUREN_ID, "DESCRIBE", accept=CONTENT_TYPE_HTML)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", wire.header(resp, "Content-Type"))
        text = resp.body_bytes.decode("utf-8")
        self.assertIn("<!DOCTYPE html>", text)

    # ---- PROPOSE: structural refusal + accept path on existing verb ----

    def test_propose_refuses_out_of_scope_when_no_close_match(self) -> None:
        # §7: PROPOSE refusal returns 463 Proposal Rejected with
        # structured reason. FROBNICATE has no recipe and no
        # close-match counter; the runtime declines and the handler
        # surfaces proposal-rejected with
        # reason=composition-impossible (no near-match available).
        resp = _send(
            self.server,
            ORCH_ID,
            "PROPOSE",
            body={"name": "FROBNICATE"},
        )
        self.assertEqual(resp.status_code, 463)
        payload = _decode_json(resp)
        self.assertEqual(payload["error"]["code"], "proposal-rejected")
        self.assertIn(
            payload["error"]["reason"],
            ("out-of-scope", "composition-impossible"),
        )

    def test_propose_accepts_proposal_naming_existing_method(self) -> None:
        from server.negotiation import SYNTHESES
        SYNTHESES.clear()
        resp = _send(
            self.server,
            ORCH_ID,
            "PROPOSE",
            body={
                "name": "QUERY",
                "parameters": {"intent": "string"},
                "outcome": "results",
                "description": "ask the agent about itself",
            },
        )
        # §7: accept returns 263 Proposal Approved; synthesis_id is
        # at the top level, multi-step details under ``synthesis``.
        self.assertEqual(resp.status_code, 263)
        payload = _decode_json(resp)
        self.assertTrue(payload["synthesis_id"].startswith("syn-"))
        self.assertEqual(payload["synthesis"]["target_method"], "QUERY")

    # ---- SUSPEND nonce shape ----

    def test_suspend_returns_resumption_nonce(self) -> None:
        resp = _send(self.server, ORCH_ID, "SUSPEND", body={"reason": "test"})
        self.assertEqual(resp.status_code, 200)
        payload = _decode_json(resp)
        self.assertIn("resumption_nonce", payload)
        self.assertGreaterEqual(len(payload["resumption_nonce"]), 16)

    # ---- DISCOVER target=methods returns the bucketed shape ----

    def test_discover_methods_returns_bucketed_shape(self) -> None:
        resp = _send(
            self.server, LAUREN_ID, "DISCOVER", body={"target": "methods"}
        )
        self.assertEqual(resp.status_code, 200)
        payload = _decode_json(resp)

        self.assertIn("embedded", payload)
        self.assertIn("custom", payload)
        self.assertIn("summary", payload)
        self.assertEqual(payload["target"], "methods")

        names = {item["name"] for item in payload["embedded"]}
        self.assertEqual(
            names,
            {"QUERY", "DISCOVER", "DESCRIBE", "SUMMARIZE", "PLAN",
             "EXECUTE", "CONFIRM", "NOTIFY"},
        )
        self.assertEqual(payload["custom"], [])
        self.assertEqual(payload["summary"]["embedded_count"], 8)
        self.assertEqual(payload["summary"]["custom_count"], 0)
        self.assertEqual(payload["summary"]["total"], 8)

    def test_discover_methods_buckets_are_sorted_alphabetically(self) -> None:
        resp = _send(
            self.server, ORCH_ID, "DISCOVER", body={"target": "methods"}
        )
        names = [item["name"] for item in _decode_json(resp)["embedded"]]
        self.assertEqual(names, sorted(names))

    def test_embedded_entries_have_no_namespace(self) -> None:
        resp = _send(
            self.server, ORCH_ID, "DISCOVER", body={"target": "methods"}
        )
        for entry in _decode_json(resp)["embedded"]:
            self.assertNotIn(
                "namespace", entry,
                f"{entry['name']} embedded entry must omit namespace",
            )

    # ---- single-agent default routing ----

    def test_agent_id_required_when_multiple_hosted(self) -> None:
        # §10: the canonical header is Agent-ID; the legacy
        # ``Target-Agent`` still works via back-compat fallback.
        # Send DESCRIBE without either header against the
        # multi-agent server and expect ``missing-agent-id``.
        sock = socket.create_connection((self.server.host, self.server.port))
        try:
            req = wire.AGTPRequest(
                method="DESCRIBE",
                headers={"Accept": CONTENT_TYPE_JSON, "Host": self.server.host},
            )
            sock.sendall(req.serialize())
            resp = wire.parse_response(sock.makefile("rb"))
        finally:
            sock.close()
        self.assertEqual(resp.status_code, 400)
        payload = _decode_json(resp)
        self.assertEqual(payload["error"]["code"], "missing-agent-id")


class EmbeddedSemanticBlockTests(unittest.TestCase):
    """
    Every embedded method must carry a complete semantic block.
    The composition runtime, the manifest, and Elemen's overview all
    consume these values; missing fields would silently degrade
    recipe matching and catalog readiness.
    """

    EMBEDDED = (
        "QUERY", "DISCOVER", "DESCRIBE", "SUMMARIZE", "PLAN", "EXECUTE",
        "DELEGATE", "ESCALATE", "CONFIRM", "SUSPEND", "PROPOSE", "NOTIFY",
        "INSPECT",
        "ACTIVATE", "DEACTIVATE", "REVOKE", "REINSTATE", "DEPRECATE",
    )

    def test_every_embedded_method_has_semantic_block(self):
        from server.methods import REGISTRY
        for name in self.EMBEDDED:
            with self.subTest(method=name):
                spec = REGISTRY[name]
                self.assertIsNotNone(
                    spec.semantic,
                    f"{name} is missing its semantic block",
                )

    def test_semantic_block_fields_are_well_formed(self):
        from core.endpoint import ALL_CAPABILITIES, ALL_IMPACTS
        from server.methods import REGISTRY
        for name in self.EMBEDDED:
            with self.subTest(method=name):
                sb = REGISTRY[name].semantic
                self.assertTrue(sb.intent, f"{name}.intent is empty")
                self.assertGreaterEqual(
                    len(sb.intent), 20,
                    f"{name}.intent shorter than 20 chars",
                )
                # actor is free-form per agtp-api §6; just require
                # a non-empty string.
                self.assertIsInstance(sb.actor, str)
                self.assertTrue(sb.actor.strip(), f"{name}.actor is empty")
                self.assertTrue(sb.outcome, f"{name}.outcome is empty")
                self.assertGreaterEqual(len(sb.outcome), 20)
                self.assertIn(sb.capability, ALL_CAPABILITIES)
                self.assertIsInstance(sb.confidence, float)
                self.assertGreaterEqual(sb.confidence, 0.0)
                self.assertLessEqual(sb.confidence, 1.0)
                self.assertIn(sb.impact, ALL_IMPACTS)
                self.assertIsInstance(sb.is_idempotent, bool)

    def test_semantic_is_idempotent_agrees_with_protocol_idempotent(self):
        # Coherence rule: semantic.is_idempotent must match the
        # protocol-level idempotent flag.
        from server.methods import REGISTRY
        for name in self.EMBEDDED:
            with self.subTest(method=name):
                spec = REGISTRY[name]
                self.assertEqual(
                    spec.semantic.is_idempotent,
                    spec.idempotent,
                    f"{name}: semantic.is_idempotent ({spec.semantic.is_idempotent}) "
                    f"disagrees with idempotent ({spec.idempotent})",
                )

    def test_state_modifying_methods_are_not_marked_idempotent(self):
        # Coherence rule: is_idempotent=true is incompatible
        # with state_modifying=true.
        from server.methods import REGISTRY
        for name in self.EMBEDDED:
            with self.subTest(method=name):
                spec = REGISTRY[name]
                if spec.state_modifying:
                    self.assertFalse(
                        spec.semantic.is_idempotent,
                        f"{name}: state_modifying methods must not be is_idempotent",
                    )

    def test_irreversible_methods_have_high_confidence(self):
        # Coherence rule (warning, but we want it satisfied
        # for embedded methods): impact=irreversible methods should
        # declare confidence >= 0.85.
        from core.endpoint import IRREVERSIBLE_CONFIDENCE_FLOOR
        from server.methods import REGISTRY
        for name in self.EMBEDDED:
            with self.subTest(method=name):
                sb = REGISTRY[name].semantic
                if sb.impact == "irreversible":
                    self.assertGreaterEqual(
                        sb.confidence,
                        IRREVERSIBLE_CONFIDENCE_FLOOR,
                    )

    def test_spec_to_endpoint_spec_carries_semantic(self):
        # The synthesis runtime sees embedded methods through
        # spec_to_endpoint_spec; the semantic block must survive the
        # lift.
        from server.methods import REGISTRY, spec_to_endpoint_spec
        for name in self.EMBEDDED:
            with self.subTest(method=name):
                ep = spec_to_endpoint_spec(REGISTRY[name])
                self.assertIsNotNone(
                    ep.semantic,
                    f"{name}: EndpointSpec.semantic is None after lift",
                )
                self.assertEqual(
                    ep.semantic.intent,
                    REGISTRY[name].semantic.intent,
                )

    def test_spec_to_dict_serializes_semantic(self):
        # The DISCOVER /methods endpoint and the manifest both serialize
        # via spec_to_dict; downstream catalogs and Elemen's overview
        # rely on the semantic block being present in the wire form.
        from server.methods import REGISTRY, spec_to_dict
        for name in self.EMBEDDED:
            with self.subTest(method=name):
                payload = spec_to_dict(REGISTRY[name])
                self.assertIn("semantic", payload)
                self.assertEqual(payload["semantic"]["actor"],
                                 REGISTRY[name].semantic.actor)


class CustomMethodTests(unittest.TestCase):
    """
    Exercises register_custom and the example RECONCILE method. Lives in
    its own TestCase so RECONCILE is installed in setUpClass and removed
    in tearDownClass, leaving the global REGISTRY untouched for other
    tests that assert the embedded count.
    """

    server: _TestServer
    tmp_dir: tempfile.TemporaryDirectory
    custom_agent_id: str

    @classmethod
    def setUpClass(cls) -> None:
        from server.examples import custom_methods
        custom_methods.install()

        cls.tmp_dir = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp_dir.name)
        _write_test_agents(agents_dir)

        # A fourth agent that declares the custom verb so DISCOVER can
        # surface it in the `custom` bucket.
        cls.custom_agent_id = (
            "ca5703a51c5703a51c5703a51c5703a51c5703a51c5703a51c5703a51c570001"
        )
        custom_doc = {
            "agtp_version": "1.0",
            "agent_id": cls.custom_agent_id,
            "name": "AcmeFinanceAgent",
            "principal": "Acme Finance",
            "principal_id": "principal-acme-001",
            "description": "Test agent that declares the RECONCILE custom method.",
            "status": "active",
            "capabilities": ["DESCRIBE", "DISCOVER", "RECONCILE"],
            "scopes_accepted": ["transact:reconcile"],
            "issued_at": "2026-05-07T00:00:00Z",
            "issuer": "agtp.io",
        }
        (agents_dir / "acme.agent.json").write_text(
            json.dumps(custom_doc, indent=2), encoding="utf-8"
        )

        registry = AgentRegistry(agents_dir)
        cls.server = _TestServer(registry)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.tmp_dir.cleanup()
        from server.methods import unregister
        unregister("RECONCILE")

    def test_reconcile_appears_in_registry_with_namespace(self) -> None:
        # Custom methods (non-embedded) carry a namespace.
        spec = REGISTRY["RECONCILE"]
        self.assertEqual(spec.namespace, "acme-finance")
        self.assertEqual(spec.category, "transact")

    def test_discover_buckets_custom_method_separately(self) -> None:
        resp = _send(
            self.server,
            self.custom_agent_id,
            "DISCOVER",
            body={"target": "methods"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = _decode_json(resp)

        embedded_names = {e["name"] for e in payload["embedded"]}
        custom_names = {e["name"] for e in payload["custom"]}

        self.assertEqual(embedded_names, {"DESCRIBE", "DISCOVER"})
        self.assertEqual(custom_names, {"RECONCILE"})
        self.assertEqual(payload["summary"]["embedded_count"], 2)
        self.assertEqual(payload["summary"]["custom_count"], 1)
        self.assertEqual(payload["summary"]["total"], 3)

    def test_custom_entry_carries_namespace(self) -> None:
        resp = _send(
            self.server,
            self.custom_agent_id,
            "DISCOVER",
            body={"target": "methods"},
        )
        custom = _decode_json(resp)["custom"]
        self.assertEqual(len(custom), 1)
        entry = custom[0]
        self.assertEqual(entry["name"], "RECONCILE")
        self.assertEqual(entry["namespace"], "acme-finance")

    def test_invoking_reconcile_succeeds_with_required_params(self) -> None:
        resp = _send(
            self.server,
            self.custom_agent_id,
            "RECONCILE",
            body={"account_id": "acct-001", "period": "2026-04"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = _decode_json(resp)
        self.assertEqual(payload["method"], "RECONCILE")
        self.assertEqual(payload["account_id"], "acct-001")
        self.assertEqual(payload["status"], "stub-reconciled")

    def test_register_custom_rejects_missing_namespace(self) -> None:
        # Use a verb that IS in the catalog and ISN'T already
        # registered (FORECAST) so the Phase-6 catalog-membership
        # check passes AND the structural ``namespace required``
        # validator under test actually fires (rather than the
        # already-registered RuntimeError firing first).
        from server.methods import register_custom
        with self.assertRaises(ValueError):
            register_custom(
                lambda *a, **k: None,  # noqa: E731
                name="FORECAST",
                namespace="",
                category="transact",
                semantic_class="action-intent",
                idempotent=False,
                state_modifying=True,
                required_params=["x"],
                error_codes=[400],
                description="bad",
            )

    def test_embedded_methods_register_without_namespace(self) -> None:
        # The embedded-vs-custom distinction is determined by
        # membership in EMBEDDED_VERBS. All 12 embedded primitives
        # register at module load with ``namespace=None``; verify
        # the registry state reflects that.
        from core.methods import EMBEDDED_VERBS
        for name in EMBEDDED_VERBS:
            with self.subTest(method=name):
                spec = REGISTRY[name]
                self.assertIsNone(
                    spec.namespace,
                    f"embedded method {name} should not carry a namespace",
                )


if __name__ == "__main__":
    unittest.main()
