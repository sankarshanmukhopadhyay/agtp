"""
Tests for ``tools.chain_integrity_check`` — the audit-chain
tamper-evidence detector added alongside the F5 governance finding
(chain-head store is a separate, unauthenticated pointer that can be
silently truncated or forked without touching the underlying signed
records).

Builds real signed chains with a throwaway Ed25519 key via
``SigningService``, writes them through the actual
``AuditChainStore`` / ``AuditRecordStore`` on disk, then exercises
the walker against clean chains, an orphaned head (deleted head
target), a mid-chain gap (deleted record), and a multi-agent mix.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.audit_chain import AuditChainStore
from server.audit_records import AuditRecordStore
from server.signing import SigningService
from tools.chain_integrity_check import discover_agent_ids, run, walk_chain


AGENT_A = "a" * 64
AGENT_B = "b" * 64


def _build_chain(
    signing: SigningService,
    chain_store: AuditChainStore,
    records_store: AuditRecordStore,
    agent_id: str,
    *,
    length: int,
) -> list:
    """Append ``length`` signed records to ``agent_id``'s chain on
    disk, exactly the way the daemon's ``_finalize_response`` path
    does. Returns the list of audit_ids in chain order (oldest
    first)."""
    audit_ids = []
    previous = ""
    for i in range(length):
        record = signing.build_attribution_record(
            agent_id=agent_id,
            server_id="test.local",
            issued_at=f"2026-01-0{i+1}T00:00:00Z",
            status=200,
            previous_audit_id=previous,
        )
        records_store.write(record.audit_id, record.jws)
        chain_store.write(agent_id, record.audit_id, record.payload["issued_at"])
        audit_ids.append(record.audit_id)
        previous = record.audit_id
    return audit_ids


class ChainIntegrityCheckTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.chain_head_root = self.tmp / "chain_heads"
        self.records_root = self.tmp / "records"
        self.chain_store = AuditChainStore(self.chain_head_root)
        self.records_store = AuditRecordStore(self.records_root)
        self.signing = SigningService(private_key=Ed25519PrivateKey.generate())

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_chain_reaches_genesis(self):
        _build_chain(
            self.signing, self.chain_store, self.records_store,
            AGENT_A, length=5,
        )
        result = walk_chain(
            AGENT_A, chain_store=self.chain_store,
            records_store=self.records_store,
        )
        self.assertTrue(result.reached_genesis)
        self.assertFalse(result.broken)
        self.assertFalse(result.orphaned_head)
        self.assertEqual(result.records_walked, 5)

    def test_never_audited_agent_is_not_flagged_broken(self):
        result = walk_chain(
            "c" * 64, chain_store=self.chain_store,
            records_store=self.records_store,
        )
        self.assertIsNone(result.head_audit_id)
        self.assertFalse(result.broken)

    def test_orphaned_head_detected(self):
        """Simulates deleting/replacing the chain-head file's
        target record directly — the sharpest tamper this tool
        exists to catch."""
        ids = _build_chain(
            self.signing, self.chain_store, self.records_store,
            AGENT_A, length=3,
        )
        # Delete the record the head points to, without touching
        # the head pointer itself.
        head_record_path = self.records_store._path_for(ids[-1])
        head_record_path.unlink()

        result = walk_chain(
            AGENT_A, chain_store=self.chain_store,
            records_store=self.records_store,
        )
        self.assertTrue(result.broken)
        self.assertTrue(result.orphaned_head)
        self.assertEqual(result.break_at, ids[-1])
        self.assertEqual(result.records_walked, 0)

    def test_mid_chain_gap_detected(self):
        ids = _build_chain(
            self.signing, self.chain_store, self.records_store,
            AGENT_A, length=5,
        )
        # Delete a record in the middle of the chain (index 2 of 5,
        # zero-indexed oldest-first) — the head is intact and the
        # two most recent records are readable, but the walk stops.
        mid_id = ids[2]
        self.records_store._path_for(mid_id).unlink()

        result = walk_chain(
            AGENT_A, chain_store=self.chain_store,
            records_store=self.records_store,
        )
        self.assertTrue(result.broken)
        self.assertFalse(result.orphaned_head)
        self.assertEqual(result.break_at, mid_id)
        # Walked the two records newer than the gap (ids[4], ids[3])
        # before hitting the missing ids[2].
        self.assertEqual(result.records_walked, 2)

    def test_truncated_head_masquerades_as_short_clean_chain(self):
        """Documents the residual limitation this tool cannot fully
        close from local state alone: overwriting the head pointer
        to an earlier (still-valid) audit_id in the same chain is
        structurally indistinguishable from that having always been
        the head. This is exactly why the module docstring
        recommends SCITT mode / external anchoring for deployments
        that need tamper-evidence against a compromised host, not
        just tamper-detection of missing links."""
        ids = _build_chain(
            self.signing, self.chain_store, self.records_store,
            AGENT_A, length=5,
        )
        # Rewind the head to an earlier record — simulates deleting
        # chain_heads/{agent}.json and letting the next legitimate
        # write treat ids[2] as if it were never superseded, OR an
        # attacker directly rewriting the pointer.
        self.chain_store.write(AGENT_A, ids[2], "2026-01-03T00:00:00Z")

        result = walk_chain(
            AGENT_A, chain_store=self.chain_store,
            records_store=self.records_store,
        )
        # Walks cleanly to genesis from the (tampered) head — no
        # missing links, so it reports clean. The records for
        # ids[3] and ids[4] still exist on disk and are individually
        # valid; they're just unreachable from this head. A
        # follow-up "orphaned records" scan (scanning records_root
        # for audit_ids no live chain references) would catch this
        # class; deliberately out of scope for this pass — see the
        # backlog in the accompanying evaluation report.
        self.assertTrue(result.reached_genesis)
        self.assertFalse(result.broken)

    def test_discover_agent_ids_lists_all_chain_heads(self):
        _build_chain(
            self.signing, self.chain_store, self.records_store,
            AGENT_A, length=1,
        )
        _build_chain(
            self.signing, self.chain_store, self.records_store,
            AGENT_B, length=1,
        )
        ids = discover_agent_ids(self.chain_head_root)
        self.assertEqual(sorted(ids), sorted([AGENT_A, AGENT_B]))

    def test_run_end_to_end_multi_agent_summary(self):
        _build_chain(
            self.signing, self.chain_store, self.records_store,
            AGENT_A, length=3,
        )
        ids_b = _build_chain(
            self.signing, self.chain_store, self.records_store,
            AGENT_B, length=3,
        )
        self.records_store._path_for(ids_b[-1]).unlink()  # orphan B's head

        results = run(
            chain_head_root=self.chain_head_root,
            records_root=self.records_root,
        )
        by_agent = {r.agent_id: r for r in results}
        self.assertFalse(by_agent[AGENT_A].broken)
        self.assertTrue(by_agent[AGENT_B].broken)
        self.assertTrue(by_agent[AGENT_B].orphaned_head)

    def test_signature_verification_flags_key_mismatch(self):
        _build_chain(
            self.signing, self.chain_store, self.records_store,
            AGENT_A, length=2,
        )
        wrong_key = Ed25519PrivateKey.generate().public_key()
        result = walk_chain(
            AGENT_A, chain_store=self.chain_store,
            records_store=self.records_store,
            verify_key=wrong_key,
        )
        # Structural walk still completes (signature checking is
        # additive, not a precondition for chain-completeness).
        self.assertTrue(result.reached_genesis)
        self.assertFalse(result.broken)
        self.assertEqual(len(result.signature_failures), 2)

    def test_signature_verification_clean_with_correct_key(self):
        _build_chain(
            self.signing, self.chain_store, self.records_store,
            AGENT_A, length=2,
        )
        result = walk_chain(
            AGENT_A, chain_store=self.chain_store,
            records_store=self.records_store,
            verify_key=self.signing._public,
        )
        self.assertEqual(result.signature_failures, [])


if __name__ == "__main__":
    unittest.main()
