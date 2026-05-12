"""
Tests for ``tools.openapi_import``.

Coverage strands:

  * **HTTP-method → AGTP-verb mapping** — table of synthetic
    operations covering the default mapping and the keyword
    overrides (POST cancel → CANCEL, POST book → BOOK, etc.) plus
    the GET single-resource vs collection heuristic.
  * **Path translation** — verb-in-path detection, trailing-slash
    normalization.
  * **Schema translation** — top-level ``object`` unrolls into
    ``input.required`` / ``input.optional``; bare-array bodies wrap
    under ``body``; ``oneOf`` / ``anyOf`` flag review-comments.
  * **Semantic-block heuristics** — defaults for GET / POST /
    DELETE; review-comments fire for impact-tier guesses.
  * **Petstore-shaped end-to-end** — convert a synthetic Petstore
    spec, verify expected number of operations, the keyword
    overrides apply where relevant, and every well-formed output
    passes the AGTP endpoint validator.
  * **Edge cases** — verb-in-path produces a review-comment, the
    converter still emits a TOML, validation surfaces the path
    grammar refusal.
  * **CLI** — ``main()`` returns the right exit code for happy /
    failure / strict scenarios.
  * **Spec loading** — JSON, YAML (skip when PyYAML missing),
    Swagger 2.0 refusal.
"""

from __future__ import annotations

import json
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.openapi_import import (  # noqa: E402
    Conversion,
    ConvertedOperation,
    OpenAPILoadError,
    convert_operation,
    convert_spec,
    derive_semantic_block,
    load_openapi_spec,
    main as cli_main,
    map_http_method_to_agtp_verb,
    translate_path,
    translate_schema_to_fields,
    validate_converted,
    write_conversion,
)


# ===========================================================================
# Synthetic specs.
# ===========================================================================


PETSTORE_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "servers": [{"url": "https://petstore.example.com/v1"}],
    "paths": {
        "/pets": {
            "get": {
                "summary": "List all pets",
                "operationId": "listPets",
                "tags": ["pets"],
                "parameters": [{
                    "name": "limit", "in": "query",
                    "schema": {"type": "integer"},
                    "description": "Page size",
                }],
                "responses": {
                    "200": {
                        "description": "A list of pets",
                        "content": {"application/json": {
                            "schema": {
                                "type": "array",
                                "items": {"type": "object"},
                            },
                        }},
                    },
                    "400": {"description": "Bad request"},
                },
            },
            "post": {
                "summary": "Create a pet",
                "operationId": "createPet",
                "tags": ["pets"],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["name"],
                            "properties": {
                                "name": {"type": "string"},
                                "tag": {"type": "string"},
                            },
                        },
                    }},
                },
                "responses": {
                    "201": {"description": "Pet created"},
                    "422": {"description": "Validation failed"},
                },
            },
        },
        "/pets/{petId}": {
            "get": {
                "summary": "Show a pet by id",
                "operationId": "showPetById",
                "parameters": [{
                    "name": "petId", "in": "path", "required": True,
                    "schema": {"type": "string"},
                }],
                "responses": {
                    "200": {"description": "A pet"},
                    "404": {"description": "Pet not found"},
                },
            },
            "delete": {
                "summary": "Delete a pet",
                "operationId": "deletePet",
                "parameters": [{
                    "name": "petId", "in": "path", "required": True,
                    "schema": {"type": "string"},
                }],
                "responses": {
                    "204": {"description": "Deleted"},
                    "404": {"description": "Pet not found"},
                },
            },
        },
        "/orders/{orderId}/cancel": {
            "post": {
                "summary": "Cancel an order",
                "parameters": [{
                    "name": "orderId", "in": "path", "required": True,
                    "schema": {"type": "string"},
                }],
                "responses": {
                    "200": {"description": "Order cancelled"},
                },
            },
        },
        "/rooms/{roomId}/book": {
            "post": {
                "summary": "Book a room",
                "parameters": [{
                    "name": "roomId", "in": "path", "required": True,
                    "schema": {"type": "string"},
                }],
                "responses": {
                    "201": {"description": "Reservation created"},
                },
            },
        },
    },
}


# A spec where the path's hyphen-separated token contains a verb
# (e.g., 'get-history'). The strict path-grammar normalizer
# strips dashes so this does NOT fail validation, but the
# converter's secondary token check still surfaces a review-comment.
SPEC_WITH_HYPHEN_VERB_IN_PATH = {
    "openapi": "3.0.3",
    "info": {"title": "Hyphen-verb-in-path", "version": "1.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/users/{id}/get-history": {
            "get": {
                "summary": "Get a user's activity history",
                "parameters": [{
                    "name": "id", "in": "path", "required": True,
                    "schema": {"type": "string"},
                }],
                "responses": {"200": {"description": "history"}},
            },
        },
    },
}


# A spec where the path has a whole-segment verb (e.g.,
# '/users/list/{id}'). Both the converter's review-comment
# AND the strict path-grammar refusal fire here.
SPEC_WITH_HARD_VERB_IN_PATH = {
    "openapi": "3.0.3",
    "info": {"title": "Hard-verb-in-path", "version": "1.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/users/list/{id}": {
            "get": {
                "summary": "List one user's records",
                "parameters": [{
                    "name": "id", "in": "path", "required": True,
                    "schema": {"type": "string"},
                }],
                "responses": {"200": {"description": "records"}},
            },
        },
    },
}


# ===========================================================================
# HTTP method → AGTP verb mapping.
# ===========================================================================


class VerbMappingTests(unittest.TestCase):

    def test_default_mappings(self):
        # Default mapping when no operation context applies.
        self.assertEqual(
            map_http_method_to_agtp_verb("PUT", "/users/{id}").verb,
            "REPLACE",
        )
        self.assertEqual(
            map_http_method_to_agtp_verb("DELETE", "/users/{id}").verb,
            "REMOVE",
        )
        self.assertEqual(
            map_http_method_to_agtp_verb("PATCH", "/users/{id}").verb,
            "MODIFY",
        )

    def test_get_single_resource_maps_to_fetch(self):
        # Path ending in a parameter → single-resource FETCH.
        result = map_http_method_to_agtp_verb("GET", "/pets/{id}")
        self.assertEqual(result.verb, "FETCH")

    def test_get_collection_maps_to_list(self):
        # Path NOT ending in a parameter → collection LIST.
        result = map_http_method_to_agtp_verb("GET", "/pets")
        self.assertEqual(result.verb, "LIST")
        # The review-comment mentions QUERY / DISCOVER alternatives.
        self.assertTrue(any("QUERY" in c for c in result.review_comments))

    def test_post_cancel_maps_to_cancel(self):
        result = map_http_method_to_agtp_verb(
            "POST", "/orders/{id}/cancel",
        )
        self.assertEqual(result.verb, "CANCEL")

    def test_post_book_maps_to_book(self):
        result = map_http_method_to_agtp_verb(
            "POST", "/rooms/{id}/book",
        )
        self.assertEqual(result.verb, "BOOK")

    def test_post_purchase_via_summary(self):
        result = map_http_method_to_agtp_verb(
            "POST", "/checkout",
            operation={"summary": "Purchase the cart contents"},
        )
        self.assertEqual(result.verb, "PURCHASE")

    def test_post_default_creates_with_alternatives(self):
        result = map_http_method_to_agtp_verb("POST", "/widgets")
        self.assertEqual(result.verb, "CREATE")
        # The default mapping surfaces alternative AGTP verbs.
        self.assertTrue(any(
            "SUBMIT" in c or "REGISTER" in c
            for c in result.review_comments
        ))

    def test_post_register_via_path(self):
        result = map_http_method_to_agtp_verb(
            "POST", "/users/register",
        )
        self.assertEqual(result.verb, "REGISTER")

    def test_unknown_http_method_falls_back_with_warning(self):
        # ANNOUNCE isn't in the default mapping; the converter
        # falls back to FETCH and review-comments.
        result = map_http_method_to_agtp_verb("ANNOUNCE", "/x")
        self.assertEqual(result.verb, "FETCH")
        self.assertTrue(any(
            "default mapping" in c for c in result.review_comments
        ))

    def test_keyword_match_is_word_boundary(self):
        # 'reorder' should NOT match the 'order' keyword (the
        # converter would otherwise misclassify reorder POSTs).
        result = map_http_method_to_agtp_verb(
            "POST", "/items/{id}/reorder",
        )
        # Falls through to the default CREATE mapping.
        self.assertEqual(result.verb, "CREATE")


# ===========================================================================
# Path translation.
# ===========================================================================


class PathTranslationTests(unittest.TestCase):

    def test_passthrough_for_clean_path(self):
        path, comments = translate_path("/users/{id}")
        self.assertEqual(path, "/users/{id}")
        self.assertEqual(comments, [])

    def test_strips_trailing_slash(self):
        path, comments = translate_path("/users/")
        self.assertEqual(path, "/users")
        self.assertTrue(any("trailing slash" in c for c in comments))

    def test_root_path_keeps_slash(self):
        path, _ = translate_path("/")
        self.assertEqual(path, "/")

    def test_verb_in_path_flagged(self):
        path, comments = translate_path("/users/{id}/get-history")
        # The path is left as-is so the registry's 460 fires
        # loudly and the developer sees the validation failure.
        self.assertEqual(path, "/users/{id}/get-history")
        self.assertTrue(any(
            "AGTP verb" in c and "verbs belong in the method" in c.lower()
            for c in comments
        ))

    def test_empty_path_defaulted(self):
        path, comments = translate_path("")
        self.assertEqual(path, "/")
        self.assertTrue(comments)


# ===========================================================================
# Schema translation.
# ===========================================================================


class SchemaTranslationTests(unittest.TestCase):

    def test_object_schema_unrolls_into_required_and_optional(self):
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "the name"},
                "tag": {"type": "string"},
            },
        }
        required, optional, _ = translate_schema_to_fields(schema)
        self.assertEqual([f.name for f in required], ["name"])
        self.assertEqual([f.name for f in optional], ["tag"])
        self.assertEqual(required[0].description, "the name")

    def test_format_and_enum_propagate(self):
        schema = {
            "type": "object",
            "required": [],
            "properties": {
                "tier": {"type": "string", "enum": ["free", "pro"]},
                "when": {"type": "string", "format": "date"},
            },
        }
        _, optional, _ = translate_schema_to_fields(schema)
        by_name = {f.name: f for f in optional}
        self.assertEqual(by_name["tier"].enum, ["free", "pro"])
        self.assertEqual(by_name["when"].format, "date")

    def test_complex_property_keeps_inline_schema(self):
        schema = {
            "type": "object",
            "required": ["dates"],
            "properties": {
                "dates": {
                    "type": "object",
                    "required": ["check_in"],
                    "properties": {
                        "check_in": {"type": "string", "format": "date"},
                    },
                },
            },
        }
        required, _, _ = translate_schema_to_fields(schema)
        self.assertEqual(len(required), 1)
        self.assertEqual(required[0].type, "object")
        # The full nested schema rides under .schema so the
        # validator can enforce the inner shape.
        self.assertEqual(
            required[0].schema["properties"]["check_in"]["format"],
            "date",
        )

    def test_bare_array_body_wraps_under_body(self):
        schema = {"type": "array", "items": {"type": "string"}}
        required, optional, _ = translate_schema_to_fields(schema)
        self.assertEqual(len(required), 1)
        self.assertEqual(required[0].name, "body")
        self.assertEqual(required[0].type, "array")
        self.assertEqual(optional, [])

    def test_oneof_at_top_flags_review_comment(self):
        schema = {
            "oneOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}},
                {"type": "object", "properties": {"b": {"type": "integer"}}},
            ],
        }
        required, _, comments = translate_schema_to_fields(schema)
        self.assertEqual(len(required), 1)
        self.assertEqual(required[0].name, "body")
        self.assertTrue(any("oneOf" in c for c in comments))

    def test_non_dict_schema_returns_empty(self):
        self.assertEqual(translate_schema_to_fields(None), ([], [], []))
        self.assertEqual(translate_schema_to_fields("string")[:2], ([], []))


# ===========================================================================
# Semantic-block heuristics.
# ===========================================================================


class SemanticHeuristicTests(unittest.TestCase):

    def test_get_defaults_to_informational_idempotent(self):
        sem, _ = derive_semantic_block(
            "GET", "FETCH",
            {"summary": "Fetch a record from the upstream service."},
        )
        self.assertEqual(sem["impact"], "informational")
        self.assertTrue(sem["is_idempotent"])
        self.assertEqual(sem["actor"], "agent")

    def test_post_marks_irreversible_with_review_comment(self):
        sem, comments = derive_semantic_block(
            "POST", "CREATE",
            {"summary": "Create a record in the upstream service."},
        )
        self.assertEqual(sem["impact"], "irreversible")
        self.assertFalse(sem["is_idempotent"])
        self.assertTrue(any(
            "is_idempotent" in c.lower() for c in comments
        ))

    def test_intent_falls_back_to_description(self):
        sem, _ = derive_semantic_block(
            "GET", "FETCH",
            {"description": "Returns a record. Other notes follow."},
        )
        self.assertTrue(sem["intent"].startswith("Returns a record"))

    def test_capability_derived_from_verb_category(self):
        # BOOK is in the 'transaction' category in the AGTP catalog.
        sem, _ = derive_semantic_block(
            "POST", "BOOK",
            {"summary": "Book a room for the named guest."},
        )
        self.assertEqual(sem["capability"], "transaction")


# ===========================================================================
# End-to-end petstore conversion.
# ===========================================================================


class PetstoreEndToEndTests(unittest.TestCase):

    def test_converts_all_six_operations(self):
        # /pets (GET, POST), /pets/{petId} (GET, DELETE),
        # /orders/{orderId}/cancel (POST), /rooms/{roomId}/book (POST)
        # = 6 operations.
        conv = convert_spec(PETSTORE_SPEC)
        self.assertEqual(len(conv.operations), 6)

    def test_verb_mappings_match_keyword_overrides(self):
        conv = convert_spec(PETSTORE_SPEC)
        by_path = {(op.http_method, op.path): op.agtp_verb
                   for op in conv.operations}
        self.assertEqual(by_path[("GET", "/pets")], "LIST")
        self.assertEqual(by_path[("POST", "/pets")], "CREATE")
        self.assertEqual(by_path[("GET", "/pets/{petId}")], "FETCH")
        self.assertEqual(by_path[("DELETE", "/pets/{petId}")], "REMOVE")
        self.assertEqual(
            by_path[("POST", "/orders/{orderId}/cancel")], "CANCEL",
        )
        self.assertEqual(
            by_path[("POST", "/rooms/{roomId}/book")], "BOOK",
        )

    def test_handler_reference_uses_resolved_base_url(self):
        conv = convert_spec(PETSTORE_SPEC)
        list_pets = next(
            op for op in conv.operations
            if op.http_method == "GET" and op.path == "/pets"
        )
        self.assertIn("https://petstore.example.com/v1/pets",
                      list_pets.toml_body)

    def test_base_url_override_applied(self):
        conv = convert_spec(
            PETSTORE_SPEC,
            base_url="https://staging.petstore.example.com",
        )
        list_pets = next(
            op for op in conv.operations if op.http_method == "GET"
            and op.path == "/pets"
        )
        self.assertIn(
            "https://staging.petstore.example.com/pets",
            list_pets.toml_body,
        )

    def test_well_formed_operations_pass_validator(self):
        conv = convert_spec(PETSTORE_SPEC)
        validate_converted(conv)
        # The /orders/{orderId}/cancel and /rooms/{roomId}/book
        # paths each embed a recognized AGTP verb token at the
        # whole-segment level → both fail the strict path-grammar
        # validation. The 4 remaining operations pass.
        passes = [op for op in conv.operations if not op.validation_error]
        self.assertEqual(len(passes), 4)
        for op in passes:
            self.assertNotIn("/cancel", op.path)
            self.assertNotIn("/book", op.path)

    def test_cancel_in_path_fails_validation_with_grammar_detail(self):
        conv = convert_spec(PETSTORE_SPEC)
        validate_converted(conv)
        cancel_op = next(
            op for op in conv.operations
            if op.path == "/orders/{orderId}/cancel"
        )
        self.assertIsNotNone(cancel_op.validation_error)
        self.assertIn("path-grammar", cancel_op.validation_error)
        # The review-comment fired before validation, so the developer
        # has a head start.
        self.assertTrue(any(
            "AGTP verb" in c.lower() or "verb" in c.lower()
            for c in cancel_op.review_comments
        ))

    def test_review_comments_include_idempotency_guidance(self):
        conv = convert_spec(PETSTORE_SPEC)
        post_pets = next(
            op for op in conv.operations
            if op.http_method == "POST" and op.path == "/pets"
        )
        self.assertTrue(any(
            "is_idempotent" in c.lower()
            for c in post_pets.review_comments
        ))

    def test_error_map_renames_response_descriptions(self):
        conv = convert_spec(PETSTORE_SPEC)
        post_pets = next(
            op for op in conv.operations
            if op.http_method == "POST" and op.path == "/pets"
        )
        # 'Validation failed' → 'validation_failed' AGTP error code.
        self.assertIn("validation_failed", post_pets.toml_body)
        self.assertIn('"422" = "validation_failed"', post_pets.toml_body)


# ===========================================================================
# Verb-in-path edge case.
# ===========================================================================


class VerbInPathTests(unittest.TestCase):

    def test_hyphen_separated_verb_token_review_comment(self):
        # The strict path-grammar layer strips dashes (so
        # 'get-history' becomes 'GETHISTORY' — not a verb) and
        # accepts the path. The converter's secondary token-level
        # check catches the leak and emits a review-comment.
        conv = convert_spec(SPEC_WITH_HYPHEN_VERB_IN_PATH)
        op = conv.operations[0]
        self.assertTrue(any(
            "verb token" in c.lower() and "GET" in c
            for c in op.review_comments
        ))

    def test_hyphen_token_validation_passes(self):
        # The strict layer doesn't refuse hyphen-token verbs; only
        # the converter's review-comment fires. Validation passes
        # so the developer can ship the endpoint as-is if they
        # decide to.
        conv = convert_spec(SPEC_WITH_HYPHEN_VERB_IN_PATH)
        validate_converted(conv)
        op = conv.operations[0]
        self.assertIsNone(op.validation_error)

    def test_hard_verb_in_path_validation_fails_with_grammar_detail(self):
        # Whole-segment verb ('/users/list/{id}') trips the strict
        # path-grammar refusal at validation time.
        conv = convert_spec(SPEC_WITH_HARD_VERB_IN_PATH)
        validate_converted(conv)
        op = conv.operations[0]
        self.assertIsNotNone(op.validation_error)
        self.assertIn("path-grammar", op.validation_error)
        # And the review-comment fires too.
        self.assertTrue(any(
            "AGTP verb" in c
            for c in op.review_comments
        ))


# ===========================================================================
# Spec loading.
# ===========================================================================


class SpecLoadingTests(unittest.TestCase):

    def test_loads_json_spec(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "spec.json").write_text(
                json.dumps(PETSTORE_SPEC), encoding="utf-8",
            )
            spec = load_openapi_spec(tdp / "spec.json")
        self.assertEqual(spec["openapi"], "3.0.3")

    def test_swagger_2_0_refused_with_helpful_message(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "spec.json").write_text(
                json.dumps({"swagger": "2.0", "info": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(OpenAPILoadError) as ctx:
                load_openapi_spec(tdp / "spec.json")
        self.assertIn("OpenAPI 2.0", str(ctx.exception))
        self.assertIn("swagger2openapi", str(ctx.exception))

    def test_missing_openapi_field_refused(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "spec.json").write_text(
                json.dumps({"info": {"title": "x"}}),
                encoding="utf-8",
            )
            with self.assertRaises(OpenAPILoadError):
                load_openapi_spec(tdp / "spec.json")

    def test_unsupported_version_refused(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "spec.json").write_text(
                json.dumps({"openapi": "4.0.0", "info": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(OpenAPILoadError) as ctx:
                load_openapi_spec(tdp / "spec.json")
        self.assertIn("3.0", str(ctx.exception))

    def test_nonexistent_path_refused(self):
        with self.assertRaises(OpenAPILoadError):
            load_openapi_spec("/no/such/spec.json")

    def test_loads_yaml_spec_when_pyyaml_installed(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        yaml_text = textwrap.dedent("""
            openapi: 3.0.3
            info:
              title: Tiny
              version: '1.0'
            servers:
              - url: https://api.example.com
            paths:
              /things:
                get:
                  summary: List things
                  responses:
                    '200':
                      description: ok
        """).lstrip()
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "spec.yaml").write_text(yaml_text, encoding="utf-8")
            spec = load_openapi_spec(tdp / "spec.yaml")
        self.assertEqual(spec["info"]["title"], "Tiny")


# ===========================================================================
# CLI orchestration.
# ===========================================================================


class CLITests(unittest.TestCase):

    def _write_spec(self, td: Path, spec: dict) -> Path:
        path = td / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def test_cli_writes_files_and_returns_zero_on_clean_run(self):
        clean_spec = {
            "openapi": "3.0.3",
            "info": {"title": "x", "version": "1"},
            "servers": [{"url": "https://api.example.com"}],
            "paths": {
                "/widgets": {
                    "get": {
                        "summary": "List widgets",
                        "responses": {"200": {"description": "list"}},
                    },
                },
            },
        }
        with TemporaryDirectory() as td:
            tdp = Path(td)
            spec_path = self._write_spec(tdp, clean_spec)
            out_dir = tdp / "out"
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                rc = cli_main([
                    str(spec_path), "--output", str(out_dir),
                ])
            self.assertEqual(rc, 0)
            # The output directory now has at least one TOML file.
            self.assertTrue(any(out_dir.iterdir()))

    def test_cli_returns_one_when_validation_fails(self):
        # SPEC_WITH_HARD_VERB_IN_PATH trips the strict path-grammar
        # refusal, which the validator surfaces as a hard failure.
        with TemporaryDirectory() as td:
            tdp = Path(td)
            spec_path = self._write_spec(tdp, SPEC_WITH_HARD_VERB_IN_PATH)
            out_dir = tdp / "out"
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                rc = cli_main([
                    str(spec_path), "--output", str(out_dir),
                ])
            self.assertEqual(rc, 1)

    def test_cli_strict_returns_one_on_review_comment(self):
        # Even a clean conversion fires review-comments
        # (semantic-block defaults). --strict turns those into a
        # hard failure.
        clean_spec = {
            "openapi": "3.0.3",
            "info": {"title": "x", "version": "1"},
            "servers": [{"url": "https://api.example.com"}],
            "paths": {
                "/widgets": {
                    "get": {
                        "summary": "List widgets",
                        "responses": {"200": {"description": "list"}},
                    },
                },
            },
        }
        with TemporaryDirectory() as td:
            tdp = Path(td)
            spec_path = self._write_spec(tdp, clean_spec)
            out_dir = tdp / "out"
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                rc = cli_main([
                    str(spec_path), "--output", str(out_dir), "--strict",
                ])
            self.assertEqual(rc, 1)

    def test_cli_returns_two_on_load_error(self):
        with mock.patch("sys.stderr"):
            rc = cli_main(["/no/such/spec.json", "--output", "/tmp/x"])
        self.assertEqual(rc, 2)

    def test_cli_no_review_comments_strips_them_from_output(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            spec_path = self._write_spec(tdp, PETSTORE_SPEC)
            out_dir = tdp / "out"
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                cli_main([
                    str(spec_path), "--output", str(out_dir),
                    "--no-review-comments",
                ])
            files = list(out_dir.glob("*.toml"))
            self.assertGreater(len(files), 0)
            for f in files:
                text = f.read_text()
                # No '# REVIEW:' lines left; comments-stripped output
                # is purely TOML.
                self.assertNotIn("# REVIEW", text)


# ===========================================================================
# write_conversion / output structure.
# ===========================================================================


class WriteConversionTests(unittest.TestCase):

    def test_writes_one_file_per_operation(self):
        conv = convert_spec(PETSTORE_SPEC)
        with TemporaryDirectory() as td:
            tdp = Path(td) / "out"
            paths = write_conversion(conv, tdp)
            self.assertEqual(len(paths), 6)
            self.assertTrue(all(p.endswith(".toml") for p in paths))

    def test_filename_includes_verb_prefix(self):
        conv = convert_spec(PETSTORE_SPEC)
        list_pets = next(
            op for op in conv.operations
            if op.http_method == "GET" and op.path == "/pets"
        )
        self.assertTrue(list_pets.toml_filename.startswith("list_"))


if __name__ == "__main__":
    unittest.main()
