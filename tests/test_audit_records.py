"""
Tests for server.audit_records — the per-audit-id JWS store.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from server.audit_records import AuditRecordStore, default_records_root


def _aid(prefix: str) -> str:
    """Build a valid 64-char hex audit_id from a short prefix."""
    return (prefix + "0" * 64)[:64]


def test_read_returns_none_for_unknown(tmp_path: Path) -> None:
    store = AuditRecordStore(tmp_path)
    assert store.read(_aid("a")) is None


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    store = AuditRecordStore(tmp_path)
    aid = _aid("abc")
    jws = "header.payload.signature"
    store.write(aid, jws)
    assert store.read(aid) == jws


def test_records_are_sharded_by_prefix(tmp_path: Path) -> None:
    store = AuditRecordStore(tmp_path)
    aid = _aid("ab")
    store.write(aid, "x")
    # File lives under {prefix-2-chars}/{aid}.jws
    expected = tmp_path / "ab" / f"{aid}.jws"
    assert expected.exists()


def test_subsequent_writes_replace(tmp_path: Path) -> None:
    """Atomic replace: rewriting an audit_id keeps a single valid file."""
    store = AuditRecordStore(tmp_path)
    aid = _aid("c")
    store.write(aid, "first")
    store.write(aid, "second")
    assert store.read(aid) == "second"


def test_concurrent_writes_do_not_corrupt(tmp_path: Path) -> None:
    """Many threads racing on a single audit_id must leave a
    well-formed file (one of the written values)."""
    store = AuditRecordStore(tmp_path)
    aid = _aid("d")
    N = 30

    def writer(i: int) -> None:
        store.write(aid, f"value-{i}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out = store.read(aid)
    assert out is not None
    assert out.startswith("value-")


def test_malformed_audit_id_is_rejected(tmp_path: Path) -> None:
    """The id used as a path component MUST be validated to prevent
    path-traversal. Note: uppercase hex is accepted and lowered (case
    is a presentation detail; the canonical form is lowercase)."""
    store = AuditRecordStore(tmp_path)
    with pytest.raises(ValueError):
        store.write("../escape", "x")
    with pytest.raises(ValueError):
        store.write("g" * 64, "x")   # non-hex
    with pytest.raises(ValueError):
        store.write("a" * 63, "x")   # too short
    with pytest.raises(ValueError):
        store.write("a" * 65, "x")   # too long


def test_uppercase_audit_id_normalizes_to_lowercase(tmp_path: Path) -> None:
    """Uppercase hex audit_ids are accepted and lowered to the
    canonical form so reads after a write match regardless of case."""
    store = AuditRecordStore(tmp_path)
    aid_upper = "AB" * 32
    aid_lower = "ab" * 32
    store.write(aid_upper, "value")
    assert store.read(aid_lower) == "value"
    assert store.read(aid_upper) == "value"


def test_read_with_malformed_id_returns_none(tmp_path: Path) -> None:
    """Reads of malformed ids return None (not raise) — INSPECT
    treats attacker-supplied ids as cache misses, not 5xx."""
    store = AuditRecordStore(tmp_path)
    assert store.read("../escape") is None
    assert store.read("not-hex") is None


def test_default_records_root_structure() -> None:
    root = default_records_root()
    assert root.name == "records"
    assert root.parent.name == "audit"


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific")
def test_default_records_root_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    root = default_records_root()
    assert root == tmp_path / "agtp" / "audit" / "records"
