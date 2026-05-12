"""
Tests for ``tools.catalog_diff`` (the ``agtp-catalog-diff`` CLI).

Coverage:

  * Pure catalog diffs: added / removed / newly-deprecated.
  * Deployment-aware diffs: path-grammar conflicts (paths whose
    segments now collide with newly-added verbs), endpoint
    conflicts (TOMLs declaring removed verbs), recipe conflicts
    (steps naming removed verbs), [policies.methods] conflicts.
  * Exit codes match the documented contract:
      0 = no breakage / clean diff;
      1 = breaking changes detected in deployment context;
      2 = parse error in either catalog file.
  * --json flag produces parseable structured output.
  * Edge cases: empty deployment dir, missing files, malformed
    catalog.
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

from tools.catalog_diff import (
    CatalogDiff,
    CatalogDiffError,
    diff_catalogs,
    load_catalog,
    main as cli_main,
    render_text,
    scan_deployment,
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _catalog(version: str, methods: dict, *, legacy: dict = None) -> dict:
    return {
        "version": version,
        "embedded": [],
        "legacy": legacy or {},
        "categories": {},
        "methods": methods,
    }


_OLD = _catalog("1.0.0", {
    "RECONCILE": {"categories": ["transaction"], "description": "reconcile"},
    "AUDIT":     {"categories": ["analysis"],    "description": "audit"},
    "LEGACY_AUDIT": {"categories": ["analysis"], "description": "old"},
    "QUERY":     {"categories": ["mechanics"],   "description": "embedded"},
    "FETCH":     {"categories": ["retrieval"],   "description": "fetch"},
})

_NEW = _catalog("1.1.0", {
    "RECONCILE": {"categories": ["transaction"], "description": "reconcile"},
    "AUDIT":     {"categories": ["analysis"],    "description": "audit"},
    "AUDIT_LEGACY": {
        "categories": ["analysis"], "description": "use AUDIT",
        "deprecated_in": "1.1.0", "removed_in": "2.0.0",
        "successor": "AUDIT",
    },
    "FORECAST":  {"categories": ["analysis"],    "description": "new"},
    "QUERY":     {"categories": ["mechanics"],   "description": "embedded"},
    "FETCH":     {"categories": ["retrieval"],   "description": "fetch"},
})


def _write_catalog(path: Path, doc: dict) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


# ===========================================================================
# Pure catalog diff.
# ===========================================================================


class PureDiffTests(unittest.TestCase):

    def test_detects_added_and_removed(self):
        diff = diff_catalogs(_OLD, _NEW)
        self.assertEqual(diff.removed, ["LEGACY_AUDIT"])
        # AUDIT_LEGACY and FORECAST are both new in NEW.
        self.assertEqual(sorted(diff.added), ["AUDIT_LEGACY", "FORECAST"])

    def test_no_change_returns_empty_diff(self):
        diff = diff_catalogs(_OLD, _OLD)
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.removed, [])
        self.assertEqual(diff.newly_deprecated, [])

    def test_newly_deprecated_when_old_active_new_deprecated(self):
        # Mark RECONCILE deprecated in NEW; old had no deprecation.
        new_with_dep = json.loads(json.dumps(_NEW))
        new_with_dep["methods"]["RECONCILE"]["deprecated_in"] = "1.1.0"
        new_with_dep["methods"]["RECONCILE"]["successor"] = "AUDIT"
        diff = diff_catalogs(_OLD, new_with_dep)
        names = [d["name"] for d in diff.newly_deprecated]
        self.assertIn("RECONCILE", names)

    def test_already_deprecated_in_old_is_not_newly_deprecated(self):
        old_with_dep = json.loads(json.dumps(_OLD))
        old_with_dep["methods"]["RECONCILE"]["deprecated_in"] = "1.0.0"
        new_with_dep = json.loads(json.dumps(_NEW))
        new_with_dep["methods"]["RECONCILE"]["deprecated_in"] = "1.0.0"
        diff = diff_catalogs(old_with_dep, new_with_dep)
        names = [d["name"] for d in diff.newly_deprecated]
        self.assertNotIn("RECONCILE", names)

    def test_diff_records_versions(self):
        diff = diff_catalogs(_OLD, _NEW)
        self.assertEqual(diff.old_version, "1.0.0")
        self.assertEqual(diff.new_version, "1.1.0")

    def test_diff_to_dict_round_trip(self):
        diff = diff_catalogs(_OLD, _NEW)
        d = diff.to_dict()
        self.assertEqual(d["old_version"], "1.0.0")
        self.assertEqual(d["new_version"], "1.1.0")
        self.assertEqual(set(d["added"]), {"AUDIT_LEGACY", "FORECAST"})
        self.assertEqual(d["removed"], ["LEGACY_AUDIT"])

    def test_no_deployment_means_no_breakage(self):
        diff = diff_catalogs(_OLD, _NEW)
        self.assertFalse(diff.has_deployment_breakage)


# ===========================================================================
# Deployment scan.
# ===========================================================================


class DeploymentScanTests(unittest.TestCase):

    def test_endpoint_declares_removed_verb(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            ep = tdp / "endpoints"
            ep.mkdir()
            (ep / "audit.toml").write_text(
                '[endpoint]\nmethod = "LEGACY_AUDIT"\npath = "/audits"\n',
                encoding="utf-8",
            )
            diff = diff_catalogs(_OLD, _NEW)
            scan_deployment(diff, tdp, new_catalog=_NEW)
        self.assertEqual(len(diff.endpoint_conflicts), 1)
        self.assertEqual(
            diff.endpoint_conflicts[0]["method"], "LEGACY_AUDIT",
        )
        self.assertTrue(diff.has_deployment_breakage)

    def test_path_grammar_conflict_with_newly_added_verb(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            ep = tdp / "endpoints"
            ep.mkdir()
            # FORECAST is newly added; this path uses 'forecast'
            # as a segment which would now fail path-grammar.
            (ep / "fcst.toml").write_text(
                '[endpoint]\nmethod = "QUERY"\npath = "/forecast/{id}"\n',
                encoding="utf-8",
            )
            diff = diff_catalogs(_OLD, _NEW)
            scan_deployment(diff, tdp, new_catalog=_NEW)
        self.assertEqual(len(diff.path_grammar_conflicts), 1)
        self.assertEqual(
            diff.path_grammar_conflicts[0]["verb"], "FORECAST",
        )

    def test_path_with_parameter_segment_does_not_trigger(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            ep = tdp / "endpoints"
            ep.mkdir()
            # ``{forecast}`` is a parameterized segment; the
            # converter doesn't flag it because parameter names are
            # author-chosen.
            (ep / "ok.toml").write_text(
                '[endpoint]\nmethod = "QUERY"\npath = "/items/{forecast}"\n',
                encoding="utf-8",
            )
            diff = diff_catalogs(_OLD, _NEW)
            scan_deployment(diff, tdp, new_catalog=_NEW)
        self.assertEqual(diff.path_grammar_conflicts, [])

    def test_recipe_step_references_removed_verb(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "agtp-recipes.toml").write_text(textwrap.dedent('''
                [[recipe]]
                name = "audit-flow"
                description = "uses removed verb"
                [recipe.pattern]
                name_exact = "AUDIT"

                [[recipe.steps]]
                method = "QUERY"

                [[recipe.steps]]
                method = "LEGACY_AUDIT"
            ''').strip(), encoding="utf-8")
            diff = diff_catalogs(_OLD, _NEW)
            scan_deployment(diff, tdp, new_catalog=_NEW)
        self.assertEqual(len(diff.recipe_conflicts), 1)
        self.assertEqual(
            diff.recipe_conflicts[0]["recipe"], "audit-flow",
        )
        self.assertEqual(diff.recipe_conflicts[0]["step"], 2)

    def test_policies_methods_references_removed_verb(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "agtp-server.toml").write_text(
                '[server]\nserver_id = "t.local"\n'
                '[policies.methods]\n'
                'allow = ["RECONCILE", "LEGACY_AUDIT"]\n'
                'disallow = ["LEGACY_AUDIT"]\n',
                encoding="utf-8",
            )
            diff = diff_catalogs(_OLD, _NEW)
            scan_deployment(diff, tdp, new_catalog=_NEW)
        # Two conflicts: one in ``allow``, one in ``disallow``.
        self.assertEqual(len(diff.method_policy_conflicts), 2)
        directives = sorted(
            c["directive"] for c in diff.method_policy_conflicts
        )
        self.assertEqual(directives, ["allow", "disallow"])

    def test_policies_methods_skips_wildcard_directive(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "agtp-server.toml").write_text(
                '[server]\nserver_id = "t.local"\n'
                '[policies.methods]\nallow = "*"\nlegacy = "NONE"\n',
                encoding="utf-8",
            )
            diff = diff_catalogs(_OLD, _NEW)
            scan_deployment(diff, tdp, new_catalog=_NEW)
        self.assertEqual(diff.method_policy_conflicts, [])

    def test_empty_deployment_dir_yields_no_conflicts(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            diff = diff_catalogs(_OLD, _NEW)
            scan_deployment(diff, tdp, new_catalog=_NEW)
        self.assertFalse(diff.has_deployment_breakage)

    def test_missing_endpoints_dir_is_silently_skipped(self):
        # Deployment may have agtp-server.toml but no endpoints/.
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "agtp-server.toml").write_text(
                '[server]\nserver_id = "t.local"\n'
                '[policies.methods]\nallow = ["RECONCILE"]\n',
                encoding="utf-8",
            )
            diff = diff_catalogs(_OLD, _NEW)
            scan_deployment(diff, tdp, new_catalog=_NEW)
        # agtp-server.toml got scanned; no path/recipe conflicts.
        self.assertEqual(diff.path_grammar_conflicts, [])
        self.assertEqual(diff.recipe_conflicts, [])
        self.assertEqual(diff.method_policy_conflicts, [])

    def test_malformed_endpoint_toml_skipped_silently(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            ep = tdp / "endpoints"
            ep.mkdir()
            (ep / "bad.toml").write_text(
                "this is not valid toml at all =", encoding="utf-8",
            )
            (ep / "good.toml").write_text(
                '[endpoint]\nmethod = "LEGACY_AUDIT"\npath = "/x"\n',
                encoding="utf-8",
            )
            diff = diff_catalogs(_OLD, _NEW)
            scan_deployment(diff, tdp, new_catalog=_NEW)
        # Bad file silently skipped; good file produces the expected
        # endpoint conflict.
        self.assertEqual(len(diff.endpoint_conflicts), 1)


# ===========================================================================
# Rendering.
# ===========================================================================


class RenderTextTests(unittest.TestCase):

    def test_renders_added_and_removed_sections(self):
        diff = diff_catalogs(_OLD, _NEW)
        out = render_text(diff)
        self.assertIn("Catalog diff: 1.0.0 -> 1.1.0", out)
        self.assertIn("Added", out)
        self.assertIn("FORECAST", out)
        self.assertIn("Removed", out)
        self.assertIn("LEGACY_AUDIT", out)

    def test_renders_no_breakage_summary_for_clean_diff(self):
        diff = diff_catalogs(_OLD, _OLD)
        out = render_text(diff)
        self.assertIn("no breaking changes", out)

    def test_renders_breakage_summary_when_deployment_conflicts(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            ep = tdp / "endpoints"
            ep.mkdir()
            (ep / "x.toml").write_text(
                '[endpoint]\nmethod = "LEGACY_AUDIT"\npath = "/x"\n',
                encoding="utf-8",
            )
            diff = diff_catalogs(_OLD, _NEW)
            scan_deployment(diff, tdp, new_catalog=_NEW)
            out = render_text(diff)
        self.assertIn("Endpoint conflicts", out)
        self.assertIn("breaking change", out)


# ===========================================================================
# Catalog loader.
# ===========================================================================


class LoadCatalogTests(unittest.TestCase):

    def test_loads_valid_catalog(self):
        with TemporaryDirectory() as td:
            p = _write_catalog(Path(td) / "v.json", _OLD)
            doc = load_catalog(p)
        self.assertEqual(doc["version"], "1.0.0")

    def test_missing_file_raises(self):
        with self.assertRaises(CatalogDiffError):
            load_catalog("/no/such/catalog.json")

    def test_malformed_json_raises(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("not json at all", encoding="utf-8")
            with self.assertRaises(CatalogDiffError):
                load_catalog(p)

    def test_missing_methods_object_raises(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "v.json"
            p.write_text(json.dumps({"version": "1"}), encoding="utf-8")
            with self.assertRaises(CatalogDiffError):
                load_catalog(p)


# ===========================================================================
# CLI.
# ===========================================================================


class CLITests(unittest.TestCase):

    def test_cli_returns_zero_on_clean_diff(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            old_p = _write_catalog(tdp / "old.json", _OLD)
            new_p = _write_catalog(tdp / "new.json", _NEW)
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                rc = cli_main([str(old_p), str(new_p)])
        self.assertEqual(rc, 0)

    def test_cli_returns_one_on_deployment_breakage(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            old_p = _write_catalog(tdp / "old.json", _OLD)
            new_p = _write_catalog(tdp / "new.json", _NEW)
            deploy = tdp / "deploy"
            (deploy / "endpoints").mkdir(parents=True)
            (deploy / "endpoints" / "x.toml").write_text(
                '[endpoint]\nmethod = "LEGACY_AUDIT"\npath = "/x"\n',
                encoding="utf-8",
            )
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                rc = cli_main([
                    str(old_p), str(new_p),
                    "--against-deployment", str(deploy),
                ])
        self.assertEqual(rc, 1)

    def test_cli_returns_two_on_parse_error(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "old.json").write_text("not json", encoding="utf-8")
            new_p = _write_catalog(tdp / "new.json", _NEW)
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                rc = cli_main([str(tdp / "old.json"), str(new_p)])
        self.assertEqual(rc, 2)

    def test_cli_json_output_is_valid_json(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            old_p = _write_catalog(tdp / "old.json", _OLD)
            new_p = _write_catalog(tdp / "new.json", _NEW)
            captured = []

            def _capture(text="", **kw):
                captured.append(text)

            with mock.patch("builtins.print", side_effect=_capture), \
                 mock.patch("sys.stderr"):
                rc = cli_main([str(old_p), str(new_p), "--json"])
        self.assertEqual(rc, 0)
        # Reconstruct the JSON output.
        joined = "\n".join(s for s in captured if s.strip())
        parsed = json.loads(joined)
        self.assertEqual(parsed["old_version"], "1.0.0")
        self.assertEqual(parsed["new_version"], "1.1.0")
        self.assertIn("FORECAST", parsed["added"])

    def test_cli_no_deployment_on_clean_diff_returns_zero(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            same_p = _write_catalog(tdp / "v.json", _OLD)
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                rc = cli_main([str(same_p), str(same_p)])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
