"""
Tests for the Elemen Compose drawer's Python surface.

The drawer's UI is exercised manually; its Python contract has two
testable layers:

  1. ``client.amg.composer.validate_partial`` — the partial-validation
     function that drives live form feedback.
  2. ``client.elemen.bridge.Api`` — the methods the JS module calls
     (``validate_compose``, ``get_substitution_catalog``,
     ``save_method_yaml``, ``export_library``, ``import_library``).

UI-level interaction (clicks, keyboard shortcuts, DOM rendering) is
out of scope; manual smoke-testing covers it.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.amg.composer import validate_partial
from client.elemen import bridge as elemen_bridge
from client.elemen.bridge import Api


# ===========================================================================
# validate_partial
# ===========================================================================


class ValidatePartialTests(unittest.TestCase):

    def test_empty_draft_is_valid_with_all_untouched(self):
        out = validate_partial({})
        self.assertTrue(out["valid"])
        self.assertEqual(out["errors"], {})
        self.assertEqual(out["warnings"], {})
        for section in ("identity", "semantic", "parameters",
                        "authority", "substitution"):
            self.assertEqual(out["completion"][section], "untouched")

    def test_stoplist_name_is_rejected(self):
        out = validate_partial({"name": "STATUS"})
        self.assertFalse(out["valid"])
        self.assertIn("name", out["errors"])
        self.assertIn("stoplist", out["errors"]["name"].lower())

    def test_lowercase_name_is_rejected(self):
        out = validate_partial({"name": "evaluate"})
        self.assertIn("name", out["errors"])
        # Suggestion to uppercase appears in the error text.
        self.assertIn("EVALUATE", out["errors"]["name"])

    def test_http_method_name_is_rejected(self):
        out = validate_partial({"name": "GET"})
        self.assertIn("name", out["errors"])
        self.assertIn("HTTP", out["errors"]["name"])

    def test_only_populated_fields_produce_errors(self):
        # No name, no semantic block — nothing should error. The user
        # has not yet had a chance to fill these.
        out = validate_partial({"description": "running a check on something here"})
        self.assertNotIn("name", out["errors"])
        self.assertNotIn("semantic.intent", out["errors"])
        self.assertEqual(out["completion"]["identity"], "partial")

    def test_irreversible_low_confidence_warns_not_errors(self):
        # Confidence-floor check is a soft warning, not a hard error —
        # the spec is still composable.
        out = validate_partial({
            "name": "PURGE",
            "namespace": "acme-store",
            "semantic": {
                "impact_tier": "irreversible",
                "confidence_guidance": 0.5,
            },
        })
        self.assertIn("semantic.confidence_guidance", out["warnings"])
        self.assertNotIn("semantic.confidence_guidance", out["errors"])

    def test_object_param_without_schema_is_rejected(self):
        out = validate_partial({
            "required_params": [
                {"name": "input", "type": "object", "description": "data"},
            ],
        })
        self.assertIn("required_params[0].schema", out["errors"])

    def test_completion_complete_when_all_section_fields_present(self):
        draft = {
            "name": "RECONCILE",
            "description": "reconcile a ledger account against the backing transactions",
            "semantic": {
                "intent": "reconciles transactions for an account window",
                "actor": "agent",
                "outcome": "a reconciliation summary is returned to the caller",
                "capability": "transaction",
                "impact_tier": "reversible",
                "confidence_guidance": 0.9,
                "is_idempotent": False,
            },
        }
        out = validate_partial(draft)
        self.assertEqual(out["completion"]["identity"], "complete")
        self.assertEqual(out["completion"]["semantic"], "complete")

    def test_namespace_pattern_warning(self):
        out = validate_partial({
            "name": "RECONCILE",
            "semantic": {"intent": "reconciles a ledger window for the caller"},
            "namespace": "Acme Finance",
        })
        self.assertIn("namespace", out["warnings"])

    def test_description_matches_intent_warning(self):
        text = "reconciles transactions for an account window"
        out = validate_partial({
            "name": "RECONCILE",
            "description": text,
            "semantic": {"intent": text, "actor": "agent",
                         "outcome": "a reconciliation summary is returned"},
        })
        self.assertIn("description", out["warnings"])


# ===========================================================================
# Api.validate_compose, get_substitution_catalog
# ===========================================================================


class ApiValidationSurfaceTests(unittest.TestCase):

    def setUp(self):
        self.api = Api()

    def test_validate_compose_passes_through(self):
        out = self.api.validate_compose({"name": "STATUS"})
        self.assertFalse(out["valid"])
        self.assertIn("name", out["errors"])

    def test_validate_compose_handles_non_dict(self):
        # JS could conceivably hand us a non-object during a half-baked
        # call; surface a sensible fallback rather than crash.
        out = self.api.validate_compose("not a dict")
        self.assertTrue(out["valid"])
        self.assertEqual(out["errors"], {})

    def test_get_substitution_catalog_structure(self):
        cat = self.api.get_substitution_catalog()
        self.assertIsInstance(cat, list)
        self.assertGreater(len(cat), 0)
        for entry in cat:
            self.assertIn("name", entry)
            self.assertIn("members", entry)
            self.assertIsInstance(entry["members"], list)
            for m in entry["members"]:
                self.assertEqual(m, m.upper())  # canonical case


# ===========================================================================
# Api.save_method_yaml + export_library + import_library (native dialogs
# monkeypatched).
# ===========================================================================


class ApiFileSurfaceTests(unittest.TestCase):

    def setUp(self):
        self.api = Api()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_method_yaml_writes_file(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("pyyaml not installed")
        target = self.tmp_path / "evaluate.method.yaml"
        spec = {
            "name": "EVALUATE",
            "semantic": {"intent": "evaluates input",
                         "actor": "agent",
                         "outcome": "an assessment is returned"},
            "required_params": [{"name": "input", "type": "string",
                                  "description": "the data"}],
        }
        with mock.patch.object(
            elemen_bridge, "_open_save_dialog",
            return_value=str(target),
        ):
            saved = self.api.save_method_yaml(spec, "evaluate.method.yaml")
        self.assertEqual(saved, str(target))
        self.assertTrue(target.exists())
        text = target.read_text(encoding="utf-8")
        self.assertIn("EVALUATE", text)
        self.assertIn("intent:", text)

    def test_save_method_yaml_cancelled_returns_empty(self):
        with mock.patch.object(
            elemen_bridge, "_open_save_dialog", return_value="",
        ):
            saved = self.api.save_method_yaml({"name": "X"}, "x.yaml")
        self.assertEqual(saved, "")

    def test_export_then_import_library_round_trip(self):
        target = self.tmp_path / "library.json"
        library = {
            "version": 1,
            "entries": [
                {"id": "lib_a", "name": "ALPHA", "spec": {}, "status": "draft",
                 "saved_at": "2026-05-08T00:00:00Z"},
                {"id": "lib_b", "name": "BETA",  "spec": {}, "status": "accepted",
                 "saved_at": "2026-05-09T00:00:00Z"},
            ],
        }
        with mock.patch.object(
            elemen_bridge, "_open_save_dialog", return_value=str(target),
        ):
            saved = self.api.export_library(library)
        self.assertEqual(saved, str(target))
        self.assertTrue(target.exists())

        with mock.patch.object(
            elemen_bridge, "_open_open_dialog", return_value=str(target),
        ):
            imported = self.api.import_library()
        self.assertEqual(imported["entries"][0]["name"], "ALPHA")
        self.assertEqual(len(imported["entries"]), 2)

    def test_import_library_handles_cancel(self):
        with mock.patch.object(
            elemen_bridge, "_open_open_dialog", return_value="",
        ):
            data = self.api.import_library()
        self.assertEqual(data, {})

    def test_import_library_handles_malformed_json(self):
        bad = self.tmp_path / "bad.json"
        bad.write_text("this is not json", encoding="utf-8")
        with mock.patch.object(
            elemen_bridge, "_open_open_dialog", return_value=str(bad),
        ):
            data = self.api.import_library()
        self.assertEqual(data, {})


if __name__ == "__main__":
    unittest.main()
