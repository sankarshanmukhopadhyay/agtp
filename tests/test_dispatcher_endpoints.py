"""
End-to-end dispatcher tests for the Phase-2 endpoint registry.

Covers the new gates wired into ``server.methods.dispatch``:

  * (method, path) hit → handler runs, response is the validated
    EndpointResponse body.
  * Input validation failure → 422 with structured detail.
  * Required-scope violation → 403 ``insufficient_scope``.
  * Unknown path (not "/") → 404 ``endpoint-not-found``.
  * Known path, wrong method → 405 with ``allowed_methods_for_path``.
  * Handler returns EndpointError → 422 with the error name + details.
  * Handler raises an exception → 500 ``handler-exception``.
  * Handler returns the wrong type → 500 ``bad-handler-return-type``.
  * Output schema violation → 500 ``output-validation-failed``.
  * Composition / external_service handler bindings raise
    NotImplementedError at registry-load time so the operator's
    boot sequence can skip them.

The fixture builds an in-process registry + dispatcher invocation
and bypasses the wire layer; we exercise dispatch directly.
"""

from __future__ import annotations

import json
import sys
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse
from core import wire
from core.endpoint import (
    EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
)
from core.identity import AgentDocument, RequiresDeclaration
from server.endpoint_registry import EndpointRegistry
from server.handler_resolution import resolve_handler
from server.methods import dispatch


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _semantic(**overrides) -> SemanticBlock:
    base = dict(
        intent="Reserve a room for the named guest at the named property.",
        actor="agent",
        outcome="A confirmed reservation_id is returned for the guest.",
        capability="transaction",
        confidence=0.85,
        impact="irreversible",
        is_idempotent=False,
    )
    base.update(overrides)
    return SemanticBlock(**base)


def _book_spec(**overrides) -> EndpointSpec:
    base = dict(
        name="BOOK", path="/room",
        description="Books a room.",
        semantic=_semantic(),
        required_params=[
            ParamSpec(name="guest_id", type="string",
                      description="guest id"),
            ParamSpec(name="room_type", type="string",
                      description="room category",
                      enum=["single", "double", "suite"]),
        ],
        optional_params=[],
        output=[
            ParamSpec(name="reservation_id", type="string",
                      description="server-assigned"),
        ],
        errors=["room_unavailable"],
        handler=HandlerBinding(
            type="registered_function",
            reference="samples.handlers.book_room",
        ),
    )
    base.update(overrides)
    return EndpointSpec(**base)


def _make_agent(scopes: List[str] = None) -> AgentDocument:
    """Minimal agent doc the dispatcher accepts. We populate the
    fields the dispatcher reads (agent_id + requires.scopes /
    requires.methods); the rest take stub values. soft-deny isn't
    invoked because we call dispatch() directly."""
    return AgentDocument(
        agtp_version="1.0",
        agent_id="0" * 64,
        name="Test Agent",
        principal="test-principal",
        principal_id="0" * 64,
        description="",
        status="active",
        skills=[],
        requires=RequiresDeclaration(
            methods=["BOOK", "QUERY", "RESERVE"],
            scopes=list(scopes or []),
            wildcards=False,
        ),
        scopes_accepted=[],
        issued_at="2026-05-09T00:00:00Z",
        issuer="test.local",
    )


class _FakeServerState:
    """Stand-in for AgentRegistry — just carries the bits dispatch
    reads (endpoint_registry + methods_policy + synthesis_runtime)."""

    def __init__(self, endpoint_registry: EndpointRegistry):
        self.endpoint_registry = endpoint_registry
        from server.config import default_methods_policy as default_policy
        self.methods_policy = default_policy()
        self.synthesis_runtime = None

    def list_ids(self) -> List[str]: return []
    def lookup(self, agent_id: str): return None


def _request(method: str, path: str, body: Dict[str, Any] = None) -> wire.AGTPRequest:
    body_bytes = json.dumps(body).encode("utf-8") if body else b""
    headers = {"Accept": "application/json", "Host": "localhost"}
    if body_bytes:
        headers["Content-Type"] = "application/json"
    return wire.AGTPRequest(
        method=method, headers=headers,
        body_bytes=body_bytes, path=path,
    )


def _decode(resp: wire.AGTPResponse) -> Dict[str, Any]:
    return json.loads(resp.body_bytes.decode("utf-8"))


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


class HappyPathTests(unittest.TestCase):

    def setUp(self):
        self.reg = EndpointRegistry()
        spec = _book_spec()
        self.reg.register(spec, handler=resolve_handler(spec.handler))
        self.state = _FakeServerState(self.reg)
        self.agent = _make_agent()

    def test_valid_request_hits_handler(self):
        req = _request("BOOK", "/room", {
            "guest_id": "00000000", "room_type": "double",
        })
        resp = dispatch(req, self.state, self.agent)
        self.assertEqual(resp.status_code, 200)
        body = _decode(resp)
        self.assertIn("reservation_id", body)

    def test_handler_returned_endpoint_error_becomes_422(self):
        # 'suite' is in the spec's enum but not in the sample handler's
        # available room types; the handler returns an EndpointError.
        req = _request("BOOK", "/room", {
            "guest_id": "00000000", "room_type": "suite",
        })
        resp = dispatch(req, self.state, self.agent)
        self.assertEqual(resp.status_code, 422)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "room_unavailable")
        self.assertEqual(body["error"]["method"], "BOOK")
        self.assertEqual(body["error"]["path"], "/room")
        self.assertIn("requested", body["error"]["details"])


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


class InputValidationTests(unittest.TestCase):

    def setUp(self):
        self.reg = EndpointRegistry()
        spec = _book_spec()
        self.reg.register(spec, handler=resolve_handler(spec.handler))
        self.state = _FakeServerState(self.reg)
        self.agent = _make_agent()

    def test_missing_required_field_returns_422(self):
        req = _request("BOOK", "/room", {"room_type": "double"})  # no guest_id
        resp = dispatch(req, self.state, self.agent)
        self.assertEqual(resp.status_code, 422)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "input-validation-failed")
        self.assertIn("guest_id", body["error"]["detail"])

    def test_enum_violation_returns_422(self):
        req = _request("BOOK", "/room", {
            "guest_id": "00000000", "room_type": "penthouse",
        })
        resp = dispatch(req, self.state, self.agent)
        self.assertEqual(resp.status_code, 422)
        body = _decode(resp)
        self.assertEqual(body["error"]["field"], "/room_type")


# ---------------------------------------------------------------------------
# Authority.
# ---------------------------------------------------------------------------


class AuthorityTests(unittest.TestCase):

    def setUp(self):
        self.reg = EndpointRegistry()
        spec = _book_spec(required_scopes=["bookings:write"])
        self.reg.register(spec, handler=resolve_handler(spec.handler))
        self.state = _FakeServerState(self.reg)

    def test_missing_scope_returns_262(self):
        # §7: authority refusals consolidate under 262 Authorization
        # Required with error.type='scope-required'. The pre-§7 form
        # returned 403 with error.code='insufficient_scope'.
        agent = _make_agent(scopes=[])  # no bookings:write
        req = _request("BOOK", "/room", {
            "guest_id": "00000000", "room_type": "double",
        })
        resp = dispatch(req, self.state, agent)
        self.assertEqual(resp.status_code, 262)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "authorization-required")
        self.assertEqual(body["error"]["type"], "scope-required")
        self.assertEqual(
            body["error"]["details"]["missing_scopes"], ["bookings:write"],
        )

    def test_present_scope_passes(self):
        agent = _make_agent(scopes=["bookings:write"])
        req = _request("BOOK", "/room", {
            "guest_id": "00000000", "room_type": "double",
        })
        resp = dispatch(req, self.state, agent)
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# 404 / 405 / wrong-method.
# ---------------------------------------------------------------------------


class NotFoundAndNotAllowedTests(unittest.TestCase):

    def setUp(self):
        self.reg = EndpointRegistry()
        # Two methods at /room so 405 has something to list.
        self.reg.register(_book_spec(), handler=lambda ctx: EndpointResponse(body={"reservation_id": "x"}))
        self.reg.register(
            _book_spec(name="CANCEL"),
            handler=lambda ctx: EndpointResponse(body={"reservation_id": "x"}),
        )
        self.state = _FakeServerState(self.reg)
        self.agent = _make_agent()

    def test_unknown_path_returns_404(self):
        req = _request("BOOK", "/unknown")
        resp = dispatch(req, self.state, self.agent)
        self.assertEqual(resp.status_code, 404)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "endpoint-not-found")
        self.assertEqual(body["error"]["path"], "/unknown")

    def test_known_path_wrong_method_returns_405(self):
        # RECONCILE is a catalog verb, not registered at /room.
        req = _request("RECONCILE", "/room", {"x": 1})
        resp = dispatch(req, self.state, self.agent)
        self.assertEqual(resp.status_code, 405)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "method-not-allowed")
        self.assertEqual(
            sorted(body["error"]["allowed_methods_for_path"]),
            ["BOOK", "CANCEL"],
        )

    def test_root_path_falls_through_to_method_only(self):
        # path "/" with no endpoint at (BOOK, "/") should fall
        # through to the existing method-only REGISTRY. BOOK isn't
        # in REGISTRY either, so we expect 405 method-not-implemented
        # (the existing error shape, not the new method-not-allowed).
        req = _request("BOOK", "/", {"x": 1})
        resp = dispatch(req, self.state, self.agent)
        self.assertEqual(resp.status_code, 405)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "method-not-implemented")


# ---------------------------------------------------------------------------
# Handler error paths.
# ---------------------------------------------------------------------------


class HandlerErrorTests(unittest.TestCase):

    def setUp(self):
        self.state_factory = lambda r: _FakeServerState(r)
        self.agent = _make_agent()

    def test_undeclared_error_code_returns_500(self):
        # Handler returns an EndpointError code that the spec's
        # ``errors`` list doesn't declare → 500.
        spec = _book_spec(errors=["room_unavailable"])

        def bad_handler(ctx):
            return EndpointError(code="not-declared", message="oops")

        reg = EndpointRegistry()
        reg.register(spec, handler=bad_handler)
        req = _request("BOOK", "/room", {
            "guest_id": "00000000", "room_type": "double",
        })
        resp = dispatch(req, self.state_factory(reg), self.agent)
        self.assertEqual(resp.status_code, 500)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "undeclared-error-code")

    def test_handler_raising_returns_500(self):
        def bomb(ctx):
            raise RuntimeError("boom")

        spec = _book_spec()
        reg = EndpointRegistry()
        reg.register(spec, handler=bomb)
        req = _request("BOOK", "/room", {
            "guest_id": "00000000", "room_type": "double",
        })
        resp = dispatch(req, self.state_factory(reg), self.agent)
        self.assertEqual(resp.status_code, 500)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "handler-exception")
        self.assertIn("boom", body["error"]["detail"])

    def test_handler_returning_wrong_type_returns_500(self):
        def bare_dict(ctx):
            return {"reservation_id": "abc"}  # not an EndpointResponse

        spec = _book_spec()
        reg = EndpointRegistry()
        reg.register(spec, handler=bare_dict)
        req = _request("BOOK", "/room", {
            "guest_id": "00000000", "room_type": "double",
        })
        resp = dispatch(req, self.state_factory(reg), self.agent)
        self.assertEqual(resp.status_code, 500)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "bad-handler-return-type")

    def test_handler_response_failing_output_schema_returns_500(self):
        def missing_field(ctx):
            return EndpointResponse(body={})  # no reservation_id

        spec = _book_spec()
        reg = EndpointRegistry()
        reg.register(spec, handler=missing_field)
        req = _request("BOOK", "/room", {
            "guest_id": "00000000", "room_type": "double",
        })
        resp = dispatch(req, self.state_factory(reg), self.agent)
        self.assertEqual(resp.status_code, 500)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "output-validation-failed")


# ---------------------------------------------------------------------------
# Phase-2 stubs (composition / external_service).
# ---------------------------------------------------------------------------


class StubBindingTests(unittest.TestCase):

    def test_external_service_resolution_now_implemented(self):
        # Phase-4 external_service resolution: an obviously invalid
        # binding (no method) raises a structured
        # InvalidHandlerError so the boot sequence catches it.
        from server.handler_resolution import (
            InvalidHandlerError, resolve_handler,
        )
        binding = HandlerBinding(
            type="external_service", reference="https://x.example",
        )
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_handler(binding)
        self.assertEqual(
            ctx.exception.detail, "external-service-missing-method",
        )


# ---------------------------------------------------------------------------
# Phase-3 composition end-to-end.
# ---------------------------------------------------------------------------
#
# Composition-bound endpoints route through the synthesis runtime,
# which walks recipe steps through the same dispatcher external
# invocations go through. Tests below exercise:
#
#   * happy-path: the recipe runs and the response carries the merged /
#     last step output.
#   * authority preservation: an agent that lacks permission for one
#     of the recipe's step methods gets a `composition_failed` back
#     (not silent success) — the per-step capability check fires.
#   * registration-time refusals: malformed composition endpoints
#     (missing recipe, recipe references unknown step, missing
#     `composition_failed` in errors) are caught at register time.


class _CompositionFixture:
    """Shared fixture that wires the runtime, registry, and a server-
    state stub for the composition tests."""

    def __init__(self, agent_methods: List[str] = None,
                 wildcards: bool = False):
        from server.synthesis import (
            RecipeBasedPolicy, SynthesisRuntime, load_recipes,
        )
        from server.endpoint_registry import EndpointRegistry

        REPO_ROOT = Path(__file__).resolve().parent.parent
        recipes = load_recipes(REPO_ROOT / "server" / "agtp-recipes.toml")

        self.endpoint_registry = EndpointRegistry()

        # Build the runtime with the step dispatcher pointing back at
        # ``server.methods.dispatch`` so each step goes through the
        # full gate sequence (capability checks included).
        from server.methods import dispatch
        self.runtime = SynthesisRuntime(
            policies=[RecipeBasedPolicy(recipes)],
            step_dispatcher=lambda req, state, doc: dispatch(req, state, doc),
        )

        # Server-state stub: looks like the AgentRegistry slice the
        # composition handler reaches for (synthesis_runtime,
        # endpoint_registry, methods_policy, lookup, list_ids).
        from server.config import default_methods_policy as default_policy
        self.agent = self._make_agent(
            agent_methods or ["AUDIT", "QUERY", "SUMMARIZE", "DESCRIBE"],
            wildcards=wildcards,
        )
        agent_dict = {self.agent.agent_id: self.agent}

        class _State:
            synthesis_runtime = self.runtime
            endpoint_registry = self.endpoint_registry
            methods_policy = default_policy()

            def list_ids(_self): return list(agent_dict.keys())
            def lookup(_self, agent_id): return agent_dict.get(agent_id)

        self.state = _State()

    @staticmethod
    def _make_agent(methods: List[str], wildcards: bool = False):
        return AgentDocument(
            agtp_version="1.0",
            agent_id="0" * 64,
            name="Composition Test Agent",
            principal="test-principal",
            principal_id="0" * 64,
            description="",
            status="active",
            skills=[],
            requires=RequiresDeclaration(
                methods=list(methods),
                scopes=[],
                wildcards=wildcards,
            ),
            scopes_accepted=[],
            issued_at="2026-05-09T00:00:00Z",
            issuer="test.local",
        )

    def register_audit_endpoint(self, errors=None) -> EndpointSpec:
        """Register the AUDIT /reviews/{subject_id} sample endpoint
        bound to the audit-via-query-and-summarize recipe."""
        from server.handler_resolution import resolve_handler
        spec = EndpointSpec(
            name="AUDIT", path="/reviews/{subject_id}",
            description="Audit a subject.",
            semantic=SemanticBlock(
                intent="Audit the named subject and return a structured assessment.",
                actor="agent",
                outcome="An audit summary covering the subject's current state is returned.",
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
            errors=list(errors) if errors is not None
                  else ["composition_failed"],
            handler=HandlerBinding(
                type="composition",
                reference="audit-via-query-and-summarize",
            ),
        )
        handler = resolve_handler(
            spec.handler, server_state=self.state, spec=spec,
        )
        self.endpoint_registry.register(spec, handler)
        return spec


class CompositionEndToEndTests(unittest.TestCase):

    def test_composition_endpoint_runs_recipe_end_to_end(self):
        fx = _CompositionFixture()
        fx.register_audit_endpoint()
        req = _request("AUDIT", "/reviews/{subject_id}", {
            "subject": "order-42",
        })
        resp = dispatch(req, fx.state, fx.agent)
        self.assertEqual(resp.status_code, 200)
        body = _decode(resp)
        # The recipe's merge aggregation produced a body whose last
        # step (SUMMARIZE) populated the ``summary`` key, satisfying
        # the endpoint's output schema.
        self.assertIn("summary", body)
        self.assertIn("stub-summary", body["summary"])

    def test_composition_threads_input_to_steps(self):
        # The recipe's first step uses ``proposal -> subject`` for
        # the QUERY step's ``intent`` parameter; verify the input
        # value flows through to the QUERY handler's output.
        fx = _CompositionFixture()
        fx.register_audit_endpoint()
        req = _request("AUDIT", "/reviews/{subject_id}", {
            "subject": "order-99",
        })
        resp = dispatch(req, fx.state, fx.agent)
        body = _decode(resp)
        # SUMMARIZE wraps QUERY's body via previous_step; the
        # intent text rides through.
        self.assertIn("order-99", body["summary"])


class CompositionAuthorityPreservationTests(unittest.TestCase):

    def test_step_authority_failure_surfaces_as_composition_failed(self):
        # Agent declares AUDIT (so the endpoint runs) but NOT QUERY
        # (the recipe's first step). The runtime walks step 0 via
        # the step dispatcher → soft_deny doesn't apply (we run
        # dispatch directly) but check_capability does. QUERY
        # requires the agent to declare it. The step returns 403,
        # the runtime returns the failure envelope, and the
        # composition handler converts that to EndpointError.
        fx = _CompositionFixture(
            agent_methods=["AUDIT"],  # missing QUERY, SUMMARIZE
        )
        fx.register_audit_endpoint()
        req = _request("AUDIT", "/reviews/{subject_id}", {
            "subject": "order-42",
        })
        resp = dispatch(req, fx.state, fx.agent)
        self.assertEqual(resp.status_code, 422)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "composition_failed")
        # The details name the failed step + its method so the agent
        # knows which capability they're missing.
        self.assertEqual(body["error"]["details"]["step_method"], "QUERY")
        self.assertIn("failed_step", body["error"]["details"])

    def test_wildcards_agent_runs_composition(self):
        # An agent with requires.wildcards=True clears the per-step
        # capability check (subject to server policy). The
        # composition runs to completion.
        fx = _CompositionFixture(agent_methods=[], wildcards=True)
        fx.register_audit_endpoint()
        req = _request("AUDIT", "/reviews/{subject_id}", {
            "subject": "order-42",
        })
        resp = dispatch(req, fx.state, fx.agent)
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# Phase-4 external_service end-to-end.
# ---------------------------------------------------------------------------


class ExternalServiceEndToEndTests(unittest.TestCase):
    """Register an external_service-bound endpoint, dispatch a
    request, verify the upstream call is made (with the right
    method / URL / body) and the response translates back to AGTP.

    The ``_do_external_request`` helper is mocked so the tests
    don't hit the network."""

    def _make_state(self):
        from server.endpoint_registry import EndpointRegistry
        from server.config import default_methods_policy as default_policy

        class _State:
            endpoint_registry = EndpointRegistry()
            methods_policy = default_policy()
            synthesis_runtime = None

            def list_ids(_self): return []
            def lookup(_self, _id): return None

        return _State()

    def _spec(self):
        return EndpointSpec(
            name="FETCH", path="/article/{id}",
            description="Fetch an article from the upstream.",
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
                ParamSpec(name="article_id", type="integer",
                          description="article id"),
            ],
            output=[
                ParamSpec(name="title", type="string",
                          description="article title"),
            ],
            errors=[
                "article_not_found",
                "upstream_timeout",
                "upstream_connection_error",
                "upstream_malformed_response",
                "upstream_authentication_failed",
                "upstream_error",
            ],
        )

    def _binding(self, **overrides):
        base = dict(
            type="external_service",
            reference="https://api.example.com/v1/articles/1",
            method="GET",
            timeout_seconds=10.0,
        )
        base.update(overrides)
        return HandlerBinding(**base)

    def _register(self, spec, binding):
        from server.handler_resolution import resolve_handler
        # Attach the binding to the spec so the registry's structural
        # validation has the handler block to check.
        spec.handler = binding
        handler = resolve_handler(binding, spec=spec)
        state = self._make_state()
        state.endpoint_registry.register(spec, handler)
        return state

    def test_success_round_trip(self):
        from server.handler_resolution import _UpstreamRequestOutcome
        spec = self._spec()
        binding = self._binding(
            output_map={"author_id": "userId"},
            error_map={"404": "article_not_found"},
        )
        state = self._register(spec, binding)
        agent = _make_agent()

        outcome = _UpstreamRequestOutcome(
            status=200,
            body_bytes=json.dumps({
                "id": 1, "userId": 7, "title": "Hello",
            }).encode("utf-8"),
        )
        with mock.patch(
            "server.handler_resolution._do_external_request",
            return_value=outcome,
        ) as mocked:
            req = _request("FETCH", "/article/{id}", {"article_id": 1})
            resp = dispatch(req, state, agent)

        self.assertEqual(resp.status_code, 200)
        body = _decode(resp)
        # output_map renamed userId -> author_id; ``title`` rode
        # through unchanged so the output validator passes.
        self.assertEqual(body["author_id"], 7)
        self.assertEqual(body["title"], "Hello")
        # The upstream was hit with the configured method / URL.
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(
            kwargs["url"], "https://api.example.com/v1/articles/1",
        )

    def test_mapped_404_becomes_endpoint_error(self):
        from server.handler_resolution import _UpstreamRequestOutcome
        spec = self._spec()
        binding = self._binding(
            error_map={"404": "article_not_found"},
        )
        state = self._register(spec, binding)
        agent = _make_agent()

        outcome = _UpstreamRequestOutcome(
            status=404, body_bytes=b'{"detail":"no"}',
        )
        with mock.patch(
            "server.handler_resolution._do_external_request",
            return_value=outcome,
        ):
            req = _request("FETCH", "/article/{id}", {"article_id": 99})
            resp = dispatch(req, state, agent)

        self.assertEqual(resp.status_code, 422)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "article_not_found")
        self.assertEqual(body["error"]["details"]["upstream_status"], 404)

    def test_unmapped_5xx_becomes_upstream_error(self):
        from server.handler_resolution import _UpstreamRequestOutcome
        spec = self._spec()
        binding = self._binding()
        state = self._register(spec, binding)
        agent = _make_agent()

        outcome = _UpstreamRequestOutcome(status=503, body_bytes=b"{}")
        with mock.patch(
            "server.handler_resolution._do_external_request",
            return_value=outcome,
        ):
            req = _request("FETCH", "/article/{id}", {"article_id": 1})
            resp = dispatch(req, state, agent)
        self.assertEqual(resp.status_code, 422)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "upstream_error")

    def test_timeout_translated_to_upstream_timeout(self):
        from server.handler_resolution import _UpstreamRequestOutcome
        spec = self._spec()
        binding = self._binding()
        state = self._register(spec, binding)
        agent = _make_agent()

        outcome = _UpstreamRequestOutcome(
            failure_kind="upstream_timeout",
            detail="read timed out after 10s",
        )
        with mock.patch(
            "server.handler_resolution._do_external_request",
            return_value=outcome,
        ):
            req = _request("FETCH", "/article/{id}", {"article_id": 1})
            resp = dispatch(req, state, agent)
        self.assertEqual(resp.status_code, 422)
        body = _decode(resp)
        self.assertEqual(body["error"]["code"], "upstream_timeout")


class ExternalServiceRegistrationFailureTests(unittest.TestCase):

    def test_http_scheme_refused_at_registration(self):
        from server.handler_resolution import (
            InvalidHandlerError, resolve_handler,
        )
        spec = EndpointSpec(
            name="FETCH", path="/article/{id}",
            description="x",
            semantic=SemanticBlock(
                intent="Retrieve an article by id from the upstream.",
                actor="agent",
                outcome="An article is returned.",
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
            errors=["upstream_error"],
            handler=HandlerBinding(
                type="external_service",
                reference="http://api.example.com/v1",
                method="GET",
            ),
        )
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_handler(spec.handler, spec=spec)
        self.assertEqual(
            ctx.exception.detail, "external-service-bad-scheme",
        )


class CompositionRegistrationFailureTests(unittest.TestCase):

    def setUp(self):
        from server.synthesis import (
            RecipeBasedPolicy, SynthesisRuntime, load_recipes,
        )
        REPO_ROOT = Path(__file__).resolve().parent.parent
        recipes = load_recipes(REPO_ROOT / "server" / "agtp-recipes.toml")
        self.runtime = SynthesisRuntime(
            policies=[RecipeBasedPolicy(recipes)],
            step_dispatcher=lambda *a: None,
        )
        self.state = SimpleNamespace(synthesis_runtime=self.runtime)

    def test_unknown_recipe_refuses_at_registration(self):
        from server.handler_resolution import (
            InvalidHandlerError, resolve_handler,
        )
        spec = EndpointSpec(
            name="AUDIT", path="/reviews/{subject_id}",
            description="x",
            semantic=SemanticBlock(
                intent="Audit the named subject and return findings.",
                actor="agent",
                outcome="A summary is returned.",
                capability="analysis",
                confidence=0.8,
                impact="informational",
                is_idempotent=True,
            ),
            required_params=[
                ParamSpec(name="x", type="string", description="x"),
            ],
            output=[
                ParamSpec(name="summary", type="string", description="x"),
            ],
            errors=["composition_failed"],
            handler=HandlerBinding(
                type="composition",
                reference="no-such-recipe",
            ),
        )
        with self.assertRaises(InvalidHandlerError) as ctx:
            resolve_handler(
                spec.handler, server_state=self.state, spec=spec,
            )
        self.assertEqual(ctx.exception.detail, "recipe-not-found")


if __name__ == "__main__":
    unittest.main()
