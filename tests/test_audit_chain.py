"""
Tests for server.audit_chain — the per-agent chain head store.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from server.audit_chain import AuditChainStore, ChainHead, default_chain_head_root


AGENT_A = "a" * 64
AGENT_B = "b" * 64


def test_head_returns_none_for_unknown_agent(tmp_path: Path) -> None:
    store = AuditChainStore(tmp_path)
    assert store.head(AGENT_A) is None


def test_write_then_head_roundtrip(tmp_path: Path) -> None:
    store = AuditChainStore(tmp_path)
    store.write(AGENT_A, audit_id="aud-1", at_iso="2026-05-21T10:00:00Z")
    head = store.head(AGENT_A)
    assert head == ChainHead(audit_id="aud-1", at_iso="2026-05-21T10:00:00Z")


def test_subsequent_write_replaces_head(tmp_path: Path) -> None:
    store = AuditChainStore(tmp_path)
    store.write(AGENT_A, audit_id="aud-1", at_iso="2026-05-21T10:00:00Z")
    store.write(AGENT_A, audit_id="aud-2", at_iso="2026-05-21T11:00:00Z")
    head = store.head(AGENT_A)
    assert head is not None
    assert head.audit_id == "aud-2"
    assert head.at_iso == "2026-05-21T11:00:00Z"


def test_chains_are_per_agent(tmp_path: Path) -> None:
    store = AuditChainStore(tmp_path)
    store.write(AGENT_A, audit_id="aud-a1", at_iso="2026-05-21T10:00:00Z")
    store.write(AGENT_B, audit_id="aud-b1", at_iso="2026-05-21T10:00:01Z")
    assert store.head(AGENT_A).audit_id == "aud-a1"
    assert store.head(AGENT_B).audit_id == "aud-b1"


def test_empty_agent_id_is_no_op(tmp_path: Path) -> None:
    """Server-level operations have no agent; write should silently skip."""
    store = AuditChainStore(tmp_path)
    store.write("", audit_id="aud-x", at_iso="2026-05-21T10:00:00Z")
    # No files should be created in the root.
    assert list(tmp_path.iterdir()) == []


def test_root_dir_created_lazily(tmp_path: Path) -> None:
    root = tmp_path / "does" / "not" / "exist"
    store = AuditChainStore(root)
    assert not root.exists()
    store.write(AGENT_A, audit_id="aud-1", at_iso="2026-05-21T10:00:00Z")
    assert root.exists()
    assert (root / f"{AGENT_A}.json").exists()


def test_corrupt_file_returns_none(tmp_path: Path) -> None:
    """A garbage chain head file is treated as missing so a single
    bad write doesn't break the chain forever."""
    store = AuditChainStore(tmp_path)
    (tmp_path / f"{AGENT_A}.json").write_text("not json {")
    assert store.head(AGENT_A) is None


def test_missing_fields_returns_none(tmp_path: Path) -> None:
    store = AuditChainStore(tmp_path)
    (tmp_path / f"{AGENT_A}.json").write_text(json.dumps({"foo": "bar"}))
    assert store.head(AGENT_A) is None


def test_path_separator_in_agent_id_does_not_escape(tmp_path: Path) -> None:
    """Defensive: a stray agent_id with path separators must not
    write outside the root."""
    store = AuditChainStore(tmp_path)
    evil = "../../escape"
    store.write(evil, audit_id="aud-1", at_iso="2026-05-21T10:00:00Z")
    # All files must live inside tmp_path.
    for p in tmp_path.rglob("*.json"):
        assert tmp_path in p.resolve().parents or p.resolve().parent == tmp_path


def test_concurrent_writes_do_not_corrupt(tmp_path: Path) -> None:
    """Many threads writing the same agent should leave a valid JSON
    file (the lock + atomic rename guarantees this)."""
    store = AuditChainStore(tmp_path)
    N = 50

    def writer(i: int) -> None:
        store.write(AGENT_A, audit_id=f"aud-{i}", at_iso=f"2026-05-21T10:00:{i:02d}Z")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # File exists and parses cleanly.
    head = store.head(AGENT_A)
    assert head is not None
    assert head.audit_id.startswith("aud-")


def test_default_root_structure() -> None:
    """The default root, whatever platform we're on, ends in
    audit/chain_heads."""
    root = default_chain_head_root()
    assert root.name == "chain_heads"
    assert root.parent.name == "audit"


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific")
def test_default_root_on_windows_uses_appdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    root = default_chain_head_root()
    assert root == tmp_path / "agtp" / "audit" / "chain_heads"
