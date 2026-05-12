"""
Tests for :mod:`server.schema_validation`.

Coverage:

  * Schema construction: ``spec_to_input_schema`` builds the
    expected JSON Schema document; required + optional inputs flow
    into the right slots; enum / format / inline schema all
    survive.
  * Input validation: happy path, missing required field, wrong
    type, enum violation, format violation (date), optional fields
    omitted, extra fields refused (additionalProperties: false).
  * Output validation: required output present (happy), required
    output missing (refuses), additional output keys allowed.
  * Error shape: :class:`InputValidationError` carries the failing
    field's JSON Pointer and the underlying schema-path.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.endpoint import (
    EndpointSpec, HandlerBinding, ParamSpec, SemanticBlock,
)
from server.schema_validation import (
    InputValidationError,
    OutputValidationError,
    spec_to_input_schema,
    spec_to_output_schema,
    validate_input,
    validate_output,
)


def _semantic() -> SemanticBlock:
    return SemanticBlock(
        intent="Reserve a room for the named guest at the named property.",
        actor="agent",
        outcome="A confirmed reservation_id is returned for the guest.",
        capability="transaction",
        confidence=0.85,
        impact="irreversible",
        is_idempotent=False,
    )


def _spec(**overrides) -> EndpointSpec:
    base = dict(
        name="BOOK", path="/room",
        semantic=_semantic(),
        required_params=[
            ParamSpec(name="guest_id", type="string",
                      description="guest id", format="uuid"),
            ParamSpec(name="check_in", type="string",
                      description="ISO date", format="date"),
            ParamSpec(name="room_type", type="string",
                      description="room category",
                      enum=["single", "double", "suite"]),
        ],
        optional_params=[
            ParamSpec(name="notes", type="string",
                      description="front-desk notes"),
        ],
        output=[
            ParamSpec(name="reservation_id", type="string",
                      description="server-assigned handle"),
        ],
        handler=HandlerBinding(
            type="registered_function", reference="x.y.z",
        ),
    )
    base.update(overrides)
    return EndpointSpec(**base)


# ===========================================================================
# Schema construction.
# ===========================================================================


class SchemaConstructionTests(unittest.TestCase):

    def test_input_schema_lists_required_and_properties(self):
        schema = spec_to_input_schema(_spec())
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(
            schema["required"],
            sorted(["guest_id", "check_in", "room_type"]),
        )
        self.assertIn("notes", schema["properties"])  # optional present

    def test_input_schema_layers_format_enum(self):
        schema = spec_to_input_schema(_spec())
        self.assertEqual(
            schema["properties"]["check_in"]["format"], "date",
        )
        self.assertEqual(
            schema["properties"]["room_type"]["enum"],
            ["single", "double", "suite"],
        )

    def test_inline_schema_overrides_synthesized_fragment(self):
        complex_spec = ParamSpec(
            name="meta", type="object", description="nested",
            schema={"type": "object",
                    "properties": {"k": {"type": "string"}},
                    "required": ["k"]},
        )
        spec = _spec(required_params=[complex_spec], optional_params=[])
        schema = spec_to_input_schema(spec)
        self.assertEqual(
            schema["properties"]["meta"]["properties"]["k"]["type"],
            "string",
        )

    def test_output_schema_marks_outputs_required(self):
        schema = spec_to_output_schema(_spec())
        self.assertEqual(schema["required"], ["reservation_id"])
        # additionalProperties is True for outputs so handlers can
        # add forward-compat fields.
        self.assertTrue(schema["additionalProperties"])


# ===========================================================================
# Input validation.
# ===========================================================================


def _valid_body() -> dict:
    return {
        "guest_id": "00000000-0000-0000-0000-000000000001",
        "check_in": "2026-05-12",
        "room_type": "double",
    }


class InputValidationTests(unittest.TestCase):

    def test_valid_body_passes(self):
        body = _valid_body()
        out = validate_input(_spec(), body)
        self.assertEqual(out["guest_id"], body["guest_id"])

    def test_missing_required_field_refused(self):
        body = _valid_body()
        body.pop("check_in")
        with self.assertRaises(InputValidationError) as ctx:
            validate_input(_spec(), body)
        self.assertIn("check_in", str(ctx.exception))

    def test_wrong_type_refused(self):
        body = _valid_body()
        body["guest_id"] = 42  # not a string
        with self.assertRaises(InputValidationError) as ctx:
            validate_input(_spec(), body)
        self.assertEqual(ctx.exception.field, "/guest_id")

    def test_enum_violation_refused(self):
        body = _valid_body()
        body["room_type"] = "penthouse"  # not in enum
        with self.assertRaises(InputValidationError) as ctx:
            validate_input(_spec(), body)
        self.assertEqual(ctx.exception.field, "/room_type")

    def test_date_format_violation_refused(self):
        body = _valid_body()
        body["check_in"] = "tomorrow"  # not a date
        with self.assertRaises(InputValidationError) as ctx:
            validate_input(_spec(), body)
        self.assertEqual(ctx.exception.field, "/check_in")

    def test_optional_field_may_be_absent(self):
        body = _valid_body()
        # ``notes`` is optional and absent — must pass.
        out = validate_input(_spec(), body)
        self.assertNotIn("notes", out)

    def test_optional_field_when_present_validated(self):
        body = _valid_body()
        body["notes"] = "extra towels"
        out = validate_input(_spec(), body)
        self.assertEqual(out["notes"], "extra towels")

    def test_extra_field_refused(self):
        # additionalProperties: False — typo'd inputs surface early.
        body = _valid_body()
        body["evil"] = "trojan"
        with self.assertRaises(InputValidationError) as ctx:
            validate_input(_spec(), body)
        self.assertIn("evil", str(ctx.exception))

    def test_none_body_treated_as_empty(self):
        # When the request carries no body and the spec has required
        # params, validation refuses with a missing-property error
        # rather than crashing.
        with self.assertRaises(InputValidationError):
            validate_input(_spec(), None)

    def test_email_format_enforced(self):
        spec = _spec(
            required_params=[
                ParamSpec(name="contact", type="string",
                          description="email", format="email"),
            ],
            optional_params=[],
        )
        with self.assertRaises(InputValidationError) as ctx:
            validate_input(spec, {"contact": "not-an-email"})
        self.assertEqual(ctx.exception.field, "/contact")
        # And valid email passes.
        validate_input(spec, {"contact": "alice@example.com"})


# ===========================================================================
# Output validation.
# ===========================================================================


class OutputValidationTests(unittest.TestCase):

    def test_valid_output_passes(self):
        out = validate_output(_spec(), {"reservation_id": "res-001"})
        self.assertEqual(out["reservation_id"], "res-001")

    def test_missing_required_output_refused(self):
        with self.assertRaises(OutputValidationError):
            validate_output(_spec(), {})

    def test_additional_output_keys_allowed(self):
        # additionalProperties=True — handlers can return forward-compat
        # extras without the validator complaining.
        out = validate_output(
            _spec(),
            {"reservation_id": "res-001", "next_check_in": "2026-05-13"},
        )
        self.assertEqual(out["next_check_in"], "2026-05-13")


# ===========================================================================
# Error rendering.
# ===========================================================================


class ErrorRenderingTests(unittest.TestCase):

    def test_validation_error_carries_field_path(self):
        body = _valid_body()
        body["check_in"] = "tomorrow"
        try:
            validate_input(_spec(), body)
        except InputValidationError as exc:
            self.assertEqual(exc.field, "/check_in")
            self.assertTrue(exc.schema_path)
            d = exc.to_dict()
            self.assertEqual(d["field"], "/check_in")
            self.assertIn("date", d["message"])
            return
        self.fail("expected InputValidationError")


if __name__ == "__main__":
    unittest.main()
