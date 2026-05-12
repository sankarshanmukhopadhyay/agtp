"""
Tests for :func:`server.endpoint_loader.load_endpoints`.

Coverage:

  * Loading a directory of valid TOML files; every spec parsed,
    no errors.
  * Malformed TOML: parse-shaped LoadError.
  * Validation failures (missing fields, bad shapes): validation
    LoadError with the same ``detail`` tag the registry would
    raise.
  * Empty directory: empty specs, no errors.
  * Nonexistent / non-directory path: io error.
  * Mixed valid + invalid files: both lists populated, valid
    specs survive.
  * The shipped samples in repo's ``endpoints/`` directory all
    parse cleanly (regression guard against the documentation
    drifting out of sync with the loader).
"""

from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.endpoint_loader import LoadError, load_endpoints


REPO_ROOT = Path(__file__).resolve().parent.parent


VALID_BOOK = textwrap.dedent("""
    [endpoint]
    method = "BOOK"
    path = "/room"
    description = "Books a room."
    namespace = "reservations"

    [endpoint.semantic]
    intent = "Reserve a room for the named guest at the named property."
    actor = "agent"
    outcome = "A confirmed reservation_id is returned for the guest."
    capability = "transaction"
    confidence = 0.85
    impact = "irreversible"
    is_idempotent = false

    [[endpoint.input.required]]
    name = "guest_id"
    type = "string"
    description = "Booking guest's id."
    format = "uuid"

    [[endpoint.input.optional]]
    name = "notes"
    type = "string"
    description = "Front-desk notes."

    [[endpoint.output]]
    name = "reservation_id"
    type = "string"
    description = "Server-assigned handle."

    [endpoint.errors]
    list = ["room_unavailable", "invalid_dates"]

    [endpoint.handler]
    type = "registered_function"
    reference = "staybeta.handlers.book_room"
""").strip()


VALID_QUERY = textwrap.dedent("""
    [endpoint]
    method = "QUERY"
    path = "/catalog"
    description = "Catalog query."

    [endpoint.semantic]
    intent = "Surface the catalog of items the server is willing to sell."
    actor = "agent"
    outcome = "An array of item summaries is returned."
    capability = "retrieval"
    confidence = 0.95
    impact = "informational"
    is_idempotent = true

    [[endpoint.output]]
    name = "items"
    type = "array"
    description = "Catalog item summaries."

    [endpoint.handler]
    type = "registered_function"
    reference = "server.methods.handle_query"
""").strip()


# ===========================================================================
# Happy path.
# ===========================================================================


class HappyPathTests(unittest.TestCase):

    def test_load_directory_with_two_valid_files(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "a.toml").write_text(VALID_BOOK, encoding="utf-8")
            (tdp / "b.toml").write_text(VALID_QUERY, encoding="utf-8")
            specs, errors = load_endpoints(tdp)
        self.assertEqual(errors, [])
        self.assertEqual({s.name for s in specs}, {"BOOK", "QUERY"})
        self.assertEqual({s.path for s in specs}, {"/room", "/catalog"})

    def test_load_returns_specs_in_sorted_filename_order(self):
        # The loader processes files in sorted order so the output
        # is stable across platforms / filesystems.
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "z.toml").write_text(VALID_QUERY, encoding="utf-8")
            (tdp / "a.toml").write_text(VALID_BOOK, encoding="utf-8")
            specs, errors = load_endpoints(tdp)
        self.assertEqual(errors, [])
        self.assertEqual([s.name for s in specs], ["BOOK", "QUERY"])

    def test_loaded_spec_carries_full_contract(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "book.toml").write_text(VALID_BOOK, encoding="utf-8")
            specs, _ = load_endpoints(tdp)
        spec = specs[0]
        self.assertEqual(spec.path, "/room")
        self.assertEqual(spec.namespace, "reservations")
        self.assertEqual(spec.required_params[0].name, "guest_id")
        self.assertEqual(spec.required_params[0].format, "uuid")
        self.assertEqual(spec.errors,
                         ["room_unavailable", "invalid_dates"])
        self.assertEqual(spec.handler.type, "registered_function")
        self.assertEqual(spec.semantic.capability, "transaction")

    def test_non_toml_files_ignored(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "a.toml").write_text(VALID_BOOK, encoding="utf-8")
            # README, JSON, YAML siblings should not produce errors.
            (tdp / "README.md").write_text("ignore me", encoding="utf-8")
            (tdp / "schema.json").write_text("{}", encoding="utf-8")
            specs, errors = load_endpoints(tdp)
        self.assertEqual(errors, [])
        self.assertEqual(len(specs), 1)


# ===========================================================================
# Empty / missing directories.
# ===========================================================================


class DirectoryEdgeCaseTests(unittest.TestCase):

    def test_empty_directory_returns_no_specs_no_errors(self):
        with TemporaryDirectory() as td:
            specs, errors = load_endpoints(td)
        self.assertEqual(specs, [])
        self.assertEqual(errors, [])

    def test_nonexistent_directory_returns_io_error(self):
        # Use a path that's guaranteed not to exist.
        specs, errors = load_endpoints("/no/such/directory/please")
        self.assertEqual(specs, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].error_type, "io")

    def test_path_pointing_at_a_file_returns_io_error(self):
        with TemporaryDirectory() as td:
            fp = Path(td) / "looks-like-a-dir.toml"
            fp.write_text(VALID_BOOK, encoding="utf-8")
            specs, errors = load_endpoints(fp)
        self.assertEqual(specs, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].error_type, "io")


# ===========================================================================
# Parse failures.
# ===========================================================================


class ParseFailureTests(unittest.TestCase):

    def test_malformed_toml_returns_parse_error(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "broken.toml").write_text(
                "this is = not = valid = toml\n", encoding="utf-8",
            )
            specs, errors = load_endpoints(tdp)
        self.assertEqual(specs, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].error_type, "parse")
        self.assertIn("broken.toml", errors[0].file_path)

    def test_missing_top_level_endpoint_table_returns_parse_error(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "wrong.toml").write_text(
                'method = "BOOK"\npath = "/room"\n', encoding="utf-8",
            )
            specs, errors = load_endpoints(tdp)
        self.assertEqual(specs, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].error_type, "parse")
        self.assertIn("[endpoint]", errors[0].message)


# ===========================================================================
# Validation failures (parsed but registry rules refused).
# ===========================================================================


class ValidationFailureTests(unittest.TestCase):

    def test_missing_path_yields_validation_error(self):
        body = textwrap.dedent("""
            [endpoint]
            method = "BOOK"
            description = "no path"

            [endpoint.semantic]
            intent = "..."
            actor = "agent"
            outcome = "..."
            capability = "transaction"
            confidence = 0.85
            impact = "irreversible"
            is_idempotent = false

            [endpoint.handler]
            type = "registered_function"
            reference = "x.y.z"
        """).strip()
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "x.toml").write_text(body, encoding="utf-8")
            specs, errors = load_endpoints(tdp)
        self.assertEqual(specs, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].error_type, "validation")
        self.assertEqual(errors[0].detail, "path-missing")
        # The partial spec is attached so callers can render the
        # method / description in error UIs.
        self.assertIsNotNone(errors[0].spec)
        self.assertEqual(errors[0].spec.name, "BOOK")

    def test_unknown_verb_yields_validation_error(self):
        body = VALID_BOOK.replace('method = "BOOK"', 'method = "FROBNICATE"')
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "f.toml").write_text(body, encoding="utf-8")
            _, errors = load_endpoints(tdp)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].error_type, "validation")
        self.assertEqual(errors[0].detail, "verb-not-in-catalog")

    def test_path_with_verb_token_yields_validation_error(self):
        body = VALID_BOOK.replace('path = "/room"', 'path = "/get/room"')
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "vp.toml").write_text(body, encoding="utf-8")
            _, errors = load_endpoints(tdp)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].error_type, "validation")
        self.assertTrue(errors[0].detail.startswith("path-grammar:"))

    def test_missing_semantic_field_yields_validation_error(self):
        body = VALID_BOOK.replace(
            'capability = "transaction"\n', "",
        )
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "ms.toml").write_text(body, encoding="utf-8")
            _, errors = load_endpoints(tdp)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].error_type, "validation")
        self.assertEqual(
            errors[0].detail, "semantic-missing-field:capability",
        )

    def test_missing_handler_yields_validation_error(self):
        # Drop the [endpoint.handler] block.
        body = VALID_BOOK.split("[endpoint.handler]")[0].rstrip()
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "mh.toml").write_text(body, encoding="utf-8")
            _, errors = load_endpoints(tdp)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].error_type, "validation")
        self.assertEqual(errors[0].detail, "handler-missing")

    def test_handler_type_invalid_yields_validation_error(self):
        body = VALID_BOOK.replace(
            'type = "registered_function"',
            'type = "lambda"',
        )
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "ht.toml").write_text(body, encoding="utf-8")
            _, errors = load_endpoints(tdp)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].error_type, "validation")
        self.assertEqual(errors[0].detail, "handler-bad-type")


# ===========================================================================
# Mixed: some files good, some bad.
# ===========================================================================


class MixedTests(unittest.TestCase):

    def test_valid_specs_survive_alongside_errors(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "good.toml").write_text(VALID_BOOK, encoding="utf-8")
            (tdp / "broken.toml").write_text(
                "= this isn't toml =", encoding="utf-8",
            )
            (tdp / "bad-verb.toml").write_text(
                VALID_BOOK.replace('"BOOK"', '"FROBNICATE"'),
                encoding="utf-8",
            )
            specs, errors = load_endpoints(tdp)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].name, "BOOK")
        self.assertEqual(len(errors), 2)
        types = {e.error_type for e in errors}
        self.assertEqual(types, {"parse", "validation"})


# ===========================================================================
# Regression: shipped samples must always parse cleanly.
# ===========================================================================


class ShippedSamplesTests(unittest.TestCase):

    def test_repo_endpoints_directory_loads_without_errors(self):
        # Documentation-grade samples in repo's ``endpoints/`` —
        # if they ever drift out of sync with the loader, this
        # regression catches it before anyone copy-pastes a broken
        # template.
        endpoints_dir = REPO_ROOT / "endpoints"
        specs, errors = load_endpoints(endpoints_dir)
        self.assertEqual(errors, [], errors)
        names = {s.name for s in specs}
        # Phase 1 shipped 3 samples (QUERY, BOOK, AUDIT). Phase 4
        # adds FETCH (the external_service wrap-and-expose sample).
        self.assertEqual(names, {"QUERY", "BOOK", "AUDIT", "FETCH"})


if __name__ == "__main__":
    unittest.main()
