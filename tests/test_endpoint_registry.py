"""
Tests for :class:`server.endpoint_registry.EndpointRegistry`.

Coverage:

  * Successful register + lookup round-trip.
  * Each validation rule fires with the right ``detail`` tag.
  * Duplicate registration raises :class:`DuplicateEndpointError`.
  * ``methods_for_path`` / ``has_path`` / ``all_endpoints`` /
    ``count`` reflect the live state.
  * ``render_manifest_section`` produces the Phase-1 manifest shape
    (Phase-1 keys only, no historical aliases leaked).
  * Concurrent registration from multiple threads doesn't corrupt
    state.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.endpoint import (
    EndpointSpec,
    HandlerBinding,
    ParamSpec,
    SemanticBlock,
)
from server.endpoint_registry import (
    DuplicateEndpointError,
    EndpointRegistry,
    InvalidEndpointError,
)


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


def _spec(**overrides) -> EndpointSpec:
    """Build a fully-valid spec; tests override one field at a time
    to exercise specific validation rules."""
    base = dict(
        name="BOOK",
        path="/room",
        description="Books a room.",
        namespace="reservations",
        semantic=_semantic(),
        required_params=[
            ParamSpec(name="guest_id", type="string",
                      description="guest id"),
        ],
        optional_params=[],
        output=[
            ParamSpec(name="reservation_id", type="string",
                      description="server-assigned handle"),
        ],
        errors=["room_unavailable"],
        handler=HandlerBinding(
            type="registered_function",
            reference="staybeta.handlers.book_room",
        ),
    )
    base.update(overrides)
    return EndpointSpec(**base)


# ===========================================================================
# Successful registration + lookup.
# ===========================================================================


class HappyPathTests(unittest.TestCase):

    def test_register_and_lookup(self):
        reg = EndpointRegistry()
        spec = _spec()
        reg.register(spec, handler=lambda *a, **k: None)
        looked = reg.lookup("BOOK", "/room")
        self.assertIsNotNone(looked)
        self.assertIs(looked[0], spec)

    def test_lookup_uppercases_method(self):
        reg = EndpointRegistry()
        reg.register(_spec())
        # Callers may pass lowercase; the registry normalizes.
        self.assertIsNotNone(reg.lookup("book", "/room"))

    def test_lookup_returns_none_for_miss(self):
        reg = EndpointRegistry()
        reg.register(_spec())
        self.assertIsNone(reg.lookup("BOOK", "/wrong"))
        self.assertIsNone(reg.lookup("RESERVE", "/room"))

    def test_count_reflects_state(self):
        reg = EndpointRegistry()
        self.assertEqual(reg.count(), 0)
        reg.register(_spec())
        self.assertEqual(reg.count(), 1)
        reg.register(_spec(name="RESERVE"))
        self.assertEqual(reg.count(), 2)

    def test_all_endpoints_preserves_insertion_order(self):
        reg = EndpointRegistry()
        a = _spec(name="BOOK", path="/room")
        b = _spec(name="RESERVE", path="/table")
        c = _spec(name="QUERY", path="/catalog",
                  semantic=_semantic(capability="retrieval",
                                     impact="informational",
                                     is_idempotent=True))
        reg.register(a); reg.register(b); reg.register(c)
        names = [s.name for s in reg.all_endpoints()]
        self.assertEqual(names, ["BOOK", "RESERVE", "QUERY"])


# ===========================================================================
# methods_for_path / has_path.
# ===========================================================================


class PathQueryTests(unittest.TestCase):

    def test_methods_for_path_returns_set(self):
        reg = EndpointRegistry()
        reg.register(_spec(name="BOOK", path="/room"))
        reg.register(_spec(name="RESERVE", path="/room"))
        reg.register(_spec(name="QUERY", path="/catalog",
                           semantic=_semantic(capability="retrieval",
                                              impact="informational",
                                              is_idempotent=True)))
        methods = reg.methods_for_path("/room")
        self.assertEqual(methods, {"BOOK", "RESERVE"})
        self.assertEqual(reg.methods_for_path("/catalog"), {"QUERY"})

    def test_methods_for_path_unknown_returns_empty_set(self):
        reg = EndpointRegistry()
        reg.register(_spec())
        self.assertEqual(reg.methods_for_path("/nope"), set())

    def test_has_path_correctness(self):
        reg = EndpointRegistry()
        reg.register(_spec())
        self.assertTrue(reg.has_path("/room"))
        self.assertFalse(reg.has_path("/empty"))


# ===========================================================================
# Validation rules.
# ===========================================================================


class ValidationTests(unittest.TestCase):

    def test_unknown_verb_refused(self):
        reg = EndpointRegistry()
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(name="FROBNICATE"))
        self.assertEqual(ctx.exception.detail, "verb-not-in-catalog")

    def test_missing_path_refused(self):
        reg = EndpointRegistry()
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(path=None))
        self.assertEqual(ctx.exception.detail, "path-missing")

    def test_path_grammar_violation_refused(self):
        reg = EndpointRegistry()
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(path="/get/orders"))
        # ``/get/orders`` triggers the verb-in-path rule.
        self.assertTrue(
            ctx.exception.detail.startswith("path-grammar:"),
            ctx.exception.detail,
        )
        self.assertIn("verb-in-path", ctx.exception.detail)

    def test_path_must_begin_with_slash(self):
        reg = EndpointRegistry()
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(path="room"))
        self.assertTrue(ctx.exception.detail.startswith("path-grammar:"))

    def test_missing_semantic_refused(self):
        reg = EndpointRegistry()
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(semantic=None))
        self.assertEqual(ctx.exception.detail, "semantic-missing")

    def test_semantic_field_individually_required(self):
        reg = EndpointRegistry()
        for field, value in (
            ("intent", ""), ("actor", ""), ("outcome", ""),
            ("capability", None), ("confidence", None),
            ("impact", None), ("is_idempotent", None),
        ):
            with self.subTest(field=field):
                sem = _semantic()
                setattr(sem, field, value)
                with self.assertRaises(InvalidEndpointError) as ctx:
                    EndpointRegistry().register(_spec(semantic=sem))
                self.assertEqual(
                    ctx.exception.detail,
                    f"semantic-missing-field:{field}",
                )

    def test_freeform_actor_accepted(self):
        # Per agtp-api §6, ``actor`` is a free-form identifier — values
        # outside the suggested vocabulary (e.g., domain-specific tags
        # like ``merchant`` / ``auditor``) are accepted. Empty actor is
        # still refused by the missing-field check.
        reg = EndpointRegistry()
        # A clearly non-default actor should pass.
        reg.register(_spec(semantic=_semantic(actor="merchant")))
        # And an empty actor must still raise.
        with self.assertRaises(InvalidEndpointError) as ctx:
            EndpointRegistry().register(_spec(semantic=_semantic(actor="")))
        self.assertEqual(ctx.exception.detail, "semantic-missing-field:actor")

    def test_bad_capability_refused(self):
        reg = EndpointRegistry()
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(semantic=_semantic(capability="magic")))
        self.assertEqual(ctx.exception.detail, "semantic-bad-capability")

    def test_bad_impact_refused(self):
        reg = EndpointRegistry()
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(semantic=_semantic(impact="catastrophic")))
        self.assertEqual(ctx.exception.detail, "semantic-bad-impact")

    def test_bad_confidence_refused(self):
        reg = EndpointRegistry()
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(semantic=_semantic(confidence=1.5)))
        self.assertEqual(ctx.exception.detail, "semantic-bad-confidence")

    def test_param_with_unrecognized_type_refused(self):
        reg = EndpointRegistry()
        bad = ParamSpec(name="x", type="bigint", description="custom type")
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(required_params=[bad]))
        self.assertIn("bad-type", ctx.exception.detail)

    def test_param_with_empty_description_refused(self):
        reg = EndpointRegistry()
        bad = ParamSpec(name="x", type="string", description="")
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(required_params=[bad]))
        self.assertIn("empty-description", ctx.exception.detail)

    def test_param_with_empty_name_refused(self):
        reg = EndpointRegistry()
        bad = ParamSpec(name="", type="string", description="ok")
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(required_params=[bad]))
        self.assertIn("empty-name", ctx.exception.detail)

    def test_output_param_validated_too(self):
        reg = EndpointRegistry()
        bad = ParamSpec(name="x", type="bigint", description="...")
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(output=[bad]))
        self.assertIn("output", ctx.exception.detail)

    def test_errors_must_be_list_of_strings(self):
        reg = EndpointRegistry()
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(errors=[42]))  # type: ignore[list-item]
        self.assertTrue(ctx.exception.detail.startswith("errors-bad-shape"))

    def test_handler_required(self):
        reg = EndpointRegistry()
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(handler=None))
        self.assertEqual(ctx.exception.detail, "handler-missing")

    def test_handler_type_must_be_recognized(self):
        reg = EndpointRegistry()
        bad = HandlerBinding(type="lambda", reference="x")
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(handler=bad))
        self.assertEqual(ctx.exception.detail, "handler-bad-type")

    def test_handler_reference_must_be_non_empty(self):
        reg = EndpointRegistry()
        bad = HandlerBinding(type="registered_function", reference="")
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(_spec(handler=bad))
        self.assertEqual(ctx.exception.detail, "handler-empty-reference")


# ===========================================================================
# Duplicate detection.
# ===========================================================================


class DuplicateTests(unittest.TestCase):

    def test_duplicate_method_path_pair_refused(self):
        reg = EndpointRegistry()
        reg.register(_spec())
        with self.assertRaises(DuplicateEndpointError) as ctx:
            reg.register(_spec())
        self.assertEqual(ctx.exception.method, "BOOK")
        self.assertEqual(ctx.exception.path, "/room")

    def test_same_method_different_path_is_fine(self):
        reg = EndpointRegistry()
        reg.register(_spec(path="/room"))
        reg.register(_spec(path="/suite"))   # no raise
        self.assertEqual(reg.count(), 2)

    def test_same_path_different_method_is_fine(self):
        reg = EndpointRegistry()
        reg.register(_spec(name="BOOK"))
        reg.register(_spec(name="RESERVE"))   # no raise
        self.assertEqual(reg.count(), 2)


# ===========================================================================
# Manifest rendering.
# ===========================================================================


class ManifestRenderingTests(unittest.TestCase):

    def test_manifest_section_uses_canonical_keys(self):
        reg = EndpointRegistry()
        reg.register(_spec())
        section = reg.render_manifest_section()
        self.assertEqual(len(section), 1)
        entry = section[0]

        # Canonical AGTP-API keys present.
        for key in (
            "method", "path", "description",
            "input_schema", "output_schema",
            "errors", "semantic", "handler", "namespace",
        ):
            self.assertIn(key, entry, f"missing {key}")

        # Historical alias keys + the parameter-list shape are NOT
        # leaked. ``input_schema`` / ``output_schema`` are the
        # canonical wire form; ``input`` / ``output`` parameter
        # lists are TOML-authoring ergonomics only.
        for leaked_key in (
            "name", "required_params", "optional_params",
            "category", "error_codes",
            "input", "output",          # parameter-list shape stays internal
            "handler_type",             # superseded by handler: {type: ...}
        ):
            self.assertNotIn(leaked_key, entry, f"leaked {leaked_key}")

    def test_manifest_input_schema_is_json_schema_document(self):
        # The wire shape exposes a JSON Schema document, not the
        # parameter-list authoring shape. Operators author
        # parameter lists in TOML; the registry projects to JSON
        # Schema for the manifest.
        reg = EndpointRegistry()
        reg.register(_spec())
        entry = reg.render_manifest_section()[0]
        schema = entry["input_schema"]
        self.assertEqual(schema["type"], "object")
        self.assertIn("properties", schema)
        self.assertIn("guest_id", schema["properties"])
        # Required-input names go into the required array.
        self.assertIn("guest_id", schema.get("required", []))
        # additionalProperties: False so typo'd inputs surface.
        self.assertFalse(schema.get("additionalProperties", True))

    def test_input_schema_distinguishes_required_and_optional(self):
        reg = EndpointRegistry()
        reg.register(_spec(
            required_params=[
                ParamSpec(name="guest_id", type="string",
                          description="guest id"),
            ],
            optional_params=[
                ParamSpec(name="notes", type="string",
                          description="free-form notes"),
            ],
        ))
        schema = reg.render_manifest_section()[0]["input_schema"]
        # Both fields appear in properties.
        self.assertIn("guest_id", schema["properties"])
        self.assertIn("notes", schema["properties"])
        # Only the required ones appear in the required array.
        self.assertEqual(schema.get("required"), ["guest_id"])

    def test_handler_block_surfaces_type_only(self):
        # The manifest exposes ``handler: {type: ...}`` so agents
        # can reason about expected latency / behavior; the
        # underlying ``reference`` is implementation detail and
        # stays private. A future-proof object shape — additional
        # public per-binding metadata can layer in without breaking
        # readers.
        reg = EndpointRegistry()
        reg.register(_spec())
        entry = reg.render_manifest_section()[0]
        self.assertEqual(entry["handler"], {"type": "registered_function"})
        self.assertNotIn("handler_type", entry)


# ===========================================================================
# Thread safety: parallel registrations don't corrupt state.
# ===========================================================================


class ThreadSafetyTests(unittest.TestCase):

    def test_parallel_register_no_lost_writes(self):
        reg = EndpointRegistry()

        # We register at distinct paths so no DuplicateEndpointError
        # would be raised; the assertion is that all 50 registrations
        # land in the map without races.
        N = 50
        errors: list = []

        def worker(i: int) -> None:
            try:
                reg.register(_spec(name="QUERY", path=f"/q{i}",
                                   semantic=_semantic(
                                       capability="retrieval",
                                       impact="informational",
                                       is_idempotent=True)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(errors, [])
        self.assertEqual(reg.count(), N)
        self.assertEqual(len(reg.all_endpoints()), N)

    def test_parallel_duplicate_register_only_one_wins(self):
        reg = EndpointRegistry()
        first_seen = []
        duplicate_count = [0]
        lock = threading.Lock()

        def worker() -> None:
            try:
                reg.register(_spec())
                with lock:
                    first_seen.append(True)
            except DuplicateEndpointError:
                with lock:
                    duplicate_count[0] += 1

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()

        # Exactly one register call won; the rest got DuplicateEndpointError.
        self.assertEqual(len(first_seen), 1)
        self.assertEqual(duplicate_count[0], 19)
        self.assertEqual(reg.count(), 1)


if __name__ == "__main__":
    unittest.main()
