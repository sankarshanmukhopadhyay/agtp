"""
Tests for the AGTP path grammar (``core.path_grammar``).

The grammar is deliberately minimal: paths must begin with ``/``,
must not have a trailing slash unless they are exactly ``/``, and
must not embed AGTP verbs in any path segment. Everything else
(casing, kebab vs snake, segment depth, parameter naming) is
operator judgment.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.path_grammar import (
    PATH_PROTOCOL_VERBS,
    PathGrammarError,
    validate_path,
)


class PathGrammarStructuralTests(unittest.TestCase):

    def test_root_path_is_valid(self):
        # The bare root is the one allowed trailing-slash form.
        self.assertIsNone(validate_path("/"))

    def test_simple_path_is_valid(self):
        self.assertIsNone(validate_path("/orders"))
        self.assertIsNone(validate_path("/orders/123"))

    def test_kebab_case_segment_is_valid(self):
        self.assertIsNone(validate_path("/order-history"))

    def test_snake_case_segment_is_valid(self):
        self.assertIsNone(validate_path("/order_history"))

    def test_parameterized_segment_is_valid(self):
        self.assertIsNone(validate_path("/orders/{order_id}"))

    def test_must_begin_with_slash(self):
        with self.assertRaises(PathGrammarError) as ctx:
            validate_path("orders")
        self.assertEqual(ctx.exception.code, "invalid-format")

    def test_trailing_slash_rejected(self):
        with self.assertRaises(PathGrammarError) as ctx:
            validate_path("/orders/")
        self.assertEqual(ctx.exception.code, "invalid-format")

    def test_absolute_url_rejected(self):
        with self.assertRaises(PathGrammarError) as ctx:
            validate_path("https://example.com/orders")
        self.assertEqual(ctx.exception.code, "invalid-format")

    def test_empty_string_rejected(self):
        with self.assertRaises(PathGrammarError):
            validate_path("")


class PathGrammarVerbInPathTests(unittest.TestCase):

    def test_approved_verb_in_segment_rejected(self):
        # /reconcile/... is wrong; RECONCILE belongs in the method.
        with self.assertRaises(PathGrammarError) as ctx:
            validate_path("/reconcile/123")
        self.assertEqual(ctx.exception.code, "verb-in-path")
        self.assertEqual(ctx.exception.segment, "reconcile")

    def test_legacy_verb_in_segment_rejected(self):
        # /get/orders is rejected even though GET isn't an approved
        # AGTP verb — the legacy set is included in the path
        # blocklist so a future server policy enabling GET doesn't
        # have to deal with /get/orders paths shipped meanwhile.
        with self.assertRaises(PathGrammarError) as ctx:
            validate_path("/get/orders")
        self.assertEqual(ctx.exception.code, "verb-in-path")

    def test_embedded_verb_in_segment_rejected(self):
        with self.assertRaises(PathGrammarError) as ctx:
            validate_path("/orders/query")
        self.assertEqual(ctx.exception.code, "verb-in-path")
        self.assertEqual(ctx.exception.segment, "query")

    def test_compound_segments_with_verb_substring_allowed(self):
        # /fetch-price normalizes to FETCHPRICE which isn't a verb;
        # the grammar treats the segment as a noun. Same for
        # /fetch_price. The grammar only rejects exact verb tokens.
        self.assertIsNone(validate_path("/fetch-price"))
        self.assertIsNone(validate_path("/fetch_price"))

    def test_kebab_or_snake_verb_alone_still_rejected(self):
        # /F-E-T-C-H normalizes to FETCH and is rejected. Kebab/
        # snake separators are stripped before the verb check, so
        # the operator can't disguise a verb token by hyphenating it.
        with self.assertRaises(PathGrammarError):
            validate_path("/f-e-t-c-h")
        with self.assertRaises(PathGrammarError):
            validate_path("/f_e_t_c_h")

    def test_parameterized_verb_segment_is_exempt(self):
        # {fetch} is a parameterized segment; the literal text inside
        # braces is variable, so the grammar doesn't try to gate it.
        self.assertIsNone(validate_path("/orders/{fetch}"))

    def test_verb_substring_is_allowed(self):
        # 'getter' contains 'get' as a substring but isn't a
        # standalone verb token. Path grammar only rejects exact
        # matches after normalization.
        self.assertIsNone(validate_path("/getter"))


class PathProtocolVerbsTests(unittest.TestCase):

    def test_includes_approved_set(self):
        self.assertIn("QUERY", PATH_PROTOCOL_VERBS)
        self.assertIn("RECONCILE", PATH_PROTOCOL_VERBS)

    def test_includes_legacy_set(self):
        for name in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            self.assertIn(name, PATH_PROTOCOL_VERBS)


if __name__ == "__main__":
    unittest.main()
