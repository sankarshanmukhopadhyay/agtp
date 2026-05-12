"""
Tests for :mod:`server.handler_resolution`.

Coverage:

  * ``resolve_registered_function`` — happy path, missing module,
    missing attribute, non-callable target, empty / dot-less
    reference.
  * ``resolve_external_service`` — Phase 2 stub raising
    NotImplementedError with a message that points at Phase 4.
  * ``resolve_composition`` (Phase 3) — happy path returns a
    callable, missing recipe / runtime / step methods raise
    structured ``InvalidHandlerError``s at registration, and the
    closure produces ``EndpointResponse`` / ``EndpointError`` for
    success and step-failure paths.
  * ``resolve_handler`` — dispatch through the right resolver based
    on ``binding.type``; refuses unknown types.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.endpoint import (
    EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
)
from server.handler_resolution import (
    InvalidHandlerError,
    resolve_composition,
    resolve_external_service,
    resolve_handler,
    resolve_registered_function,
)


# ===========================================================================
# resolve_registered_function — Phase 2's only fully-implemented kind.
# ===========================================================================


class RegisteredFunctionTests(unittest.TestCase):

    def test_resolves_known_dotted_path(self):
        # ``samples.handlers.query_catalog`` is shipped with the repo
        # for exactly this kind of smoke test.
        fn = resolve_registered_function("samples.handlers.query_catalog")
        self.assertTrue(callable(fn))

    def test_resolves_attribute_in_nested_package(self):
        fn = resolve_registered_function("samples.handlers.book_room")
        self.assertTrue(callable(fn))

    def test_missing_module_raises_import_failed(self):
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_registered_function("no.such.module.here")
        self.assertEqual(ctx.exception.detail, "import-failed")
        self.assertIn("no.such.module", str(ctx.exception))

    def test_missing_attribute_raises_attribute_missing(self):
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_registered_function("samples.handlers.no_such_function")
        self.assertEqual(ctx.exception.detail, "attribute-missing")
        self.assertIn("no_such_function", str(ctx.exception))

    def test_non_callable_target_raises_not_callable(self):
        # ``__name__`` is a string attribute; targeting it should
        # fail the callable check.
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_registered_function("samples.handlers.__name__")
        self.assertEqual(ctx.exception.detail, "not-callable")

    def test_empty_reference_raises_empty_reference(self):
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_registered_function("")
        self.assertEqual(ctx.exception.detail, "empty-reference")

    def test_dotless_reference_raises_attribute_missing(self):
        # Without a ``.`` we have no module/function split; the
        # resolver refuses with an attribute-missing tag because the
        # operator probably forgot the module prefix.
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_registered_function("query_catalog")
        self.assertEqual(ctx.exception.detail, "attribute-missing")


# ===========================================================================
# Composition is exercised in CompositionResolverTests below (Phase 3).
# external_service is exercised in test_external_service_handler.py and
# in ExternalServiceResolverTests below (Phase 4).
# ===========================================================================


# ===========================================================================
# resolve_handler — top-level dispatch.
# ===========================================================================


class TopLevelDispatchTests(unittest.TestCase):

    def test_dispatches_registered_function(self):
        binding = HandlerBinding(
            type="registered_function",
            reference="samples.handlers.query_catalog",
        )
        fn = resolve_handler(binding)
        self.assertTrue(callable(fn))

    def test_dispatches_composition_to_resolver(self):
        # Composition resolution in Phase 3 needs both server_state
        # and a spec; calling without them surfaces the
        # InvalidHandlerError "composition-needs-spec" tag.
        binding = HandlerBinding(type="composition", reference="r")
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_handler(binding)
        self.assertEqual(ctx.exception.detail, "composition-needs-spec")

    def test_dispatches_external_service_to_resolver(self):
        # Phase-4 external_service resolution: binding without
        # ``method`` raises a structured InvalidHandlerError so the
        # boot sequence catches it cleanly.
        binding = HandlerBinding(
            type="external_service",
            reference="https://x.example",
        )
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_handler(binding)
        self.assertEqual(
            ctx.exception.detail, "external-service-missing-method",
        )

    def test_unknown_binding_type_raises_invalid(self):
        binding = HandlerBinding(type="lambda", reference="x")
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_handler(binding)
        self.assertEqual(ctx.exception.detail, "bad-binding-type")


# ===========================================================================
# Composition resolver — Phase 3.
# ===========================================================================


def _semantic() -> SemanticBlock:
    return SemanticBlock(
        intent="Audit the named subject and return a structured assessment.",
        actor="agent",
        outcome="An audit summary covering the subject's current state is returned.",
        capability="analysis",
        confidence=0.80,
        impact="informational",
        is_idempotent=True,
    )


def _composition_spec(**overrides) -> EndpointSpec:
    base = dict(
        name="AUDIT", path="/reviews/{subject_id}",
        description="Audit a subject.",
        semantic=_semantic(),
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
    base.update(overrides)
    return EndpointSpec(**base)


def _make_runtime_with_recipes(recipe_path: Path = None):
    """Build a SynthesisRuntime loaded from agtp-recipes.toml."""
    from server.synthesis import (
        RecipeBasedPolicy, SynthesisRuntime, load_recipes,
    )
    REPO_ROOT = Path(__file__).resolve().parent.parent
    rp = recipe_path or (REPO_ROOT / "server" / "agtp-recipes.toml")
    recipes = load_recipes(rp)
    runtime = SynthesisRuntime(
        policies=[RecipeBasedPolicy(recipes)],
        step_dispatcher=lambda req, state, doc: None,  # not invoked here
    )
    return runtime


class CompositionResolverHappyPathTests(unittest.TestCase):

    def test_returns_callable_with_kind_tag(self):
        runtime = _make_runtime_with_recipes()
        # The recipe references QUERY + SUMMARIZE which are embedded
        # primitives present in REGISTRY.
        state = SimpleNamespace(synthesis_runtime=runtime)
        handler = resolve_composition(
            "audit-via-query-and-summarize",
            server_state=state,
            spec=_composition_spec(),
        )
        self.assertTrue(callable(handler))
        self.assertEqual(handler.__agtp_handler_kind__, "composition")
        self.assertEqual(
            handler.__agtp_recipe_name__,
            "audit-via-query-and-summarize",
        )

    def test_resolve_handler_dispatches_composition(self):
        runtime = _make_runtime_with_recipes()
        state = SimpleNamespace(synthesis_runtime=runtime)
        binding = HandlerBinding(
            type="composition",
            reference="audit-via-query-and-summarize",
        )
        handler = resolve_handler(
            binding, server_state=state, spec=_composition_spec(),
        )
        self.assertTrue(callable(handler))


class CompositionResolverErrorTests(unittest.TestCase):

    def test_missing_recipe_raises_recipe_not_found(self):
        runtime = _make_runtime_with_recipes()
        state = SimpleNamespace(synthesis_runtime=runtime)
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_composition(
                "no-such-recipe",
                server_state=state,
                spec=_composition_spec(),
            )
        self.assertEqual(ctx.exception.detail, "recipe-not-found")
        # Available recipes listed for the operator's debug.
        self.assertIn(
            "audit-via-query-and-summarize", str(ctx.exception),
        )

    def test_missing_step_method_raises_step_method_missing(self):
        # Build a runtime with a recipe whose step references a method
        # not in the live REGISTRY. The resolver must catch that at
        # registration so first-traffic isn't where it surfaces.
        from server.synthesis import (
            RecipeBasedPolicy, SynthesisRuntime,
            Recipe, RecipePattern,
        )
        from server.synthesis.plan import (
            CompositionStep, ParameterSource,
        )
        bad_recipe = Recipe(
            name="bad-recipe",
            description="references a method that doesn't exist",
            pattern=RecipePattern(name_exact="AUDIT"),
            steps=[
                CompositionStep(
                    method_name="NOSUCHMETHOD",
                    parameter_source={
                        "x": ParameterSource(kind="proposal", value="subject"),
                    },
                ),
            ],
        )
        runtime = SynthesisRuntime(
            policies=[RecipeBasedPolicy([bad_recipe])],
            step_dispatcher=lambda *a: None,
        )
        state = SimpleNamespace(synthesis_runtime=runtime)
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_composition(
                "bad-recipe",
                server_state=state,
                spec=_composition_spec(),
            )
        self.assertEqual(
            ctx.exception.detail, "recipe-step-method-missing",
        )
        self.assertIn("NOSUCHMETHOD", str(ctx.exception))

    def test_missing_runtime_raises_runtime_not_configured(self):
        state = SimpleNamespace(synthesis_runtime=None)
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_composition(
                "audit-via-query-and-summarize",
                server_state=state,
                spec=_composition_spec(),
            )
        self.assertEqual(ctx.exception.detail, "runtime-not-configured")

    def test_missing_spec_raises_composition_needs_spec(self):
        runtime = _make_runtime_with_recipes()
        state = SimpleNamespace(synthesis_runtime=runtime)
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_composition(
                "audit-via-query-and-summarize",
                server_state=state,
                spec=None,
            )
        self.assertEqual(ctx.exception.detail, "composition-needs-spec")

    def test_endpoint_missing_composition_failed_error(self):
        # Endpoint omits 'composition_failed' from its errors list:
        # the resolver must refuse so the handler doesn't surface
        # undeclared codes at runtime.
        runtime = _make_runtime_with_recipes()
        state = SimpleNamespace(synthesis_runtime=runtime)
        bad_spec = _composition_spec(errors=["subject_not_found"])
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_composition(
                "audit-via-query-and-summarize",
                server_state=state,
                spec=bad_spec,
            )
        self.assertEqual(
            ctx.exception.detail, "composition-missing-error-code",
        )

    def test_empty_reference_raises_empty_reference(self):
        runtime = _make_runtime_with_recipes()
        state = SimpleNamespace(synthesis_runtime=runtime)
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_composition(
                "", server_state=state, spec=_composition_spec(),
            )
        self.assertEqual(ctx.exception.detail, "empty-reference")


# ===========================================================================
# Composition closure execution.
# ===========================================================================


# ===========================================================================
# External-service resolver — Phase 4. Detailed coverage lives in
# test_external_service_handler.py; the tests below exercise the
# resolve_handler dispatch surface so binding-type routing is
# tested in this file too.
# ===========================================================================


class ExternalServiceResolverTests(unittest.TestCase):

    def _spec(self):
        return EndpointSpec(
            name="FETCH", path="/article/{id}",
            description="x",
            semantic=SemanticBlock(
                intent="Retrieve the article identified by the given id.",
                actor="agent",
                outcome="An article record is returned.",
                capability="retrieval",
                confidence=0.95,
                impact="informational",
                is_idempotent=True,
            ),
            required_params=[
                ParamSpec(name="id", type="integer", description="id"),
            ],
            output=[
                ParamSpec(name="title", type="string", description="t"),
            ],
            errors=[
                "upstream_error",
                "upstream_timeout",
                "upstream_connection_error",
                "upstream_malformed_response",
                "upstream_authentication_failed",
            ],
        )

    def test_resolve_handler_returns_callable_for_valid_external_service(self):
        binding = HandlerBinding(
            type="external_service",
            reference="https://api.example.com/v1/articles/1",
            method="GET",
            timeout_seconds=10.0,
        )
        handler = resolve_handler(binding, spec=self._spec())
        self.assertTrue(callable(handler))
        self.assertEqual(
            handler.__agtp_handler_kind__, "external_service",
        )

    def test_resolve_handler_refuses_external_service_without_method(self):
        binding = HandlerBinding(
            type="external_service",
            reference="https://api.example.com/v1",
        )
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_handler(binding, spec=self._spec())
        self.assertEqual(
            ctx.exception.detail, "external-service-missing-method",
        )

    def test_resolve_handler_refuses_external_service_with_http_scheme(self):
        binding = HandlerBinding(
            type="external_service",
            reference="http://api.example.com/v1",
            method="GET",
        )
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_handler(binding, spec=self._spec())
        self.assertEqual(
            ctx.exception.detail, "external-service-bad-scheme",
        )


class CompositionClosureExecutionTests(unittest.TestCase):
    """Execute the closure handler against a stub runtime and verify
    the output translates to EndpointResponse / EndpointError."""

    def test_success_response_carries_recipe_output(self):
        from agtp.handlers import EndpointContext, EndpointResponse
        runtime = _make_runtime_with_recipes()

        # Stub the runtime's execute_plan to return a synthetic
        # success response. (We test the runtime's actual execution
        # in test_dispatcher_endpoints.)
        import json
        from core import wire as wire_mod
        success_body = json.dumps({
            "method": "AUDIT",
            "synthesis_id": "",
            "outcome": "ok",
            "output": {"summary": "fake summary"},
            "steps": [],
        }).encode("utf-8")
        runtime.execute_plan = lambda *a, **k: wire_mod.AGTPResponse(
            status_code=200, status_text="OK",
            headers={"Content-Type": "application/json"},
            body_bytes=success_body,
        )

        # Stub server_state.lookup so the closure can resolve the
        # agent_doc.
        agent_doc = SimpleNamespace(agent_id="0" * 64)
        state = SimpleNamespace(
            synthesis_runtime=runtime,
            lookup=lambda _id: agent_doc,
        )

        handler = resolve_composition(
            "audit-via-query-and-summarize",
            server_state=state,
            spec=_composition_spec(),
        )

        ctx = EndpointContext(
            input={"subject": "order-42"},
            agent_id="0" * 64,
            server_state=state,
            method="AUDIT",
            path="/reviews/{subject_id}",
        )
        result = handler(ctx)
        self.assertIsInstance(result, EndpointResponse)
        self.assertEqual(result.body, {"summary": "fake summary"})

    def test_step_failure_returned_as_endpoint_error(self):
        from agtp.handlers import EndpointContext, EndpointError
        runtime = _make_runtime_with_recipes()

        import json
        from core import wire as wire_mod
        # Mimic the runtime's failure-shape body: top-level outcome=
        # "error", error block carries failed_step / method /
        # underlying_status.
        failure_body = json.dumps({
            "method": "AUDIT",
            "synthesis_id": "",
            "outcome": "error",
            "error": {
                "code": "synthesis-step-failed",
                "failed_step": 0,
                "method": "QUERY",
                "underlying_status": 403,
                "underlying": {
                    "error": {"code": "method-not-permitted-for-agent"},
                },
                "captured_outputs": {},
            },
            "steps": [{"method": "QUERY", "status_code": 403}],
        }).encode("utf-8")
        runtime.execute_plan = lambda *a, **k: wire_mod.AGTPResponse(
            status_code=403, status_text="Forbidden",
            headers={"Content-Type": "application/json"},
            body_bytes=failure_body,
        )

        agent_doc = SimpleNamespace(agent_id="0" * 64)
        state = SimpleNamespace(
            synthesis_runtime=runtime,
            lookup=lambda _id: agent_doc,
        )

        handler = resolve_composition(
            "audit-via-query-and-summarize",
            server_state=state,
            spec=_composition_spec(),
        )

        ctx = EndpointContext(
            input={"subject": "order-42"},
            agent_id="0" * 64,
            server_state=state,
            method="AUDIT",
            path="/reviews/{subject_id}",
        )
        result = handler(ctx)
        self.assertIsInstance(result, EndpointError)
        self.assertEqual(result.code, "composition_failed")
        self.assertEqual(result.details["failed_step"], 0)
        self.assertEqual(result.details["step_method"], "QUERY")
        self.assertEqual(result.details["underlying_status"], 403)
        self.assertEqual(
            result.details["recipe"], "audit-via-query-and-summarize",
        )

    def test_unknown_agent_surfaces_as_composition_failed(self):
        from agtp.handlers import EndpointContext, EndpointError
        runtime = _make_runtime_with_recipes()

        # server_state.lookup returns None: the agent isn't on the
        # server. The handler must surface that as a composition
        # failure rather than crashing.
        state = SimpleNamespace(
            synthesis_runtime=runtime,
            lookup=lambda _id: None,
        )
        handler = resolve_composition(
            "audit-via-query-and-summarize",
            server_state=state,
            spec=_composition_spec(),
        )
        ctx = EndpointContext(
            input={"subject": "x"},
            agent_id="missing",
            server_state=state,
            method="AUDIT",
            path="/reviews/{subject_id}",
        )
        result = handler(ctx)
        self.assertIsInstance(result, EndpointError)
        self.assertEqual(result.code, "composition_failed")


if __name__ == "__main__":
    unittest.main()
