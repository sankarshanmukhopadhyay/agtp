"""
Tests for the synthesis runtime (``server.synthesis``).

Coverage in three layers:

  1. Pure unit tests — Recipe loader, RecipePattern matching, plan
     types, RecipeBasedPolicy, PassthroughPolicy. No server.
  2. Runtime unit tests — SynthesisRuntime.attempt / instantiate /
     execute / expire against a stub step dispatcher. No live socket.
  3. End-to-end — PROPOSE on a live server flows through the runtime,
     a Synthesis-Id invocation executes the plan via the actual
     dispatcher, SUSPEND clears the synthesis. These reuse the
     ``_Server`` fixture pattern from test_handshake.py.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wire
from core.identity import AgentDocument
from core.endpoint import EndpointSpec, ParamSpec
from server.config import (
    AgentsConfig,
    ServerConfig,
    ServerInfo,
    ServerPolicy,
    SynthesisConfig,
)
from server.main import AgentRegistry, handle_connection
from server.synthesis import (
    CompositionStep,
    ParameterSource,
    PassthroughPolicy,
    Recipe,
    RecipeBasedPolicy,
    RecipeFileError,
    RecipePattern,
    SynthesisError,
    SynthesisPlan,
    SynthesisRuntime,
    load_recipes,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
LAUREN_ID = "d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230"
ORCH_ID   = "9fe1dfc552a64c8bbec8dd2fe8cbe1a275f1a3405f7c5c20acca6453fd479709"


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _spec(name: str, **overrides: Any) -> EndpointSpec:
    """Cheap EndpointSpec constructor for unit tests."""
    base = dict(
        name=name,
        category="custom",
        description=f"test method {name}",
        required_params=[],
        optional_params=[],
        error_codes=[400, 422],
        namespace=None,
    )
    base.update(overrides)
    return EndpointSpec(**base)


def _ok_response(body: Dict[str, Any]) -> wire.AGTPResponse:
    body_bytes = json.dumps(body).encode("utf-8")
    return wire.AGTPResponse(
        status_code=200, status_text="OK",
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(body_bytes))},
        body_bytes=body_bytes,
    )


def _err_response(code: int, error_code: str) -> wire.AGTPResponse:
    body = {"error": {"code": error_code}}
    body_bytes = json.dumps(body).encode("utf-8")
    return wire.AGTPResponse(
        status_code=code, status_text="Error",
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(body_bytes))},
        body_bytes=body_bytes,
    )


# ===========================================================================
# Layer 1: Recipe loader + pattern matching + policies.
# ===========================================================================


class RecipeLoaderTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, content: str) -> Path:
        p = self.tmp_path / "recipes.toml"
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return p

    def test_valid_toml_loads(self):
        p = self._write("""
            [[recipe]]
            name = "test"
            description = "test recipe"

            [recipe.pattern]
            name_exact = "EVALUATE"

            [[recipe.steps]]
            method = "QUERY"

              [recipe.steps.parameters.intent]
              kind = "proposal"
              value = "input"
        """)
        recipes = load_recipes(p)
        self.assertEqual(len(recipes), 1)
        self.assertEqual(recipes[0].name, "test")
        self.assertEqual(recipes[0].pattern.name_exact, "EVALUATE")
        self.assertEqual(len(recipes[0].steps), 1)
        self.assertEqual(recipes[0].steps[0].method_name, "QUERY")

    def test_missing_file_raises(self):
        with self.assertRaises(RecipeFileError) as ctx:
            load_recipes(self.tmp_path / "nope.toml")
        self.assertIn("not found", str(ctx.exception))

    def test_malformed_toml_raises(self):
        p = self._write("this is = not [valid toml")
        with self.assertRaises(RecipeFileError) as ctx:
            load_recipes(p)
        self.assertIn("invalid TOML", str(ctx.exception))

    def test_recipe_with_no_steps_raises(self):
        p = self._write("""
            [[recipe]]
            name = "broken"
            [recipe.pattern]
            name_exact = "X"
            steps = []
        """)
        with self.assertRaises(RecipeFileError) as ctx:
            load_recipes(p)
        self.assertIn("steps", str(ctx.exception))

    def test_recipe_with_bad_param_kind_raises(self):
        p = self._write("""
            [[recipe]]
            name = "broken"
            [recipe.pattern]
            name_exact = "X"
            [[recipe.steps]]
            method = "QUERY"
              [recipe.steps.parameters.intent]
              kind = "bogus"
              value = "y"
        """)
        with self.assertRaises(RecipeFileError) as ctx:
            load_recipes(p)
        self.assertIn("kind", str(ctx.exception))


class RecipePatternTests(unittest.TestCase):

    def test_name_exact_matches(self):
        pattern = RecipePattern(name_exact="EVALUATE")
        self.assertTrue(pattern.matches(_spec("EVALUATE")))
        self.assertFalse(pattern.matches(_spec("OTHER")))

    def test_name_regex_matches(self):
        pattern = RecipePattern(name_regex=r"^EV.*")
        self.assertTrue(pattern.matches(_spec("EVALUATE")))
        self.assertFalse(pattern.matches(_spec("RECONCILE")))

    def test_category_matches(self):
        pattern = RecipePattern(category="cognitive")
        self.assertTrue(pattern.matches(_spec("X", category="cognitive")))
        self.assertFalse(pattern.matches(_spec("X", category="transact")))

    def test_has_parameters_requires_all(self):
        pattern = RecipePattern(has_parameters=["input", "ruleset"])
        spec_with_both = _spec(
            "EVALUATE",
            required_params=[
                ParamSpec(name="input", type="string", description="x"),
                ParamSpec(name="ruleset", type="string", description="y"),
            ],
        )
        spec_with_one = _spec(
            "EVALUATE",
            required_params=[
                ParamSpec(name="input", type="string", description="x"),
            ],
        )
        self.assertTrue(pattern.matches(spec_with_both))
        self.assertFalse(pattern.matches(spec_with_one))

    def test_composite_pattern_logical_and(self):
        pattern = RecipePattern(name_exact="EVALUATE", category="custom")
        self.assertTrue(pattern.matches(_spec("EVALUATE", category="custom")))
        self.assertFalse(pattern.matches(_spec("EVALUATE", category="other")))


class RecipeBasedPolicyTests(unittest.TestCase):

    def test_compose_returns_plan_when_pattern_matches(self):
        recipe = Recipe(
            name="r",
            description="d",
            pattern=RecipePattern(name_exact="EVALUATE"),
            steps=[CompositionStep(
                method_name="QUERY",
                parameter_source={"intent": ParameterSource("proposal", "input")},
            )],
        )
        policy = RecipeBasedPolicy([recipe])
        proposal = _spec("EVALUATE")
        available = [_spec("QUERY"), _spec("SUMMARIZE")]
        plan = policy.compose(proposal, available)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.steps[0].method_name, "QUERY")
        self.assertEqual(plan.policy_name, "recipes")

    def test_compose_returns_none_when_no_pattern_matches(self):
        recipe = Recipe(
            name="r",
            description="d",
            pattern=RecipePattern(name_exact="EVALUATE"),
            steps=[CompositionStep(method_name="QUERY")],
        )
        policy = RecipeBasedPolicy([recipe])
        plan = policy.compose(_spec("AUDIT"), [_spec("QUERY")])
        self.assertIsNone(plan)

    def test_compose_skips_recipe_with_missing_underlying_method(self):
        recipe = Recipe(
            name="r",
            description="d",
            pattern=RecipePattern(name_exact="EVALUATE"),
            steps=[CompositionStep(method_name="MISSING")],
        )
        policy = RecipeBasedPolicy([recipe])
        plan = policy.compose(_spec("EVALUATE"), [_spec("QUERY")])
        self.assertIsNone(plan)

    def test_duplicate_recipe_names_rejected(self):
        r1 = Recipe(
            name="dup", description="", pattern=RecipePattern(name_exact="A"),
            steps=[CompositionStep(method_name="X")],
        )
        r2 = Recipe(
            name="dup", description="", pattern=RecipePattern(name_exact="B"),
            steps=[CompositionStep(method_name="Y")],
        )
        with self.assertRaises(ValueError):
            RecipeBasedPolicy([r1, r2])


class PassthroughPolicyTests(unittest.TestCase):

    def test_compose_returns_one_step_plan_for_existing_method(self):
        policy = PassthroughPolicy()
        available = [
            _spec(
                "QUERY",
                required_params=[
                    ParamSpec(name="intent", type="string", description="x"),
                ],
            ),
        ]
        plan = policy.compose(_spec("QUERY"), available)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].method_name, "QUERY")
        self.assertEqual(plan.policy_name, "passthrough")

    def test_compose_returns_none_when_method_unknown(self):
        policy = PassthroughPolicy()
        plan = policy.compose(_spec("AUDIT"), [_spec("QUERY")])
        self.assertIsNone(plan)


# ===========================================================================
# Layer 2: SynthesisRuntime against a stub dispatcher.
# ===========================================================================


class StubDispatcher:
    """Records each step it dispatches and returns a configured response."""

    def __init__(self):
        self.calls: list = []
        self.responses_by_method: Dict[str, wire.AGTPResponse] = {}
        self.default_response = _ok_response({"echoed": True})

    def __call__(self, request, server_state, agent_doc):
        self.calls.append({
            "method": request.method,
            "headers": dict(request.headers),
            "body": (
                json.loads(request.body_bytes.decode("utf-8"))
                if request.body_bytes else None
            ),
        })
        return self.responses_by_method.get(
            request.method, self.default_response,
        )


class SynthesisRuntimeUnitTests(unittest.TestCase):

    def setUp(self):
        self.stub = StubDispatcher()
        self.runtime = SynthesisRuntime(
            policies=[],  # no recipe policies; passthrough auto-appended
            step_dispatcher=self.stub,
        )

    def test_attempt_synthesis_uses_passthrough_when_proposal_matches(self):
        plan = self.runtime.attempt_synthesis(
            _spec("QUERY"), [_spec("QUERY"), _spec("SUMMARIZE")],
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan.policy_name, "passthrough")

    def test_attempt_synthesis_returns_none_when_no_policy_matches(self):
        plan = self.runtime.attempt_synthesis(
            _spec("UNKNOWN"), [_spec("QUERY")],
        )
        self.assertIsNone(plan)

    def test_instantiate_returns_id_and_writes_legacy_synthesis(self):
        plan = self.runtime.attempt_synthesis(
            _spec("QUERY"), [_spec("QUERY")],
        )
        synthesis_id = self.runtime.instantiate(plan)
        self.assertTrue(synthesis_id.startswith("syn-"))
        self.assertIs(self.runtime.get(synthesis_id), plan)
        # Legacy registry got the same id with target_method populated.
        legacy = self.runtime.legacy_registry.get(synthesis_id)
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy.target_method, "QUERY")

    def test_execute_single_step_plan_dispatches_underlying_method(self):
        plan = self.runtime.attempt_synthesis(
            _spec(
                "QUERY",
                required_params=[
                    ParamSpec(name="intent", type="string", description="x"),
                ],
            ),
            [_spec(
                "QUERY",
                required_params=[
                    ParamSpec(name="intent", type="string", description="x"),
                ],
            )],
        )
        synthesis_id = self.runtime.instantiate(plan)
        request = wire.AGTPRequest(
            method="QUERY",
            headers={"Synthesis-Id": synthesis_id, "Agent-ID": "x"},
            body_bytes=json.dumps({"intent": "hello"}).encode("utf-8"),
        )
        resp = self.runtime.execute(synthesis_id, request, None, None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.stub.calls), 1)
        # The inner step must NOT carry Synthesis-Id; runtime stripped it.
        self.assertNotIn("Synthesis-Id", self.stub.calls[0]["headers"])
        # The proposal-kind parameter source was identity-mapped.
        self.assertEqual(self.stub.calls[0]["body"], {"intent": "hello"})

    def test_execute_multi_step_plan_threads_outputs(self):
        plan = SynthesisPlan(
            proposed_method=_spec("AUDIT"),
            steps=[
                CompositionStep(
                    method_name="QUERY",
                    parameter_source={
                        "intent": ParameterSource("proposal", "subject"),
                    },
                    capture_output_as="facts",
                ),
                CompositionStep(
                    method_name="SUMMARIZE",
                    parameter_source={
                        "source": ParameterSource("previous_step", "facts"),
                    },
                ),
            ],
            output_aggregation="last",
        )
        # Dispatch returns distinct payloads per method.
        self.stub.responses_by_method["QUERY"] = _ok_response({"q": "fact"})
        self.stub.responses_by_method["SUMMARIZE"] = _ok_response({"summary": "ok"})
        synthesis_id = self.runtime.instantiate(plan)
        request = wire.AGTPRequest(
            method="AUDIT",
            headers={"Synthesis-Id": synthesis_id, "Agent-ID": "x"},
            body_bytes=json.dumps({"subject": "lauren"}).encode("utf-8"),
        )
        resp = self.runtime.execute(synthesis_id, request, None, None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.stub.calls), 2)
        # Step 2 received the output of step 1 under the SUMMARIZE
        # target parameter "source".
        self.assertEqual(self.stub.calls[1]["body"], {"source": {"q": "fact"}})
        # Aggregation = "last" so the final output is SUMMARIZE's body.
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["output"], {"summary": "ok"})

    def test_execute_aggregation_merge(self):
        plan = SynthesisPlan(
            proposed_method=_spec("AUDIT"),
            steps=[
                CompositionStep(method_name="QUERY"),
                CompositionStep(method_name="SUMMARIZE"),
            ],
            output_aggregation="merge",
        )
        self.stub.responses_by_method["QUERY"] = _ok_response({"a": 1})
        self.stub.responses_by_method["SUMMARIZE"] = _ok_response({"b": 2})
        synthesis_id = self.runtime.instantiate(plan)
        request = wire.AGTPRequest(
            method="AUDIT",
            headers={"Synthesis-Id": synthesis_id},
            body_bytes=b"",
        )
        resp = self.runtime.execute(synthesis_id, request, None, None)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["output"], {"a": 1, "b": 2})

    def test_execute_aggregation_list(self):
        plan = SynthesisPlan(
            proposed_method=_spec("INSPECT"),
            steps=[
                CompositionStep(method_name="DISCOVER"),
                CompositionStep(method_name="DESCRIBE"),
            ],
            output_aggregation="list",
        )
        self.stub.responses_by_method["DISCOVER"] = _ok_response({"x": 1})
        self.stub.responses_by_method["DESCRIBE"] = _ok_response({"y": 2})
        synthesis_id = self.runtime.instantiate(plan)
        request = wire.AGTPRequest(
            method="INSPECT",
            headers={"Synthesis-Id": synthesis_id},
            body_bytes=b"",
        )
        resp = self.runtime.execute(synthesis_id, request, None, None)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["output"], [{"x": 1}, {"y": 2}])

    def test_execute_step_failure_returns_structured_error(self):
        # First step succeeds, second fails — captured outputs from
        # the first must appear in the error body.
        plan = SynthesisPlan(
            proposed_method=_spec("AUDIT"),
            steps=[
                CompositionStep(
                    method_name="QUERY", capture_output_as="facts",
                ),
                CompositionStep(method_name="SUMMARIZE"),
            ],
        )
        self.stub.responses_by_method["QUERY"] = _ok_response({"q": "ok"})
        self.stub.responses_by_method["SUMMARIZE"] = _err_response(
            455, "scope-violation",
        )
        synthesis_id = self.runtime.instantiate(plan)
        request = wire.AGTPRequest(
            method="AUDIT",
            headers={"Synthesis-Id": synthesis_id},
            body_bytes=b"",
        )
        resp = self.runtime.execute(synthesis_id, request, None, None)
        # Status code mirrors the underlying failure.
        self.assertEqual(resp.status_code, 455)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["outcome"], "error")
        self.assertEqual(body["error"]["failed_step"], 1)
        self.assertEqual(body["error"]["method"], "SUMMARIZE")
        self.assertEqual(body["error"]["captured_outputs"], {"facts": {"q": "ok"}})

    def test_execute_unknown_synthesis_id_returns_404(self):
        request = wire.AGTPRequest(
            method="QUERY", headers={}, body_bytes=b"",
        )
        resp = self.runtime.execute("syn-bogus", request, None, None)
        self.assertEqual(resp.status_code, 404)
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "synthesis-not-found")

    def test_expire_removes_active_and_legacy(self):
        plan = self.runtime.attempt_synthesis(
            _spec("QUERY"), [_spec("QUERY")],
        )
        synthesis_id = self.runtime.instantiate(plan)
        self.assertTrue(self.runtime.expire(synthesis_id))
        self.assertIsNone(self.runtime.get(synthesis_id))
        self.assertIsNone(self.runtime.legacy_registry.get(synthesis_id))
        # Idempotent: expiring again returns False.
        self.assertFalse(self.runtime.expire(synthesis_id))


# ===========================================================================
# Layer 3: end-to-end against a live server.
# ===========================================================================


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


def _send(server, target, method, *, body=None, synthesis_id=""):
    headers = {"Agent-ID": target, "Accept": "application/json",
               "Host": server.host}
    if synthesis_id:
        headers["Synthesis-Id"] = synthesis_id
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


class SynthesisEndToEndTests(unittest.TestCase):
    """PROPOSE → synthesis_id → invoke → SUSPEND, end-to-end."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        agents_dir = Path(cls.tmp.name)
        _stage_agents(agents_dir)
        cls.registry = AgentRegistry(agents_dir)

        # Author a recipe matching AUDIT → QUERY + SUMMARIZE.
        cls.recipes_path = Path(cls.tmp.name) / "recipes.toml"
        cls.recipes_path.write_text(textwrap.dedent("""
            [[recipe]]
            name = "audit-via-query-and-summarize"
            description = "AUDIT = QUERY then SUMMARIZE."

            [recipe.pattern]
            name_exact = "AUDIT"

            [[recipe.steps]]
            method = "QUERY"
            capture_as = "facts"

              [recipe.steps.parameters.intent]
              kind = "proposal"
              value = "subject"

            [[recipe.steps]]
            method = "SUMMARIZE"

              [recipe.steps.parameters.source]
              kind = "previous_step"
              value = "facts"

              [recipe.steps.parameters.length]
              kind = "constant"
              value = "short"

            [recipe.aggregation]
            mode = "last"
        """), encoding="utf-8")

        cls.config = ServerConfig(
            server=ServerInfo(
                server_id="t.local", operator="x", contact="",
            ),
            policy=ServerPolicy(),
            agents=AgentsConfig(disclosure="public"),
            synthesis=SynthesisConfig(
                policies=["recipes"],
                recipes_file=str(cls.recipes_path),
            ),
        )
        cls.registry.configure_synthesis(cls.config)

        cls.server = _Server(cls.registry, cls.config)
        cls.server.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_propose_matching_recipe_returns_synthesis_id(self):
        resp = _send(
            self.server, ORCH_ID, "PROPOSE",
            body={
                "name": "AUDIT",
                "parameters": {"subject": "string"},
                "outcome": "summary",
                "description": "audit a subject by querying then summarizing",
            },
        )
        self.assertEqual(resp.status_code, 263)
        payload = json.loads(resp.body_bytes.decode("utf-8"))
        # §7 263 body: synthesis_id top-level, endpoint sub-block,
        # multi-step plan under the ``synthesis`` extras key.
        self.assertTrue(payload["synthesis_id"].startswith("syn-"))
        self.assertIn("endpoint", payload)
        synth_extras = payload["synthesis"]
        self.assertIn("plan", synth_extras)
        self.assertEqual(len(synth_extras["plan"]["steps"]), 2)

    def test_propose_no_recipe_falls_through_to_passthrough(self):
        # QUERY exists; passthrough policy yields a one-step plan.
        resp = _send(
            self.server, ORCH_ID, "PROPOSE",
            body={
                "name": "QUERY",
                "parameters": {"intent": "string"},
                "outcome": "results",
                "description": "ask the agent about itself",
            },
        )
        self.assertEqual(resp.status_code, 263)
        payload = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(payload["synthesis"]["target_method"], "QUERY")

    def test_propose_no_recipe_no_match_returns_463(self):
        # §7: refusal is now 463 proposal-rejected with structured
        # reason (was 422 negotiation-refused).
        resp = _send(
            self.server, ORCH_ID, "PROPOSE",
            body={
                "name": "ZBLARGON",
                "parameters": {},
                "outcome": "x",
                "description": "verb that has no recipe and no close match",
            },
        )
        self.assertEqual(resp.status_code, 463)
        payload = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "proposal-rejected")

    def test_invoke_via_synthesis_id_executes_plan(self):
        # 1. PROPOSE AUDIT → synthesis_id.
        resp = _send(
            self.server, ORCH_ID, "PROPOSE",
            body={
                "name": "AUDIT",
                "parameters": {"subject": "string"},
                "outcome": "summary",
                "description": "audit subject via QUERY + SUMMARIZE",
            },
        )
        self.assertEqual(resp.status_code, 263)
        synth_id = json.loads(resp.body_bytes.decode("utf-8"))["synthesis_id"]

        # 2. Invoke under the synthesis_id with the proposal's
        #    parameter shape. The runtime walks the plan: QUERY first
        #    (proposal.subject -> intent), SUMMARIZE second
        #    (previous_step.facts -> source, constant 'short' -> length).
        resp = _send(
            self.server, ORCH_ID, "AUDIT",
            body={"subject": "lauren"},
            synthesis_id=synth_id,
        )
        self.assertEqual(resp.status_code, 200, resp.body_bytes.decode())
        body = json.loads(resp.body_bytes.decode("utf-8"))
        self.assertEqual(body["outcome"], "ok")
        self.assertEqual(body["synthesis_id"], synth_id)
        # Two steps reported in the audit trail.
        self.assertEqual(len(body["steps"]), 2)
        self.assertEqual(body["steps"][0]["method"], "QUERY")
        self.assertEqual(body["steps"][1]["method"], "SUMMARIZE")

    def test_suspend_clears_synthesis(self):
        # PROPOSE → SUSPEND → re-invoke gets 404.
        resp = _send(
            self.server, ORCH_ID, "PROPOSE",
            body={
                "name": "AUDIT",
                "parameters": {"subject": "string"},
                "outcome": "summary",
                "description": "audit subject via QUERY + SUMMARIZE",
            },
        )
        self.assertEqual(resp.status_code, 263)
        synth_id = json.loads(resp.body_bytes.decode("utf-8"))["synthesis_id"]

        suspend = _send(
            self.server, ORCH_ID, "SUSPEND",
            body={"synthesis_id": synth_id, "reason": "test"},
        )
        self.assertEqual(suspend.status_code, 200)

        invoke = _send(
            self.server, ORCH_ID, "AUDIT",
            body={"subject": "x"},
            synthesis_id=synth_id,
        )
        self.assertEqual(invoke.status_code, 404)
        body = json.loads(invoke.body_bytes.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "synthesis-not-found")


if __name__ == "__main__":
    unittest.main()
