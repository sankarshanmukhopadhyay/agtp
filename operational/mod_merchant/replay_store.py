"""
Intent Assertion replay detection for ``mod_merchant``.

Background (governance/security hardening pass): an Intent
Assertion (``agtp.intent.build_intent_assertion``) is a short-lived
(default 5-minute) signed JWT a buyer commits to a specific
PURCHASE. It carries a fresh ``jti`` for exactly this purpose, but
nothing in the reference implementation checked it — ``mod_merchant``
verified *who the counterparty is* (Merchant-ID, manifest
fingerprint) but never *whether this specific commitment was already
spent*. A captured or logged assertion was fully replayable against
the same merchant within its TTL, which is a direct double-spend /
duplicate-fulfillment shape for anyone who follows the documented
pattern and trusts "PURCHASE + mod_merchant passed" as sufficient.

This module is the missing half: a small, pluggable interface for
"has this jti been presented before", plus an in-memory reference
implementation. :class:`mod_merchant.hook.MerchantHook` calls it, when
configured, against the ``Intent-Assertion-Jti`` request header —
deliberately a header, not a body field, so the check runs at the
wire edge before the body is parsed, matching the module's existing
design constraint (see ``operational/mod_merchant/hook.py``
docstring: verification "must happen at the merchant edge, before
the merchant-side application layer parses the request body").

The module provides both a single-process in-memory reference store and a durable SQLite store for multi-process deployments on one host. Horizontally distributed deployments can implement `SeenJtiStore` against a shared database while preserving atomic check-and-record semantics.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Dict, Optional


class SeenJtiStore(ABC):
    """Plug-in surface for Intent Assertion replay detection.

    Implementations must be safe for concurrent use — PURCHASE
    requests for the same merchant can arrive on multiple connections
    simultaneously, and a race between two presentations of the same
    ``jti`` must not let both through.
    """

    @abstractmethod
    def seen(self, jti: str) -> bool:
        """Return True if ``jti`` has already been recorded (and not
        yet expired, for implementations that expire entries)."""

    @abstractmethod
    def record(self, jti: str, *, ttl_seconds: int) -> None:
        """Record ``jti`` as seen, valid for at least ``ttl_seconds``
        from now. Implementations MAY retain it longer; they MUST
        NOT forget it sooner — a store that expires entries early
        reopens the replay window it exists to close.
        """

    def check_and_record(self, jti: str, *, ttl_seconds: int) -> bool:
        """Atomically check-then-record. Returns True when ``jti``
        was already seen (caller should refuse); False when this
        call is the first presentation (caller should proceed — the
        jti is now recorded).

        The default implementation is NOT atomic across the two
        abstract methods and is provided only as a convenience for
        stores where ``seen``/``record`` are already safe to compose
        this way (like the in-memory reference implementation, which
        overrides this method directly to hold its lock across both
        steps). Implementations backed by an external store with
        native atomic operations (e.g. Redis ``SET ... NX``) SHOULD
        override this method rather than relying on the default.
        """
        if self.seen(jti):
            return True
        self.record(jti, ttl_seconds=ttl_seconds)
        return False


class InMemorySeenJtiStore(SeenJtiStore):
    """Single-process reference implementation.

    Entries expire ``ttl_seconds`` after they're recorded (matching
    the Intent Assertion's own TTL is the natural choice — there's no
    replay risk to guard against once the assertion itself would be
    rejected as expired by any spec-compliant verifier). A lazy sweep
    on each call keeps the dict bounded without a background thread.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, float] = {}  # jti -> expires_at unix ts
        self._lock = threading.Lock()

    def _sweep_locked(self, now: float) -> None:
        expired = [jti for jti, exp in self._entries.items() if exp <= now]
        for jti in expired:
            del self._entries[jti]

    def seen(self, jti: str) -> bool:
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            return jti in self._entries

    def record(self, jti: str, *, ttl_seconds: int) -> None:
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            self._entries[jti] = now + max(ttl_seconds, 0)

    def check_and_record(self, jti: str, *, ttl_seconds: int) -> bool:
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            if jti in self._entries:
                return True
            self._entries[jti] = now + max(ttl_seconds, 0)
            return False

    def reset_for_tests(self) -> None:
        with self._lock:
            self._entries.clear()


__all__ = [
    "InMemorySeenJtiStore",
    "SeenJtiStore",
    "SQLiteSeenJtiStore",
]

class SQLiteSeenJtiStore(SeenJtiStore):
    """Durable, cross-process replay store backed by SQLite."""
    def __init__(self, path: str) -> None:
        import sqlite3
        from pathlib import Path
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS seen_jti(jti TEXT PRIMARY KEY, expires_at REAL NOT NULL)")

    def _connect(self):
        import sqlite3
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def seen(self, jti: str) -> bool:
        now = time.time()
        with self._connect() as db:
            db.execute("DELETE FROM seen_jti WHERE expires_at<=?", (now,))
            return db.execute("SELECT 1 FROM seen_jti WHERE jti=?", (jti,)).fetchone() is not None

    def record(self, jti: str, *, ttl_seconds: int) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO seen_jti(jti,expires_at) VALUES (?,?)", (jti, time.time()+max(ttl_seconds,0)))

    def check_and_record(self, jti: str, *, ttl_seconds: int) -> bool:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM seen_jti WHERE expires_at<=?", (now,))
            if db.execute("SELECT 1 FROM seen_jti WHERE jti=?", (jti,)).fetchone():
                db.execute("COMMIT"); return True
            db.execute("INSERT INTO seen_jti(jti,expires_at) VALUES (?,?)", (jti, now+max(ttl_seconds,0)))
            db.execute("COMMIT"); return False
