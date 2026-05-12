"""
§9 handler binding field-rename tests (agtp-api §9).

Covers:

  * HandlerBinding's §9 type-specific reference fields
    (``function`` / ``recipe`` / ``url``) round-trip cleanly
    through ``to_dict`` / ``from_dict``.
  * The pre-§9 ``reference`` / ``input_map`` / ``output_map``
    init kwargs continue to construct a usable binding; the
    dataclass routes them to the new fields in __post_init__.
  * TOML loader accepts both old and new key names, emits a
    DeprecationWarning when the legacy form is used, and
    populates the new fields.
  * Manifest emit hides the handler's reference value entirely —
    only ``{type}`` rides the wire (§3 decision preserved under
    §9).
  * Registry validator's error path uses the new field names in
    error messages.

Pre-§9 handler-binding tests live in ``test_endpoint_spec.py``;
this module adds the §9-specific coverage.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.endpoint import HandlerBinding, ParamSpec, SemanticBlock
from server.endpoint_loader import load_endpoints
from server.endpoint_registry import (
    EndpointRegistry, InvalidEndpointError,
)


def _semantic() -> SemanticBlock:
    return SemanticBlock(
        intent="A test endpoint that exists to exercise §9 renames.",
        actor="agent",
        outcome="A test response is returned to verify §9 routing.",
        capability="retrieval",
        confidence=0.9,
        impact="informational",
        is_idempotent=True,
    )


# ===========================================================================
# Dataclass: type-specific fields + back-compat routing.
# ===========================================================================


class HandlerBindingFieldNamesTests(unittest.TestCase):

    def test_registered_function_uses_function_field(self):
        h = HandlerBinding(type="registered_function", function="m.fn")
        self.assertEqual(h.function, "m.fn")
        # Other type-specific slots stay empty.
        self.assertIsNone(h.recipe)
        self.assertIsNone(h.url)

    def test_composition_uses_recipe_field(self):
        h = HandlerBinding(type="composition", recipe="my-recipe")
        self.assertEqual(h.recipe, "my-recipe")
        self.assertIsNone(h.function)
        self.assertIsNone(h.url)

    def test_external_service_uses_url_field(self):
        h = HandlerBinding(
            type="external_service",
            url="https://upstream.example",
            method="GET",
        )
        self.assertEqual(h.url, "https://upstream.example")
        self.assertIsNone(h.function)
        self.assertIsNone(h.recipe)

    def test_external_service_input_output_transforms(self):
        h = HandlerBinding(
            type="external_service",
            url="https://x",
            method="POST",
            input_transform={"a": "b"},
            output_transform={"c": "d"},
        )
        self.assertEqual(h.input_transform, {"a": "b"})
        self.assertEqual(h.output_transform, {"c": "d"})


class HandlerBindingBackCompatRoutingTests(unittest.TestCase):
    """The dataclass accepts pre-§9 init kwargs and routes them."""

    def test_reference_kwarg_routes_to_function_for_registered(self):
        h = HandlerBinding(
            type="registered_function", reference="m.fn",
        )
        self.assertEqual(h.function, "m.fn")

    def test_reference_kwarg_routes_to_recipe_for_composition(self):
        h = HandlerBinding(type="composition", reference="r")
        self.assertEqual(h.recipe, "r")

    def test_reference_kwarg_routes_to_url_for_external_service(self):
        h = HandlerBinding(
            type="external_service", reference="https://x", method="GET",
        )
        self.assertEqual(h.url, "https://x")

    def test_input_map_kwarg_routes_to_input_transform(self):
        h = HandlerBinding(
            type="external_service", url="https://x", method="GET",
            input_map={"agtp": "http"},
        )
        self.assertEqual(h.input_transform, {"agtp": "http"})

    def test_output_map_kwarg_routes_to_output_transform(self):
        h = HandlerBinding(
            type="external_service", url="https://x", method="GET",
            output_map={"agtp": "http"},
        )
        self.assertEqual(h.output_transform, {"agtp": "http"})

    def test_new_name_wins_when_both_supplied(self):
        # __post_init__ only fills the new slot when it's empty,
        # so a caller that passes both gets the new value.
        h = HandlerBinding(
            type="registered_function",
            function="new.fn",
            reference="legacy.fn",
        )
        self.assertEqual(h.function, "new.fn")


class HandlerBindingSerializationTests(unittest.TestCase):

    def test_to_dict_emits_function_not_reference(self):
        h = HandlerBinding(type="registered_function", function="m.fn")
        d = h.to_dict()
        self.assertEqual(d, {"type": "registered_function", "function": "m.fn"})
        self.assertNotIn("reference", d)

    def test_to_dict_emits_recipe_not_reference(self):
        h = HandlerBinding(type="composition", recipe="r")
        d = h.to_dict()
        self.assertEqual(d, {"type": "composition", "recipe": "r"})
        self.assertNotIn("reference", d)

    def test_to_dict_emits_url_not_reference(self):
        h = HandlerBinding(
            type="external_service", url="https://x", method="POST",
            input_transform={"a": "b"},
        )
        d = h.to_dict()
        self.assertEqual(d["type"], "external_service")
        self.assertEqual(d["url"], "https://x")
        self.assertEqual(d["input_transform"], {"a": "b"})
        self.assertNotIn("reference", d)
        self.assertNotIn("input_map", d)

    def test_from_dict_accepts_legacy_reference(self):
        h = HandlerBinding.from_dict({
            "type": "registered_function",
            "reference": "legacy.fn",
        })
        self.assertEqual(h.function, "legacy.fn")
        # Round-trip emits the new name.
        self.assertEqual(
            h.to_dict(),
            {"type": "registered_function", "function": "legacy.fn"},
        )

    def test_from_dict_accepts_legacy_input_map(self):
        h = HandlerBinding.from_dict({
            "type": "external_service",
            "url": "https://x",
            "method": "POST",
            "input_map": {"agtp": "http"},
            "output_map": {"agtp": "http"},
        })
        self.assertEqual(h.input_transform, {"agtp": "http"})
        self.assertEqual(h.output_transform, {"agtp": "http"})
        d = h.to_dict()
        self.assertIn("input_transform", d)
        self.assertIn("output_transform", d)
        self.assertNotIn("input_map", d)


# ===========================================================================
# TOML loader: legacy + new keys, deprecation warnings.
# ===========================================================================


_VALID_TOML_REGISTERED_NEW = """
[endpoint]
method = "QUERY"
path = "/catalog"
description = "Test endpoint."

[endpoint.semantic]
intent = "A test endpoint that exists to exercise §9 renames."
actor = "agent"
outcome = "A test response is returned to verify §9 routing."
capability = "retrieval"
confidence = 0.9
impact = "informational"
is_idempotent = true

[[endpoint.input.required]]
name = "intent"
type = "string"
description = "Intent string."

[[endpoint.output]]
name = "result"
type = "string"
description = "Result string."

[endpoint.handler]
type = "registered_function"
function = "samples.handlers.query_catalog"
"""

_VALID_TOML_REGISTERED_LEGACY = _VALID_TOML_REGISTERED_NEW.replace(
    'function = "samples.handlers.query_catalog"',
    'reference = "samples.handlers.query_catalog"',
)

_VALID_TOML_EXTERNAL_LEGACY = """
[endpoint]
method = "FETCH"
path = "/article/{id}"
description = "Wraps an external service."

[endpoint.semantic]
intent = "Fetch an article from the upstream service."
actor = "agent"
outcome = "The article fields are returned in AGTP shape."
capability = "retrieval"
confidence = 0.9
impact = "informational"
is_idempotent = true

[[endpoint.input.required]]
name = "id"
type = "integer"
description = "Article id."

[[endpoint.output]]
name = "title"
type = "string"
description = "Title."

[endpoint.errors]
list = ["upstream_error"]

[endpoint.handler]
type = "external_service"
reference = "https://upstream.example/api"
method = "GET"

[endpoint.handler.input_map]
agtp_field = "http_field"

[endpoint.handler.output_map]
agtp_out = "http_out"
"""


class EndpointLoaderBackCompatTests(unittest.TestCase):

    def _load_one(self, contents: str, *, name: str = "x.toml"):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / name
            path.write_text(contents, encoding="utf-8")
            return load_endpoints(Path(td))

    def test_new_field_name_loads_cleanly(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            specs, errors = self._load_one(_VALID_TOML_REGISTERED_NEW)
        self.assertEqual(errors, [])
        self.assertEqual(len(specs), 1)
        self.assertEqual(
            specs[0].handler.function,
            "samples.handlers.query_catalog",
        )
        # No deprecation warning for the new field name.
        dep_warnings = [
            w for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "handler." in str(w.message)
        ]
        self.assertEqual(dep_warnings, [])

    def test_legacy_reference_loads_with_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            specs, errors = self._load_one(_VALID_TOML_REGISTERED_LEGACY)
        # The endpoint loads successfully.
        self.assertEqual(errors, [])
        self.assertEqual(len(specs), 1)
        self.assertEqual(
            specs[0].handler.function,
            "samples.handlers.query_catalog",
        )
        # And a deprecation warning fires naming the offending field.
        dep_warnings = [
            w for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "handler.reference" in str(w.message)
        ]
        self.assertEqual(len(dep_warnings), 1)
        self.assertIn("handler.function", str(dep_warnings[0].message))

    def test_legacy_external_service_keys_load_with_warnings(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            specs, errors = self._load_one(_VALID_TOML_EXTERNAL_LEGACY)
        self.assertEqual(errors, [])
        self.assertEqual(len(specs), 1)
        h = specs[0].handler
        # Routed correctly.
        self.assertEqual(h.url, "https://upstream.example/api")
        self.assertEqual(h.input_transform, {"agtp_field": "http_field"})
        self.assertEqual(h.output_transform, {"agtp_out": "http_out"})
        # Three deprecation warnings: reference, input_map, output_map.
        dep_messages = [
            str(w.message) for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "handler." in str(w.message)
        ]
        self.assertEqual(len(dep_messages), 3)
        self.assertTrue(any("handler.reference" in m for m in dep_messages))
        self.assertTrue(any("handler.input_map" in m for m in dep_messages))
        self.assertTrue(any("handler.output_map" in m for m in dep_messages))


# ===========================================================================
# Manifest emit: reference value stays hidden.
# ===========================================================================


class ManifestHandlerProjectionTests(unittest.TestCase):
    """The manifest's ``handler`` block exposes only ``{type}`` —
    the function / recipe / url value stays internal to the server
    (§3 decision, preserved under §9)."""

    def _spec(self, handler: HandlerBinding) -> object:
        from core.endpoint import EndpointSpec
        return EndpointSpec(
            name="QUERY", path="/catalog",
            description="Test endpoint.",
            semantic=_semantic(),
            required_params=[
                ParamSpec(name="intent", type="string", description="x")
            ],
            output=[
                ParamSpec(name="result", type="string", description="x")
            ],
            handler=handler,
        )

    def _manifest_entry(self, handler: HandlerBinding) -> dict:
        reg = EndpointRegistry()
        reg.register(self._spec(handler), lambda ctx: None)
        section = reg.render_manifest_section()
        self.assertEqual(len(section), 1)
        return section[0]

    def test_registered_function_manifest_hides_function_value(self):
        entry = self._manifest_entry(HandlerBinding(
            type="registered_function",
            function="staybeta.handlers.book_room",
        ))
        self.assertEqual(entry["handler"], {"type": "registered_function"})
        self.assertNotIn("function", entry["handler"])
        self.assertNotIn("reference", entry["handler"])

    def test_composition_manifest_hides_recipe_value(self):
        entry = self._manifest_entry(HandlerBinding(
            type="composition", recipe="audit-recipe",
        ))
        self.assertEqual(entry["handler"], {"type": "composition"})
        self.assertNotIn("recipe", entry["handler"])

    def test_external_service_manifest_hides_url_value(self):
        entry = self._manifest_entry(HandlerBinding(
            type="external_service",
            url="https://upstream.example/api",
            method="GET",
        ))
        self.assertEqual(entry["handler"], {"type": "external_service"})
        self.assertNotIn("url", entry["handler"])
        # Implementation extras (method, headers, timeout) also hidden.
        self.assertNotIn("method", entry["handler"])
        self.assertNotIn("timeout_seconds", entry["handler"])


# ===========================================================================
# Validator: HTTPS enforcement and reference-field error messages.
# ===========================================================================


class ValidatorMessagesTests(unittest.TestCase):

    def _spec(self, handler: HandlerBinding, *, errors=None) -> object:
        from core.endpoint import EndpointSpec
        return EndpointSpec(
            name="QUERY", path="/x",
            description="x",
            semantic=_semantic(),
            required_params=[
                ParamSpec(name="intent", type="string", description="x")
            ],
            output=[
                ParamSpec(name="result", type="string", description="x")
            ],
            handler=handler,
            errors=list(errors or []),
        )

    def test_https_enforcement_on_url(self):
        reg = EndpointRegistry()
        bad = HandlerBinding(
            type="external_service",
            url="http://insecure.example",  # http, not https
            method="GET",
        )
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(self._spec(bad))
        self.assertEqual(ctx.exception.detail, "external-service-bad-scheme")
        # The error message mentions the new field name.
        self.assertIn("handler.url", str(ctx.exception))

    def test_empty_function_refused(self):
        reg = EndpointRegistry()
        bad = HandlerBinding(type="registered_function", function="")
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(self._spec(bad))
        self.assertEqual(ctx.exception.detail, "handler-empty-reference")
        self.assertIn("handler.function", str(ctx.exception))

    def test_empty_recipe_refused(self):
        reg = EndpointRegistry()
        bad = HandlerBinding(type="composition", recipe="")
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(self._spec(bad))
        self.assertEqual(ctx.exception.detail, "handler-empty-reference")
        self.assertIn("handler.recipe", str(ctx.exception))

    def test_empty_url_refused(self):
        reg = EndpointRegistry()
        bad = HandlerBinding(
            type="external_service", url="", method="GET",
        )
        with self.assertRaises(InvalidEndpointError) as ctx:
            reg.register(self._spec(bad))
        self.assertEqual(ctx.exception.detail, "handler-empty-reference")
        self.assertIn("handler.url", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
