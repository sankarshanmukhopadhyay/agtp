"""
Tests for the simplified method-validation surface in
``core/methods.py``.

Validation reduces to list lookups against the curated verb
catalog at ``core/methods.json``. This module's surface is
everything a dispatcher needs.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.methods import (
    ALL_PROTOCOL_VERBS,
    APPROVED_VERBS,
    EMBEDDED_VERBS,
    LEGACY_VERBS,
    categorize,
    describe,
    find_close_matches,
    get_legacy_preferred,
    is_approved_verb,
    is_embedded_verb,
    is_legacy_verb,
)


class CatalogShapeTests(unittest.TestCase):

    def test_embedded_set_has_eighteen(self):
        # 12 original protocol primitives + Phase 6 INSPECT (audit
        # read surface) + Phase 8 ACTIVATE / DEACTIVATE / REVOKE /
        # REINSTATE / DEPRECATE (full identity lifecycle). Every
        # embedded verb is implemented by every AGTP server
        # identically.
        self.assertEqual(len(EMBEDDED_VERBS), 18)
        for name in (
            "QUERY", "DISCOVER", "DESCRIBE", "SUMMARIZE", "PLAN", "EXECUTE",
            "DELEGATE", "ESCALATE", "CONFIRM", "SUSPEND", "PROPOSE", "NOTIFY",
            "INSPECT",
            "ACTIVATE", "DEACTIVATE", "REVOKE", "REINSTATE", "DEPRECATE",
        ):
            self.assertIn(name, EMBEDDED_VERBS)

    def test_legacy_set_has_five_http_methods(self):
        self.assertEqual(LEGACY_VERBS, {"GET", "POST", "PUT", "DELETE", "PATCH"})

    def test_legacy_and_approved_are_disjoint(self):
        # Legacy verbs are not in the approved set; they're admitted
        # only by per-server opt-in via [policies.methods].
        self.assertEqual(APPROVED_VERBS & LEGACY_VERBS, set())

    def test_embedded_subset_of_protocol(self):
        self.assertTrue(EMBEDDED_VERBS.issubset(ALL_PROTOCOL_VERBS))

    def test_catalog_size_is_in_expected_range(self):
        # The curated list shipped at ~425 verbs. Pin a range so a
        # build_methods regression (e.g., dropping a category) trips
        # this test.
        self.assertGreater(len(APPROVED_VERBS), 300)
        self.assertLess(len(APPROVED_VERBS), 600)


class IsApprovedVerbTests(unittest.TestCase):

    def test_embedded_verb_passes(self):
        self.assertTrue(is_approved_verb("QUERY"))
        self.assertTrue(is_approved_verb("query"))  # case-insensitive

    def test_curated_verb_passes(self):
        self.assertTrue(is_approved_verb("RECONCILE"))
        self.assertTrue(is_approved_verb("EVALUATE"))
        self.assertTrue(is_approved_verb("AUDIT"))

    def test_unknown_verb_fails(self):
        self.assertFalse(is_approved_verb("FROBNICATE"))
        self.assertFalse(is_approved_verb("ZBLARGON"))

    def test_legacy_verb_is_not_approved(self):
        # Legacy verbs only become acceptable through [policies.methods]
        # opt-in; the protocol layer itself does not approve them.
        self.assertFalse(is_approved_verb("GET"))
        self.assertFalse(is_approved_verb("DELETE"))


class IsLegacyVerbTests(unittest.TestCase):

    def test_legacy_methods_recognized(self):
        for name in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(name=name):
                self.assertTrue(is_legacy_verb(name))
                self.assertTrue(is_legacy_verb(name.lower()))

    def test_non_legacy_returns_false(self):
        self.assertFalse(is_legacy_verb("QUERY"))
        self.assertFalse(is_legacy_verb("FROBNICATE"))


class IsEmbeddedVerbTests(unittest.TestCase):

    def test_embedded_methods_recognized(self):
        self.assertTrue(is_embedded_verb("QUERY"))
        self.assertTrue(is_embedded_verb("propose"))

    def test_curated_non_embedded_returns_false(self):
        self.assertFalse(is_embedded_verb("RECONCILE"))


class CategorizeTests(unittest.TestCase):

    def test_returns_categories_for_known_verb(self):
        cats = categorize("RECONCILE")
        self.assertIsNotNone(cats)
        self.assertIn("analysis", cats)

    def test_returns_none_for_unknown_verb(self):
        self.assertIsNone(categorize("FROBNICATE"))

    def test_multi_category_verb(self):
        # ENUMERATE was authored under two categories in the source
        # list; the build script merges them.
        cats = categorize("ENUMERATE")
        self.assertIsNotNone(cats)
        self.assertIn("discovery", cats)
        self.assertIn("retrieval", cats)


class DescribeTests(unittest.TestCase):

    def test_returns_description_for_known_verb(self):
        text = describe("QUERY")
        self.assertIsNotNone(text)
        self.assertGreater(len(text), 0)

    def test_returns_none_for_unknown_verb(self):
        self.assertIsNone(describe("FROBNICATE"))


class GetLegacyPreferredTests(unittest.TestCase):

    def test_get_maps_to_fetch(self):
        self.assertEqual(get_legacy_preferred("GET"), "FETCH")
        self.assertEqual(get_legacy_preferred("get"), "FETCH")

    def test_post_maps_to_create(self):
        self.assertEqual(get_legacy_preferred("POST"), "CREATE")

    def test_put_maps_to_replace(self):
        self.assertEqual(get_legacy_preferred("PUT"), "REPLACE")

    def test_delete_maps_to_remove(self):
        self.assertEqual(get_legacy_preferred("DELETE"), "REMOVE")

    def test_patch_maps_to_modify(self):
        self.assertEqual(get_legacy_preferred("PATCH"), "MODIFY")

    def test_non_legacy_returns_none(self):
        self.assertIsNone(get_legacy_preferred("QUERY"))
        self.assertIsNone(get_legacy_preferred("FROBNICATE"))


class FindCloseMatchesTests(unittest.TestCase):

    def test_typo_one_edit_away_returns_match(self):
        # PROPOSEX is one edit from PROPOSE.
        matches = find_close_matches("PROPOSEX")
        self.assertIn("PROPOSE", matches)

    def test_typo_two_edits_away_returns_match(self):
        # FETHC is two edits from FETCH.
        matches = find_close_matches("FETHC")
        self.assertIn("FETCH", matches)

    def test_legacy_preferred_appears_first(self):
        # GET is legacy with preferred=FETCH; the suggestion list
        # leads with the preferred verb.
        matches = find_close_matches("GET")
        self.assertEqual(matches[0], "FETCH")

    def test_no_close_match_returns_empty(self):
        # ZBLARGON is too far from any known verb.
        matches = find_close_matches("ZBLARGON")
        self.assertEqual(matches, [])

    def test_limit_is_honored(self):
        matches = find_close_matches("Q", max_distance=10, limit=2)
        self.assertLessEqual(len(matches), 2)


if __name__ == "__main__":
    unittest.main()
