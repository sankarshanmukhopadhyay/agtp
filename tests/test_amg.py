"""
Coverage for the AMG (Agent Method Grammar) validator and friends.

Layout follows the pass order: each pass has a positive case that
validates and at least one negative case that fails at the right
pass with the right error code. The suite also pins the
register_custom / handle_propose integration points and the CLI.
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

from server.amg import (
    AMG_VERSION,
    AMGMethodSpec,
    DEFAULT_SUBSTITUTIONS,
    EMBEDDED_METHODS,
    EquivalenceClass,
    HTTP_METHODS,
    InvalidMethodError,
    PARAM_TYPES,
    ParamSpec,
    SOURCE_AGTP,
    SOURCE_AMG,
    STOPLIST,
    SubstitutionHint,
    SynthesisContract,
    conditions_for,
    find_substitutes,
    is_reserved,
    validate,
    validate_synthesis,
)
from server.amg.grammar import SEMANTIC_PROTOCOL_MECHANIC, SEMANTIC_QUERY_INTENT
from server.methods import REGISTRY, register_custom, unregister


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"


def _good_spec(**overrides) -> AMGMethodSpec:
    """A baseline spec that validates; overrides target a single pass."""
    base = dict(
        name="RECONCILE",
        semantic_class="action-intent",
        category="transact",
        description="Reconcile transactions for a given account and period.",
        idempotent=False,
        state_modifying=True,
        required_params=[
            ParamSpec(name="account_id", type="string",
                      description="account whose transactions to reconcile"),
            ParamSpec(name="period", type="string",
                      description="time window like '2026-Q1'"),
        ],
        optional_params=[
            ParamSpec(name="tolerance", type="number",
                      description="rounding tolerance for matching"),
        ],
        error_codes=[400, 422, 451],
        source=SOURCE_AMG,
        namespace="acme-finance",
    )
    base.update(overrides)
    return AMGMethodSpec(**base)


# ===========================================================================
# Pass 1: lexical
# ===========================================================================


class Pass1LexicalTests(unittest.TestCase):

    def test_three_char_uppercase_passes(self):
        spec = _good_spec(name="ABC")
        self.assertTrue(validate(spec).valid)

    def test_thirty_two_char_passes(self):
        spec = _good_spec(name="A" * 32)
        self.assertTrue(validate(spec).valid)

    def test_two_char_too_short(self):
        spec = _good_spec(name="AB")
        r = validate(spec)
        self.assertFalse(r.valid)
        self.assertEqual(r.error.pass_name, "lexical")
        self.assertEqual(r.error.code, "malformed-name")

    def test_thirty_three_char_too_long(self):
        spec = _good_spec(name="A" * 33)
        r = validate(spec)
        self.assertFalse(r.valid)
        self.assertEqual(r.error.pass_name, "lexical")

    def test_lowercase_rejected(self):
        spec = _good_spec(name="reconcile")
        r = validate(spec)
        self.assertFalse(r.valid)
        self.assertEqual(r.error.pass_name, "lexical")

    def test_digits_rejected(self):
        spec = _good_spec(name="RECON1")
        r = validate(spec)
        self.assertFalse(r.valid)
        self.assertEqual(r.error.pass_name, "lexical")

    def test_underscore_rejected(self):
        spec = _good_spec(name="A_B_C")
        r = validate(spec)
        self.assertFalse(r.valid)
        self.assertEqual(r.error.pass_name, "lexical")

    def test_hyphen_rejected(self):
        spec = _good_spec(name="A-B-C")
        r = validate(spec)
        self.assertFalse(r.valid)
        self.assertEqual(r.error.pass_name, "lexical")


# ===========================================================================
# Pass 2: reserved
# ===========================================================================


class Pass2ReservedTests(unittest.TestCase):

    def test_get_rejected_as_http(self):
        spec = _good_spec(name="GET")
        r = validate(spec)
        self.assertFalse(r.valid)
        self.assertEqual(r.error.pass_name, "reserved")
        self.assertEqual(r.error.code, "reserved-http-method")

    def test_post_rejected_as_http(self):
        spec = _good_spec(name="POST")
        self.assertEqual(
            validate(spec).error.code, "reserved-http-method"
        )

    def test_user_method_cannot_register_embedded_name(self):
        spec = _good_spec(name="QUERY")  # source defaults to amg/1.0
        r = validate(spec)
        self.assertFalse(r.valid)
        self.assertEqual(r.error.code, "reserved-embedded-method")

    def test_embedded_method_with_agtp_source_passes_reserved_pass(self):
        # Source = agtp/1.0 grandfathers in the embedded names.
        spec = _good_spec(
            name="QUERY",
            source=SOURCE_AGTP,
            namespace=None,
            semantic_class="action-intent",
        )
        # Pass 2 should not flag this; later passes might still
        # complain (e.g. namespace must be absent, which it is).
        r = validate(spec)
        self.assertTrue(r.valid, r.error)

    def test_is_reserved_helper(self):
        self.assertIn("HTTP method", is_reserved("GET") or "")
        self.assertIn("embedded", is_reserved("QUERY") or "")
        self.assertIsNone(is_reserved("RECONCILE"))


# ===========================================================================
# Pass 3: semantic class
# ===========================================================================


class Pass3SemanticClassTests(unittest.TestCase):

    def test_action_intent_passes(self):
        spec = _good_spec()
        self.assertTrue(validate(spec).valid)

    def test_query_intent_passes(self):
        spec = _good_spec(semantic_class=SEMANTIC_QUERY_INTENT)
        self.assertTrue(validate(spec).valid)

    def test_unknown_class_rejected(self):
        spec = _good_spec(semantic_class="weird-vibes")
        r = validate(spec)
        self.assertEqual(r.error.code, "unknown-semantic-class")

    def test_protocol_mechanic_rejected_for_user_methods(self):
        spec = _good_spec(semantic_class=SEMANTIC_PROTOCOL_MECHANIC)
        r = validate(spec)
        self.assertEqual(r.error.code, "protocol-mechanic-not-allowed")

    def test_protocol_mechanic_allowed_for_embedded(self):
        spec = _good_spec(
            name="DELEGATE",
            semantic_class=SEMANTIC_PROTOCOL_MECHANIC,
            source=SOURCE_AGTP,
            namespace=None,
        )
        r = validate(spec)
        self.assertTrue(r.valid, r.error)


# ===========================================================================
# Pass 4: stoplist
# ===========================================================================


class Pass4StoplistTests(unittest.TestCase):

    def test_status_rejected(self):
        spec = _good_spec(name="STATUS")
        r = validate(spec)
        self.assertEqual(r.error.code, "non-action-intent")

    def test_data_rejected(self):
        r = validate(_good_spec(name="DATA"))
        self.assertEqual(r.error.code, "non-action-intent")

    def test_active_rejected(self):
        r = validate(_good_spec(name="ACTIVE"))
        self.assertEqual(r.error.code, "non-action-intent")

    def test_info_rejected_with_suggestion(self):
        r = validate(_good_spec(name="INFO"))
        self.assertEqual(r.error.code, "non-action-intent")
        self.assertIsNotNone(r.error.suggestion)
        self.assertIn("DESCRIBE", r.error.suggestion)


# ===========================================================================
# Pass 5: required fields
# ===========================================================================


class Pass5RequiredFieldsTests(unittest.TestCase):

    def test_amg_method_without_namespace_fails(self):
        spec = _good_spec(namespace=None)
        r = validate(spec)
        self.assertEqual(r.error.code, "missing-namespace")

    def test_error_codes_must_include_422(self):
        spec = _good_spec(error_codes=[400, 451])
        r = validate(spec)
        self.assertEqual(r.error.code, "error-codes-missing-422")

    def test_empty_description_fails_required_fields(self):
        # Description "" is truthy-empty; Pass 5 sees it as missing
        # before Pass 6 description-quality runs.
        spec = _good_spec(description="")
        r = validate(spec)
        self.assertEqual(r.error.pass_name, "required-fields")
        self.assertEqual(r.error.code, "missing-required-field")

    def test_unknown_source_rejected(self):
        spec = _good_spec(source="future/1.0")
        r = validate(spec)
        self.assertEqual(r.error.code, "unknown-source")

    def test_namespace_on_embedded_rejected(self):
        # Embedded methods (source=agtp/1.0) cannot declare a namespace.
        # Use a non-embedded uppercase name so we don't trip Pass 4
        # (stoplist) before getting here.
        spec = _good_spec(
            name="SETTLE",
            source=SOURCE_AGTP,
            namespace="agtp.io",
        )
        r = validate(spec)
        self.assertEqual(r.error.code, "namespace-on-embedded")


# ===========================================================================
# Pass 6: description quality
# ===========================================================================


class Pass6DescriptionTests(unittest.TestCase):

    def test_short_description_fails(self):
        spec = _good_spec(description="too short")
        r = validate(spec)
        self.assertEqual(r.error.pass_name, "description")
        self.assertEqual(r.error.code, "description-too-short")

    def test_todo_pattern_rejected(self):
        spec = _good_spec(description="TODO: describe what this method does")
        r = validate(spec)
        self.assertEqual(r.error.code, "stub-description")

    def test_stub_pattern_rejected(self):
        spec = _good_spec(description="Stub for now, will fill in later")
        r = validate(spec)
        self.assertEqual(r.error.code, "stub-description")

    def test_placeholder_pattern_rejected(self):
        spec = _good_spec(
            description="placeholder description, fill in once the spec lands",
        )
        r = validate(spec)
        self.assertEqual(r.error.code, "stub-description")


# ===========================================================================
# Pass 7: parameter well-formedness
# ===========================================================================


class Pass7ParametersTests(unittest.TestCase):

    def test_camel_case_param_rejected(self):
        spec = _good_spec(
            required_params=[
                ParamSpec(name="accountId", type="string",
                          description="ledger account id"),
            ],
        )
        r = validate(spec)
        self.assertEqual(r.error.code, "malformed-param-name")

    def test_unknown_param_type_rejected(self):
        spec = _good_spec(
            required_params=[
                ParamSpec(name="account_id", type="binary",
                          description="raw bytes"),
            ],
        )
        r = validate(spec)
        self.assertEqual(r.error.code, "unknown-param-type")

    def test_object_param_without_schema_rejected(self):
        spec = _good_spec(
            required_params=[
                ParamSpec(name="filter", type="object",
                          description="match clause"),
            ],
        )
        r = validate(spec)
        self.assertEqual(r.error.code, "missing-param-schema")

    def test_object_param_with_schema_passes(self):
        spec = _good_spec(
            required_params=[
                ParamSpec(name="account_id", type="string",
                          description="ledger account"),
                ParamSpec(name="period", type="string",
                          description="time window"),
                ParamSpec(name="filter", type="object",
                          description="optional match clause",
                          schema={"type": "object", "properties": {}}),
            ],
        )
        self.assertTrue(validate(spec).valid)

    def test_duplicate_param_name_rejected(self):
        spec = _good_spec(
            required_params=[
                ParamSpec(name="period", type="string", description="window"),
                ParamSpec(name="period", type="string",
                          description="duplicate"),
            ],
        )
        r = validate(spec)
        self.assertEqual(r.error.code, "duplicate-param-name")

    def test_required_optional_param_collision_rejected(self):
        spec = _good_spec(
            required_params=[
                ParamSpec(name="account_id", type="string", description="x"),
                ParamSpec(name="period", type="string", description="y"),
            ],
            optional_params=[
                ParamSpec(name="period", type="string",
                          description="conflicts"),
            ],
        )
        r = validate(spec)
        self.assertEqual(r.error.code, "duplicate-param-name")


# ===========================================================================
# Pass 9: substitution
# ===========================================================================


class Pass9SubstitutionTests(unittest.TestCase):

    def test_known_target_passes(self):
        spec = _good_spec(
            substitutes_for=[SubstitutionHint(target_method="EXECUTE")],
        )
        self.assertTrue(validate(spec).valid)

    def test_unknown_target_rejected(self):
        spec = _good_spec(
            substitutes_for=[
                SubstitutionHint(target_method="ZBLARGON"),
            ],
        )
        r = validate(spec)
        self.assertEqual(r.error.code, "unknown-substitution-target")

    def test_self_reference_rejected(self):
        spec = _good_spec(
            substitutes_for=[SubstitutionHint(target_method="RECONCILE")],
        )
        r = validate(spec)
        self.assertEqual(r.error.code, "self-substitution")

    def test_duplicate_substitution_target_rejected(self):
        spec = _good_spec(
            substitutes_for=[
                SubstitutionHint(target_method="EXECUTE"),
                SubstitutionHint(target_method="EXECUTE"),
            ],
        )
        r = validate(spec)
        self.assertEqual(r.error.code, "duplicate-substitution-target")


# ===========================================================================
# Embedded methods regression: all 12 must validate.
# ===========================================================================


class EmbeddedMethodRegressionTests(unittest.TestCase):

    def test_all_embedded_methods_pass_validation(self):
        # Build AMGMethodSpec mirrors of the registry entries and
        # validate. Mechanics carry semantic_class="protocol-mechanic"
        # under source=agtp/1.0; cognitive verbs use action-intent.
        from server.amg.grammar import SEMANTIC_ACTION_INTENT
        cognitive = {"QUERY", "DISCOVER", "DESCRIBE", "SUMMARIZE", "PLAN", "EXECUTE"}
        for name in EMBEDDED_METHODS:
            with self.subTest(method=name):
                live = REGISTRY[name]
                cls = (
                    SEMANTIC_ACTION_INTENT if name in cognitive
                    else SEMANTIC_PROTOCOL_MECHANIC
                )
                spec = AMGMethodSpec(
                    name=name,
                    semantic_class=cls,
                    category=live.category,
                    description=live.description,
                    idempotent=live.idempotent,
                    state_modifying=live.state_modifying,
                    required_params=[
                        ParamSpec.from_bare_name(p) for p in live.required_params
                    ],
                    optional_params=[
                        ParamSpec.from_bare_name(p) for p in live.optional_params
                    ],
                    error_codes=(
                        list(live.error_codes)
                        if 422 in live.error_codes
                        else list(live.error_codes) + [422]
                    ),
                    source=SOURCE_AGTP,
                    namespace=None,
                )
                r = validate(spec)
                self.assertTrue(
                    r.valid,
                    f"{name} should validate but failed: {r.error}",
                )


# ===========================================================================
# Substitution catalog
# ===========================================================================


class SubstitutionCatalogTests(unittest.TestCase):

    def test_default_substitutions_loaded(self):
        names = [c.name for c in DEFAULT_SUBSTITUTIONS]
        self.assertIn("reservation", names)
        self.assertIn("retrieval", names)

    def test_find_substitutes_returns_registry_intersection(self):
        registry = {"BOOK", "RESERVE", "QUERY"}
        subs = find_substitutes("BOOK", registry)
        self.assertIn("RESERVE", subs)
        self.assertNotIn("QUERY", subs)
        # SCHEDULE is in the equivalence class but not in the registry.
        self.assertNotIn("SCHEDULE", subs)

    def test_find_substitutes_excludes_self(self):
        registry = {"BOOK", "RESERVE"}
        self.assertNotIn("BOOK", find_substitutes("BOOK", registry))

    def test_find_substitutes_empty_for_unknown(self):
        self.assertEqual(find_substitutes("ZBLARGON", {"FOO"}), [])

    def test_conditions_for_known_member(self):
        conds = conditions_for("BOOK")
        self.assertTrue(any("calendar" in c.lower() for c in conds))


# ===========================================================================
# Synthesis contract
# ===========================================================================


class SynthesisContractTests(unittest.TestCase):

    def _spec(self, name="RESERVE"):
        return AMGMethodSpec(
            name=name,
            semantic_class="action-intent",
            category="transact",
            description="Reserve a calendar slot for the given window.",
            idempotent=False,
            state_modifying=True,
            required_params=[
                ParamSpec(name="resource", type="string",
                          description="thing to reserve"),
                ParamSpec(name="start_time", type="string",
                          description="ISO 8601 start"),
            ],
            optional_params=[],
            error_codes=[400, 422, 451],
            source=SOURCE_AMG,
            namespace="acme-bookings",
        )

    def test_synthesis_validates_when_target_known(self):
        contract = SynthesisContract(
            synthesis_id="syn-test",
            proposed_method=self._spec(),
            target_methods=["EXECUTE"],
            parameter_mapping={
                "resource": "plan_id",
                "start_time": "plan_id",
            },
        )
        r = validate_synthesis(contract, server_methods={"EXECUTE"})
        self.assertTrue(r.valid, r.error)

    def test_synthesis_rejects_unknown_target(self):
        contract = SynthesisContract(
            synthesis_id="syn-test",
            proposed_method=self._spec(),
            target_methods=["ZBLARGON"],
            parameter_mapping={"resource": "x", "start_time": "y"},
        )
        r = validate_synthesis(contract, server_methods=set())
        self.assertEqual(r.error.code, "synthesis-unknown-target")

    def test_synthesis_rejects_cycle(self):
        contract = SynthesisContract(
            synthesis_id="syn-test",
            proposed_method=self._spec(name="RESERVE"),
            target_methods=["RESERVE"],
            parameter_mapping={"resource": "r", "start_time": "s"},
        )
        r = validate_synthesis(contract, server_methods={"RESERVE", "EXECUTE"})
        self.assertEqual(r.error.code, "synthesis-cycle")

    def test_synthesis_rejects_incomplete_mapping(self):
        contract = SynthesisContract(
            synthesis_id="syn-test",
            proposed_method=self._spec(),
            target_methods=["EXECUTE"],
            parameter_mapping={"resource": "plan_id"},  # start_time missing
        )
        r = validate_synthesis(contract, server_methods={"EXECUTE"})
        self.assertEqual(r.error.code, "synthesis-mapping-incomplete")

    def test_synthesis_rejects_empty_targets(self):
        contract = SynthesisContract(
            synthesis_id="syn-test",
            proposed_method=self._spec(),
            target_methods=[],
            parameter_mapping={"resource": "x", "start_time": "y"},
        )
        r = validate_synthesis(contract, server_methods=set())
        self.assertEqual(r.error.code, "synthesis-empty-targets")


# ===========================================================================
# register_custom integration
# ===========================================================================


class RegisterCustomAMGGateTests(unittest.TestCase):

    def setUp(self):
        unregister("CUSTOMOK")
        unregister("STATUS")

    def tearDown(self):
        unregister("CUSTOMOK")
        unregister("STATUS")

    def _stub(self, *a, **k):
        return None

    def test_register_custom_passes_amg(self):
        register_custom(
            self._stub,
            name="CUSTOMOK",
            namespace="acme-test",
            category="transact",
            semantic_class="action-intent",
            idempotent=False,
            state_modifying=True,
            required_params=["account_id"],
            error_codes=[400, 422],
            description=(
                "Custom verb that exercises the AMG gate during registration."
            ),
        )
        self.assertIn("CUSTOMOK", REGISTRY)

    def test_register_custom_rejects_stoplist_name(self):
        with self.assertRaises(InvalidMethodError) as cm:
            register_custom(
                self._stub,
                name="STATUS",
                namespace="acme-test",
                category="transact",
                semantic_class="action-intent",
                idempotent=False,
                state_modifying=True,
                required_params=["x"],
                error_codes=[400, 422],
                description="Should be refused at Pass 4 (stoplist).",
            )
        self.assertEqual(cm.exception.result.error.code, "non-action-intent")
        self.assertNotIn("STATUS", REGISTRY)


# ===========================================================================
# handle_propose AMG gate
# ===========================================================================


class _ProposeServer:
    """Minimal in-process server reused for the PROPOSE gate test."""

    def __init__(self):
        from server.main import AgentRegistry, handle_connection
        from server.config import default_config
        self._handle = handle_connection
        self._config = default_config()
        # Stage Lauren + Orchestrator from the demo.
        self._tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(self._tmp.name)
        for n in ("lauren.agent.json", "orchestrator.agent.json"):
            (agents_dir / n).write_text(
                (REPO_ROOT / "server" / "agents" / n).read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
        self._registry = AgentRegistry(agents_dir)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.host, self.port = self.sock.getsockname()
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self): self._thread.start()

    def stop(self):
        self._stop.set()
        try: self.sock.close()
        except OSError: pass
        self._tmp.cleanup()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout: continue
            except OSError: return
            threading.Thread(
                target=self._handle,
                args=(conn, self._registry, self._config),
                daemon=True,
            ).start()


class HandleProposeAMGTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = _ProposeServer()
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _send(self, body: dict):
        from core import wire
        ORCH_ID = "9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"
        body_bytes = json.dumps(body).encode("utf-8")
        headers = {
            "Target-Agent": ORCH_ID,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Host": self.server.host,
        }
        req = wire.AGTPRequest(method="PROPOSE", headers=headers, body_bytes=body_bytes)
        sock = socket.create_connection(
            (self.server.host, self.server.port), timeout=5.0,
        )
        try:
            sock.sendall(req.serialize())
            return wire.parse_response(sock.makefile("rb"))
        finally:
            sock.close()

    def test_propose_with_http_method_name_returns_460(self):
        # GET is reserved; AMG should refuse before the negotiation
        # policy ever evaluates the proposal.
        resp = self._send({
            "name": "GET",
            "parameters": {"resource": "string"},
            "outcome": "object",
        })
        self.assertEqual(resp.status_code, 460)
        payload = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(payload["error"]["reason"], "ambiguous")
        self.assertEqual(payload["error"]["amg_code"], "reserved-http-method")

    def test_propose_with_stoplist_name_returns_460(self):
        resp = self._send({
            "name": "STATUS",
            "parameters": {"x": "string"},
            "outcome": "object",
        })
        self.assertEqual(resp.status_code, 460)
        payload = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(payload["error"]["amg_code"], "non-action-intent")

    def test_propose_with_existing_embedded_name_still_accepted(self):
        # The reserved-embedded-method case is benign for proposals;
        # naming an existing verb is the accept-with-synthesis path.
        resp = self._send({
            "name": "QUERY",
            "parameters": {"intent": "string"},
            "outcome": "results",
        })
        self.assertEqual(resp.status_code, 200)


# ===========================================================================
# CLI integration
# ===========================================================================


class CLIIntegrationTests(unittest.TestCase):

    def _run_cli(self, *args):
        return subprocess.run(
            [PYTHON, "-m", "server.amg.cli", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_cli_exits_zero_on_valid_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.method.json"
            path.write_text(json.dumps({
                "name": "RECONCILE",
                "semantic_class": "action-intent",
                "category": "transact",
                "description": "Reconcile transactions for an account window.",
                "idempotent": False,
                "state_modifying": True,
                "required_params": [
                    {"name": "account_id", "type": "string",
                     "description": "ledger account id"},
                    {"name": "period", "type": "string",
                     "description": "time window"},
                ],
                "optional_params": [],
                "error_codes": [400, 422, 451],
                "source": "amg/1.0",
                "namespace": "acme-finance",
            }), encoding="utf-8")
            out = self._run_cli(str(path))
            self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
            self.assertIn("VALID", out.stdout)

    def test_cli_exits_one_on_invalid_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.method.json"
            path.write_text(json.dumps({
                "name": "GET",
                "semantic_class": "action-intent",
                "category": "transact",
                "description": "GET resource by id (would conflict with HTTP).",
                "idempotent": True,
                "state_modifying": False,
                "required_params": [],
                "optional_params": [],
                "error_codes": [400, 422],
                "source": "amg/1.0",
                "namespace": "acme-finance",
            }), encoding="utf-8")
            out = self._run_cli(str(path))
            self.assertEqual(out.returncode, 1)
            self.assertIn("INVALID", out.stdout)
            self.assertIn("reserved-http-method", out.stdout)

    def test_cli_check_substitution(self):
        out = self._run_cli("--check-substitution", "BOOK")
        self.assertEqual(out.returncode, 0)
        # BOOK belongs to the 'reservation' equivalence class; with no
        # --known-methods the registry only has the embedded set, so
        # the output should announce no candidates rather than error.
        self.assertTrue(
            "RESERVE" in out.stdout or "No substitution" in out.stdout,
            out.stdout,
        )


# ===========================================================================
# Public API sanity
# ===========================================================================


class PublicAPISanityTests(unittest.TestCase):

    def test_amg_version_string(self):
        self.assertIsInstance(AMG_VERSION, str)
        self.assertTrue(AMG_VERSION)

    def test_http_methods_includes_canonical_set(self):
        for n in ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertIn(n, HTTP_METHODS)

    def test_embedded_methods_count_is_twelve(self):
        self.assertEqual(len(EMBEDDED_METHODS), 12)

    def test_param_types_include_six_known(self):
        for t in ("string", "integer", "number", "boolean", "object", "array"):
            self.assertIn(t, PARAM_TYPES)


if __name__ == "__main__":
    unittest.main()
