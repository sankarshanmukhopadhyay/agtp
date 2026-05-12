"""
Serialization tests for the Phase-1 endpoint primitives.

Covers:

  * ``ParamSpec`` round-trips through ``to_dict`` / ``from_dict``,
    including the new ``enum`` and ``format`` fields plus the
    historical ``schema`` field.
  * ``HandlerBinding`` serialization (every recognized type).
  * ``EndpointSpec`` round-trips for fully-populated specs, with
    every field exercised; plus a sanity check that the
    historical-shape ``required_params`` / ``optional_params``
    keys still resolve via ``from_dict`` (PROPOSE bodies and
    persisted manifest entries carry that shape).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.endpoint import (
    EndpointSpec,
    FieldSpec,
    HandlerBinding,
    ParamSpec,
    SemanticBlock,
)


def _full_semantic() -> SemanticBlock:
    return SemanticBlock(
        intent="Reserve a room for the named guest at the named property.",
        actor="agent",
        outcome="A confirmed reservation_id is returned for the guest.",
        capability="transaction",
        confidence=0.85,
        impact="irreversible",
        is_idempotent=False,
    )


def _full_spec() -> EndpointSpec:
    return EndpointSpec(
        name="BOOK",
        path="/room",
        description="Books a room for the named guest.",
        namespace="reservations",
        semantic=_full_semantic(),
        required_params=[
            ParamSpec(
                name="guest_id", type="string",
                description="The booking guest's id.",
                format="uuid",
            ),
            ParamSpec(
                name="room_type", type="string",
                description="Category of room.",
                enum=["single", "double", "suite"],
            ),
            ParamSpec(
                name="dates", type="object",
                description="Check-in and check-out.",
                schema={
                    "type": "object",
                    "properties": {
                        "check_in": {"type": "string", "format": "date"},
                        "check_out": {"type": "string", "format": "date"},
                    },
                    "required": ["check_in", "check_out"],
                },
            ),
        ],
        optional_params=[
            ParamSpec(
                name="special_requests", type="string",
                description="Front-desk notes.",
            ),
        ],
        output=[
            ParamSpec(
                name="reservation_id", type="string",
                description="Server-assigned handle.",
                format="uuid",
            ),
        ],
        errors=["room_unavailable", "invalid_dates"],
        handler=HandlerBinding(
            type="registered_function",
            reference="staybeta.handlers.book_room",
        ),
    )


class ParamSpecSerializationTests(unittest.TestCase):

    def test_roundtrip_basic(self):
        p = ParamSpec(name="x", type="string", description="bare")
        d = p.to_dict()
        self.assertEqual(d["name"], "x")
        self.assertEqual(d["type"], "string")
        self.assertNotIn("enum", d)
        self.assertNotIn("format", d)
        self.assertNotIn("schema", d)
        round = ParamSpec.from_dict(d)
        self.assertEqual(round.name, "x")
        self.assertIsNone(round.enum)
        self.assertIsNone(round.format)

    def test_roundtrip_with_enum(self):
        p = ParamSpec(
            name="tier", type="string", description="role tier",
            enum=["free", "pro", "enterprise"],
        )
        d = p.to_dict()
        self.assertEqual(d["enum"], ["free", "pro", "enterprise"])
        round = ParamSpec.from_dict(d)
        self.assertEqual(round.enum, ["free", "pro", "enterprise"])

    def test_roundtrip_with_format(self):
        p = ParamSpec(
            name="when", type="string", description="ISO date",
            format="date",
        )
        round = ParamSpec.from_dict(p.to_dict())
        self.assertEqual(round.format, "date")

    def test_roundtrip_with_schema(self):
        p = ParamSpec(
            name="meta", type="object", description="nested",
            schema={"type": "object", "properties": {"k": {"type": "string"}}},
        )
        d = p.to_dict()
        self.assertEqual(d["schema"]["type"], "object")
        round = ParamSpec.from_dict(d)
        self.assertEqual(
            round.schema,
            {"type": "object", "properties": {"k": {"type": "string"}}},
        )

    def test_fieldspec_alias_resolves_to_paramspec(self):
        # FieldSpec is documented as the new general term but is
        # the same dataclass under the hood.
        self.assertIs(FieldSpec, ParamSpec)
        f = FieldSpec(name="x", type="string", description="ok")
        self.assertIsInstance(f, ParamSpec)


class HandlerBindingSerializationTests(unittest.TestCase):

    def test_roundtrip_registered_function(self):
        # §9: registered_function uses the ``function`` field.
        h = HandlerBinding(
            type="registered_function",
            function="staybeta.handlers.book_room",
        )
        d = h.to_dict()
        self.assertEqual(d, {
            "type": "registered_function",
            "function": "staybeta.handlers.book_room",
        })
        round = HandlerBinding.from_dict(d)
        self.assertEqual(round.type, h.type)
        self.assertEqual(round.function, h.function)

    def test_roundtrip_composition(self):
        # §9: composition uses the ``recipe`` field.
        h = HandlerBinding(type="composition", recipe="audit-recipe")
        round = HandlerBinding.from_dict(h.to_dict())
        self.assertEqual(round.type, "composition")
        self.assertEqual(round.recipe, "audit-recipe")

    def test_roundtrip_external_service(self):
        # §9: external_service uses the ``url`` field.
        h = HandlerBinding(
            type="external_service",
            url="https://upstream.example/api/v1",
        )
        round = HandlerBinding.from_dict(h.to_dict())
        self.assertEqual(round.type, "external_service")
        self.assertEqual(round.url, "https://upstream.example/api/v1")

    def test_legacy_reference_kwarg_routes_per_type(self):
        # §9 back-compat: callers may still construct with the
        # generic ``reference`` kwarg; __post_init__ routes it.
        h1 = HandlerBinding(
            type="registered_function", reference="m.fn",
        )
        self.assertEqual(h1.function, "m.fn")
        h2 = HandlerBinding(type="composition", reference="my-recipe")
        self.assertEqual(h2.recipe, "my-recipe")
        h3 = HandlerBinding(type="external_service", reference="https://x")
        self.assertEqual(h3.url, "https://x")

    def test_reference_value_property_returns_routed_value(self):
        # ``reference_value`` is the type-agnostic read accessor
        # for code that doesn't want to switch on type.
        h = HandlerBinding(type="registered_function", function="m.fn")
        self.assertEqual(h.reference_value, "m.fn")
        h2 = HandlerBinding(type="composition", recipe="r")
        self.assertEqual(h2.reference_value, "r")

    def test_legacy_input_map_kwarg_routes_to_input_transform(self):
        h = HandlerBinding(
            type="external_service", url="https://x", method="GET",
            input_map={"a": "b"},
        )
        self.assertEqual(h.input_transform, {"a": "b"})

    def test_from_dict_accepts_legacy_reference_key(self):
        # Pre-§9 wire / TOML payloads used ``reference``; from_dict
        # accepts it.
        h = HandlerBinding.from_dict({
            "type": "registered_function",
            "reference": "legacy.path",
        })
        self.assertEqual(h.function, "legacy.path")
        self.assertEqual(h.to_dict(), {
            "type": "registered_function",
            "function": "legacy.path",
        })


class EndpointSpecSerializationTests(unittest.TestCase):

    def test_full_roundtrip(self):
        spec = _full_spec()
        d = spec.to_dict()

        # Every Phase-1 canonical key is present.
        for key in (
            "method", "path", "description", "namespace",
            "input", "output", "errors", "semantic", "handler",
        ):
            self.assertIn(key, d, f"missing key {key!r}")

        # And the historical ``name`` / params keys ride alongside.
        self.assertEqual(d["name"], "BOOK")
        self.assertEqual(d["method"], "BOOK")
        self.assertEqual(d["required_params"], d["input"]["required"])
        self.assertEqual(d["optional_params"], d["input"]["optional"])

        round = EndpointSpec.from_dict(d)
        self.assertEqual(round.name, "BOOK")
        self.assertEqual(round.path, "/room")
        self.assertEqual(round.namespace, "reservations")
        self.assertEqual(len(round.required_params), 3)
        self.assertEqual(len(round.optional_params), 1)
        self.assertEqual(len(round.output), 1)
        self.assertEqual(round.errors, ["room_unavailable", "invalid_dates"])
        self.assertEqual(round.handler.type, "registered_function")
        self.assertEqual(
            round.handler.function, "staybeta.handlers.book_room",
        )

    def test_method_property_aliases_name(self):
        spec = _full_spec()
        self.assertEqual(spec.method, spec.name)
        self.assertEqual(spec.method, "BOOK")

    def test_input_required_and_optional_aliases(self):
        spec = _full_spec()
        self.assertEqual(spec.input_required, spec.required_params)
        self.assertEqual(spec.input_optional, spec.optional_params)

    def test_complex_param_schema_survives_roundtrip(self):
        spec = _full_spec()
        d = spec.to_dict()
        # The dates param has an inline JSON Schema; it must survive
        # to_dict / from_dict.
        round = EndpointSpec.from_dict(d)
        dates = next(p for p in round.required_params if p.name == "dates")
        self.assertEqual(dates.schema["properties"]["check_in"]["format"], "date")

    def test_from_dict_accepts_historical_proposal_shape(self):
        # PROPOSE bodies use ``name`` plus ``required_params`` /
        # ``optional_params`` as flat lists; from_dict must still
        # construct a usable spec from that older shape.
        legacy = {
            "name": "RECONCILE",
            "description": "Reconciles ledger entries.",
            "required_params": [
                {"name": "account_id", "type": "string",
                 "description": "ledger account"},
            ],
            "optional_params": [],
            "semantic": _full_semantic().to_dict(),
            "namespace": "finance",
        }
        spec = EndpointSpec.from_dict(legacy)
        self.assertEqual(spec.name, "RECONCILE")
        self.assertEqual(spec.required_params[0].name, "account_id")
        self.assertEqual(spec.namespace, "finance")
        # Path / handler / output / errors default cleanly when absent.
        self.assertIsNone(spec.path)
        self.assertIsNone(spec.handler)
        self.assertEqual(spec.output, [])
        self.assertEqual(spec.errors, [])

    def test_from_proposal_accepts_path_field(self):
        # CLI's --propose --params-file flow puts ``path`` in the
        # body. EndpointSpec.from_proposal must surface it.
        proposal = {
            "name": "RECONCILE",
            "path": "/orders/{order_id}",
            "parameters": {"account_id": "string"},
        }
        spec = EndpointSpec.from_proposal(proposal)
        self.assertEqual(spec.path, "/orders/{order_id}")

    def test_empty_optional_blocks_roundtrip(self):
        # A spec with no optional params, no output, no errors must
        # serialize and deserialize cleanly without dropping fields.
        spec = EndpointSpec(
            name="QUERY",
            path="/catalog",
            description="catalog",
            semantic=_full_semantic(),
            required_params=[],
            optional_params=[],
            output=[],
            errors=[],
            handler=HandlerBinding(
                type="registered_function",
                reference="server.methods.handle_query",
            ),
        )
        d = spec.to_dict()
        round = EndpointSpec.from_dict(d)
        self.assertEqual(round.optional_params, [])
        self.assertEqual(round.output, [])
        self.assertEqual(round.errors, [])


if __name__ == "__main__":
    unittest.main()
