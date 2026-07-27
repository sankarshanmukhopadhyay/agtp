"""
AGTP-Presence M4 — adaptive scope partitioning (split/merge).
"""

from __future__ import annotations

import hashlib
import unittest

from presence.partition import PartitionManager


def aid(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class PartitionTests(unittest.TestCase):
    def test_single_leaf_below_threshold(self):
        pm = PartitionManager("{capability: settle}", split_threshold=8, merge_threshold=3)
        for i in range(5):
            pm.add_member(aid(f"a{i}"))
        self.assertEqual(pm.leaf_count(), 1)
        self.assertEqual(pm.leaf_prefixes(), [""])

    def test_splits_when_over_threshold(self):
        pm = PartitionManager("{capability: settle}", split_threshold=8, merge_threshold=3)
        ids = [aid(f"agent-{i}") for i in range(20)]
        for i in ids:
            pm.add_member(i)
        self.assertGreater(pm.leaf_count(), 1)
        # every leaf respects the threshold, members conserved, no dup/loss
        self.assertTrue(all(len(pm.members(p)) <= 8 for p in pm.leaf_prefixes()))
        placed = sum(len(pm.members(p)) for p in pm.leaf_prefixes())
        self.assertEqual(placed, 20)
        self.assertEqual(pm.total_members(), 20)

    def test_each_leaf_has_distinct_overlay_id(self):
        pm = PartitionManager("{capability: settle}", split_threshold=4, merge_threshold=1)
        for i in range(16):
            pm.add_member(aid(f"x{i}"))
        keys = set(pm.leaf_scope_keys().values())
        self.assertEqual(len(keys), pm.leaf_count())

    def test_a_member_lands_in_exactly_one_leaf(self):
        pm = PartitionManager("s", split_threshold=4, merge_threshold=1)
        ids = [aid(f"m{i}") for i in range(30)]
        for i in ids:
            pm.add_member(i)
        for i in ids:
            hits = [p for p in pm.leaf_prefixes() if i in pm.members(p)]
            self.assertEqual(len(hits), 1)

    def test_merges_back_when_sparse(self):
        pm = PartitionManager("s", split_threshold=8, merge_threshold=3)
        ids = [aid(f"agent-{i}") for i in range(20)]
        for i in ids:
            pm.add_member(i)
        self.assertGreater(pm.leaf_count(), 1)
        for i in ids[:18]:
            pm.remove_member(i)
        self.assertEqual(pm.leaf_count(), 1)
        self.assertEqual(pm.total_members(), 2)

    def test_rejects_bad_thresholds(self):
        with self.assertRaises(ValueError):
            PartitionManager("s", split_threshold=4, merge_threshold=4)


if __name__ == "__main__":
    unittest.main()
