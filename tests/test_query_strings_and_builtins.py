"""
Tests for the AGTP-API §2 path-grammar alignment work.

Three scope buckets:

  * **Wire-format query parsing** — the request line ``METHOD
    PATH[?QUERY]`` parses into a bare path + a string-valued
    ``query`` dict; serialize round-trips. Fragments are rejected
    at the wire layer with a structured :class:`WireFormatError`.
  * **Dispatcher merging** — query parameters merge into the
    request input alongside the body before schema validation.
    Body wins on key conflicts; both flow through the same
    validator. The ``additionalProperties: false`` default is
    intact so a typo in either source surfaces.
  * **Built-in endpoints** — ``DISCOVER /methods`` returns a
    lightweight ``{method, path, description}`` listing of the
    server's registered endpoints. The endpoint registers
    automatically at server startup; an operator-authored TOML at
    the same ``(method, path)`` takes precedence.

Path-grammar permissiveness (mixed case, underscores, hyphens
all OK) is exercised in ``tests/test_path_grammar.py``; the
checks below are end-to-end via the dispatcher rather than
direct calls into ``core.path_grammar``.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import wire
from core.endpoint import (
    EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
)
from core.identity import AgentDocument, RequiresDeclaration
from core.path_grammar import PathGrammarError, validate_path
from server.endpoint_registry import EndpointRegistry
from server.methods import dispatch
from server.config import default_methods_policy as default_policy


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _make_state(reg: EndpointRegistry):
    class _State:
        endpoint_registry = reg
        methods_policy = default_policy()
        synthesis_runtime = None

        def list_ids(_self): return []
        def lookup(_self, _id): return None

    return _State()


def _make_agent(*, methods=None) -> AgentDocument:
    return AgentDocument(
        agtp_version="1.0", agent_id="0" * 64, name="T",
        principal="p", principal_id="0" * 64, description="",
        status="active", skills=[],
        requires=RequiresDeclaration(
            methods=list(methods or ["QUERY", "SCHEDULE", "BOOK"]),
            scopes=[], wildcards=False,
        ),
        scopes_accepted=[],
        issued_at="2026-05-09T00:00:00Z", issuer="t.local",
    )


# ===========================================================================
# Wire-format: query parsing + serialization.
# ===========================================================================


class WireQueryParseTests(unittest.TestCase):

    def test_request_with_query_parses_path_and_query_separately(self):
        raw = b"AGTP/1.0 SCHEDULE /meeting?date=050526&attendees=alice%2Cbob\r\n\r\n"
        req = wire.parse_request(io.BufferedReader(io.BytesIO(raw)))
        self.assertEqual(req.method, "SCHEDULE")
        self.assertEqual(req.path, "/meeting")
        self.assertEqual(req.query, {
            "date": "050526",
            "attendees": "alice,bob",
        })

    def test_path_without_query_yields_empty_query_dict(self):
        raw = b"AGTP/1.0 BOOK /room\r\n\r\n"
        req = wire.parse_request(io.BufferedReader(io.BytesIO(raw)))
        self.assertEqual(req.path, "/room")
        self.assertEqual(req.query, {})

    def test_two_token_form_still_parses(self):
        # No path token at all; back-compat for pre-Phase-2 callers.
        raw = b"AGTP/1.0 QUERY\r\n\r\n"
        req = wire.parse_request(io.BufferedReader(io.BytesIO(raw)))
        self.assertEqual(req.path, "/")
        self.assertEqual(req.query, {})

    def test_empty_path_before_query_defaults_to_root(self):
        # Bare ``?date=...`` with no path body is unusual but tolerable;
        # the parser fills in ``/`` and surfaces the query as-is.
        raw = b"AGTP/1.0 QUERY ?date=2026\r\n\r\n"
        req = wire.parse_request(io.BufferedReader(io.BytesIO(raw)))
        self.assertEqual(req.path, "/")
        self.assertEqual(req.query, {"date": "2026"})

    def test_repeated_keys_collapse_to_last_value(self):
        # Documented contract: query strings carry simple
        # ``key=value`` pairs; richer multi-valued shapes ride in
        # the body.
        raw = b"AGTP/1.0 QUERY /x?tag=a&tag=b&tag=c\r\n\r\n"
        req = wire.parse_request(io.BufferedReader(io.BytesIO(raw)))
        self.assertEqual(req.query, {"tag": "c"})

    def test_keyless_or_empty_query_pair_skipped(self):
        # ``?&=value&key=`` should produce ``{key: ""}`` (empty
        # value retained, keyless dropped, empty-pair dropped).
        raw = b"AGTP/1.0 QUERY /x?&key=&=val\r\n\r\n"
        req = wire.parse_request(io.BufferedReader(io.BytesIO(raw)))
        self.assertEqual(req.query, {"key": ""})


class WireQuerySerializeTests(unittest.TestCase):

    def test_round_trip_with_query(self):
        original = wire.AGTPRequest(
            method="SCHEDULE", path="/meeting",
            query={"date": "2026-05-12", "tz": "America/New_York"},
        )
        serialized = original.serialize()
        parsed = wire.parse_request(
            io.BufferedReader(io.BytesIO(serialized))
        )
        self.assertEqual(parsed.method, "SCHEDULE")
        self.assertEqual(parsed.path, "/meeting")
        self.assertEqual(parsed.query, original.query)

    def test_serialize_omits_question_when_query_empty(self):
        req = wire.AGTPRequest(method="BOOK", path="/room", query={})
        first_line = req.serialize().split(b"\r\n")[0]
        self.assertNotIn(b"?", first_line)
        self.assertEqual(first_line, b"AGTP/1.0 BOOK /room")

    def test_serialize_root_with_no_query_emits_two_token_form(self):
        # Default path + empty query stays byte-identical to a
        # pre-Phase-2 client (no third token).
        req = wire.AGTPRequest(method="QUERY")
        first_line = req.serialize().split(b"\r\n")[0]
        self.assertEqual(first_line, b"AGTP/1.0 QUERY")

    def test_serialize_url_encodes_special_chars(self):
        req = wire.AGTPRequest(
            method="QUERY", path="/x",
            query={"q": "hello world", "tag": "a,b"},
        )
        first_line = req.serialize().split(b"\r\n")[0].decode("utf-8")
        self.assertIn("hello%20world", first_line)
        self.assertIn("a%2Cb", first_line)


# ===========================================================================
# Wire-format: fragment rejection.
# ===========================================================================


class WireFragmentRejectionTests(unittest.TestCase):

    def test_fragment_in_path_rejected(self):
        raw = b"AGTP/1.0 QUERY /items#anchor\r\n\r\n"
        with self.assertRaises(wire.WireFormatError) as ctx:
            wire.parse_request(io.BufferedReader(io.BytesIO(raw)))
        self.assertIn("fragment", str(ctx.exception).lower())

    def test_fragment_after_query_rejected(self):
        raw = b"AGTP/1.0 QUERY /items?a=1#anchor\r\n\r\n"
        with self.assertRaises(wire.WireFormatError):
            wire.parse_request(io.BufferedReader(io.BytesIO(raw)))

    def test_bare_fragment_in_path_rejected(self):
        raw = b"AGTP/1.0 QUERY /#\r\n\r\n"
        with self.assertRaises(wire.WireFormatError):
            wire.parse_request(io.BufferedReader(io.BytesIO(raw)))


# ===========================================================================
# Path grammar permissiveness (smoke; full coverage in test_path_grammar.py).
# ===========================================================================


class PathPermissivenessTests(unittest.TestCase):

    def test_underscore_segments_accepted(self):
        # ``customer_id`` is not a verb token; underscores allowed.
        self.assertIsNone(validate_path("/customer_id"))
        self.assertIsNone(validate_path("/orders/{order_id}/line_items"))

    def test_mixed_case_segments_accepted(self):
        self.assertIsNone(validate_path("/Mixed_Case-Path"))
        self.assertIsNone(validate_path("/UPPER"))
        self.assertIsNone(validate_path("/CamelCase"))

    def test_hyphen_segments_accepted(self):
        self.assertIsNone(validate_path("/customer-records"))
        self.assertIsNone(validate_path("/orders/2026-Q1"))

    def test_verb_token_rejected_regardless_of_case(self):
        # ``BOOK``/``book``/``Book`` all normalize to BOOK; refusal
        # is case-insensitive.
        for path in ("/book/rooms", "/BOOK/rooms", "/Book/rooms"):
            with self.assertRaises(PathGrammarError):
                validate_path(path)

    def test_verb_token_rejected_with_separator_stripping(self):
        # ``get-rooms`` normalizes to GETROOMS (hyphens stripped) —
        # not a verb. But a *whole-segment* verb after stripping
        # IS rejected: ``get_orders`` → ``GETORDERS`` (not a verb;
        # accepted), ``orders/get`` → ``GET`` (rejected).
        self.assertIsNone(validate_path("/get-rooms"))
        self.assertIsNone(validate_path("/get_orders"))
        with self.assertRaises(PathGrammarError):
            validate_path("/orders/get")


# ===========================================================================
# Dispatcher: query merges into input.
# ===========================================================================


def _semantic() -> SemanticBlock:
    return SemanticBlock(
        intent="Schedule a meeting at the named time.",
        actor="agent",
        outcome="A scheduled meeting handle is returned.",
        capability="transaction",
        confidence=0.85,
        impact="reversible",
        is_idempotent=False,
    )


def _schedule_endpoint():
    """An endpoint that takes ``date`` (required) + ``length``
    (optional) so we can probe how query params merge with body
    params."""
    spec = EndpointSpec(
        name="SCHEDULE", path="/meeting",
        description="Schedule a meeting.",
        semantic=_semantic(),
        required_params=[
            ParamSpec(name="date", type="string",
                      description="meeting date"),
        ],
        optional_params=[
            ParamSpec(name="length", type="string",
                      description="duration"),
        ],
        output=[
            ParamSpec(name="meeting_id", type="string",
                      description="server-assigned id"),
        ],
        errors=["composition_failed"],
        handler=HandlerBinding(
            type="registered_function",
            reference="x.y.z",
        ),
    )

    def handler(ctx):
        from agtp.handlers import EndpointResponse
        return EndpointResponse(body={
            "meeting_id": f"m-{ctx.input.get('date','?')}-"
                          f"{ctx.input.get('length','?')}",
        })
    return spec, handler


class QueryMergeIntoInputTests(unittest.TestCase):

    def setUp(self):
        spec, handler = _schedule_endpoint()
        self.reg = EndpointRegistry()
        self.reg.register(spec, handler)
        self.state = _make_state(self.reg)
        self.agent = _make_agent()

    def _request(self, *, query=None, body=None):
        body_bytes = json.dumps(body).encode("utf-8") if body else b""
        headers = {"Accept": "application/json"}
        if body_bytes:
            headers["Content-Type"] = "application/json"
        return wire.AGTPRequest(
            method="SCHEDULE", path="/meeting", query=query or {},
            headers=headers, body_bytes=body_bytes,
        )

    def test_query_only_satisfies_required_input(self):
        req = self._request(query={"date": "2026-05-12"})
        resp = dispatch(req, self.state, self.agent)
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.body_bytes)
        self.assertEqual(body["meeting_id"], "m-2026-05-12-?")

    def test_body_only_satisfies_required_input(self):
        # Pre-Phase-§2 callers continue to work — body alone is fine.
        req = self._request(body={"date": "2026-05-12"})
        resp = dispatch(req, self.state, self.agent)
        self.assertEqual(resp.status_code, 200)

    def test_query_and_body_merged(self):
        req = self._request(
            query={"length": "30m"},
            body={"date": "2026-05-12"},
        )
        resp = dispatch(req, self.state, self.agent)
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.body_bytes)
        # Both came through the validator and reached the handler.
        self.assertEqual(body["meeting_id"], "m-2026-05-12-30m")

    def test_body_wins_on_key_conflict(self):
        # Documented contract: body is authoritative on key conflict.
        req = self._request(
            query={"date": "wrong"},
            body={"date": "2026-05-12"},
        )
        resp = dispatch(req, self.state, self.agent)
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.body_bytes)
        self.assertIn("2026-05-12", body["meeting_id"])
        self.assertNotIn("wrong", body["meeting_id"])

    def test_unknown_query_param_refused_by_input_validator(self):
        # additionalProperties: false applies to inputs; a query
        # key the schema doesn't recognize triggers 422.
        req = self._request(
            query={"date": "2026-05-12", "evil": "1"},
        )
        resp = dispatch(req, self.state, self.agent)
        self.assertEqual(resp.status_code, 422)
        body = json.loads(resp.body_bytes)
        self.assertEqual(body["error"]["code"], "input-validation-failed")
        self.assertIn("evil", body["error"]["detail"])


# ===========================================================================
# Built-in DISCOVER /methods endpoint (post-§6).
# ===========================================================================


class DiscoverMethodsBuiltinTests(unittest.TestCase):
    """Per agtp-api §6/§8, the server's built-in lightweight method
    inventory is ``DISCOVER /methods``. The endpoint reads the
    in-process endpoint_registry and returns a compact
    {method, path, description} array."""

    def test_builtin_registers_on_empty_registry(self):
        from server.builtins import register_builtins
        reg = EndpointRegistry()
        count = register_builtins(reg)
        self.assertEqual(count, 1)
        self.assertEqual(reg.count(), 1)
        # Registered at the documented (method, path) pair.
        self.assertIsNotNone(reg.lookup("DISCOVER", "/methods"))

    def test_builtin_returns_method_path_listing(self):
        from server.builtins import register_builtins
        reg = EndpointRegistry()
        # Register a sample operator endpoint so the listing has
        # more than just the built-in itself.
        sample = EndpointSpec(
            name="QUERY", path="/catalog",
            description="catalog query",
            semantic=_semantic(),
            required_params=[],
            optional_params=[],
            output=[ParamSpec(name="items", type="array", description="x")],
            errors=[],
            handler=HandlerBinding(
                type="registered_function",
                reference="sample.handler",
            ),
        )
        reg.register(sample, lambda ctx: None)
        register_builtins(reg)
        state = _make_state(reg)
        agent = _make_agent()
        req = wire.AGTPRequest(
            method="DISCOVER", headers={}, body_bytes=b"",
            path="/methods",
        )
        resp = dispatch(req, state, agent)
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.body_bytes)
        self.assertIn("methods", body)
        listing = {(e["method"], e["path"]) for e in body["methods"]}
        # Both the operator endpoint and the built-in itself are
        # visible in the listing.
        self.assertIn(("QUERY", "/catalog"), listing)
        self.assertIn(("DISCOVER", "/methods"), listing)

    def test_builtin_skipped_when_operator_already_registered(self):
        # Operator override — TOML registered (DISCOVER, /methods)
        # before the built-in attempts to. The built-in's
        # DuplicateEndpointError is silently swallowed; the
        # operator's handler stays.
        from server.builtins import register_builtins
        reg = EndpointRegistry()
        operator_handler = lambda ctx: None  # noqa: E731
        operator_spec = EndpointSpec(
            name="DISCOVER", path="/methods",
            description="operator override",
            semantic=_semantic(),
            required_params=[],
            optional_params=[],
            output=[ParamSpec(name="x", type="string", description="x")],
            errors=[],
            handler=HandlerBinding(
                type="registered_function",
                reference="x.y.z",
            ),
        )
        reg.register(operator_spec, operator_handler)
        # Built-in sees the duplicate; skips silently.
        count = register_builtins(reg)
        self.assertEqual(count, 0)
        self.assertEqual(reg.count(), 1)
        # Operator handler is still the one registered.
        looked = reg.lookup("DISCOVER", "/methods")
        self.assertIs(looked[1], operator_handler)

    def test_builtin_appears_in_manifest_section(self):
        # The endpoint is discoverable like any other endpoint.
        from server.builtins import register_builtins
        reg = EndpointRegistry()
        register_builtins(reg)
        section = reg.render_manifest_section()
        entries = [e for e in section if e["path"] == "/methods"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["method"], "DISCOVER")
        self.assertEqual(
            entries[0]["handler"], {"type": "registered_function"},
        )


if __name__ == "__main__":
    unittest.main()
