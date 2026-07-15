"""Durable RCNS rate-limit and idempotency state backends."""
from __future__ import annotations
import sqlite3, threading, time
from pathlib import Path
from typing import Optional, Protocol

class RcnsStateStore(Protocol):
    def consume(self, agent_id: str, *, limit: int, now: float) -> bool: ...
    def get_idempotency(self, agent_id: str, key: str, *, now: float) -> Optional[str]: ...
    def put_idempotency(self, agent_id: str, key: str, synthesis_id: str, *, expires_at: float) -> None: ...

class SQLiteRcnsStateStore:
    """Cross-process RCNS state using SQLite transactions.

    ``consume`` atomically prunes the rolling window, checks the limit, and
    reserves the current attempt. WAL mode permits multiple daemon processes
    on one host to share the same state file safely.
    """
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS negotiations(
              agent_id TEXT NOT NULL, occurred_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rcns_negotiations
              ON negotiations(agent_id, occurred_at);
            CREATE TABLE IF NOT EXISTS idempotency(
              agent_id TEXT NOT NULL, idem_key TEXT NOT NULL,
              synthesis_id TEXT NOT NULL, expires_at REAL NOT NULL,
              PRIMARY KEY(agent_id, idem_key)
            );
            """)

    def consume(self, agent_id: str, *, limit: int, now: float) -> bool:
        if limit <= 0:
            return False
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM negotiations WHERE occurred_at < ?", (now - 60.0,))
            count = db.execute(
                "SELECT COUNT(*) FROM negotiations WHERE agent_id=? AND occurred_at>=?",
                (agent_id, now - 60.0),
            ).fetchone()[0]
            if count >= limit:
                db.execute("COMMIT")
                return True
            db.execute("INSERT INTO negotiations(agent_id, occurred_at) VALUES (?,?)", (agent_id, now))
            db.execute("COMMIT")
            return False

    def get_idempotency(self, agent_id: str, key: str, *, now: float) -> Optional[str]:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM idempotency WHERE expires_at<=?", (now,))
            row = db.execute(
                "SELECT synthesis_id FROM idempotency WHERE agent_id=? AND idem_key=? AND expires_at>?",
                (agent_id, key, now),
            ).fetchone()
            return str(row[0]) if row else None

    def put_idempotency(self, agent_id: str, key: str, synthesis_id: str, *, expires_at: float) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO idempotency(agent_id,idem_key,synthesis_id,expires_at) VALUES (?,?,?,?) "
                "ON CONFLICT(agent_id,idem_key) DO UPDATE SET synthesis_id=excluded.synthesis_id, expires_at=excluded.expires_at",
                (agent_id, key, synthesis_id, expires_at),
            )
