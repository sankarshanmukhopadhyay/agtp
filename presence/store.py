"""
The in-memory presence store — M1 coordinator state.

A single coordinator holds every announced :class:`PresenceRecord` keyed
by Agent-ID, plus a capability -> {agent_id} inverted index so the
population query is an index lookup rather than a scan. This is the M1
substitute for the DHT: at demo scale (n=2, one coordinator) routing is
trivial, so the store is a dict with a lock.

Deferred to later milestones (kept out on purpose):
  * TTL aging / eviction sweeps (M2) — records are stored with their TTL
    but never expired here yet;
  * gossip replication to peer full nodes (M2/M4);
  * DHT k-bucket routing (M4).

Thread-safety: the AGTP server handles each connection on its own thread,
so every mutation and read takes ``self._lock``.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Set

from core.identity import AgentDocument
from presence import scopes as _scopes
from presence.records import DEFAULT_TTL_SECONDS, PresenceRecord, Visibility


class PresenceStore:
    """Coordinator-held map of Agent-ID -> PresenceRecord."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: Dict[str, PresenceRecord] = {}
        #: capability token -> set of agent_ids carrying it.
        self._by_capability: Dict[str, Set[str]] = {}

    # -- mutation -----------------------------------------------------

    def announce(self, record: PresenceRecord) -> None:
        """Insert or replace a presence record (ANNOUNCE is idempotent)."""
        with self._lock:
            self._drop_indexes(record.agent_id)
            self._records[record.agent_id] = record
            for cap in record.result_entry.get("capabilities", []):
                self._by_capability.setdefault(cap, set()).add(record.agent_id)

    def withdraw(self, agent_id: str) -> bool:
        """Remove an agent's presence. Returns True if it was present."""
        with self._lock:
            existed = agent_id in self._records
            self._drop_indexes(agent_id)
            self._records.pop(agent_id, None)
            return existed

    def _drop_indexes(self, agent_id: str) -> None:
        for cap, ids in list(self._by_capability.items()):
            ids.discard(agent_id)
            if not ids:
                self._by_capability.pop(cap, None)

    # -- read ---------------------------------------------------------

    def probe(
        self, agent_id: str, *, now: Optional[float] = None
    ) -> Optional[PresenceRecord]:
        """
        Return the record for ``agent_id``, or None if not present or
        aged out. TTL is applied lazily here so a probe never returns a
        stale record; ``now`` is injectable for deterministic tests.
        """
        _now = time.time() if now is None else now
        with self._lock:
            record = self._records.get(agent_id)
            if record is None:
                return None
            if record.is_expired(_now):
                self._drop_indexes(agent_id)
                self._records.pop(agent_id, None)
                return None
            return record

    def query_population(
        self,
        *,
        capability: Optional[str] = None,
        tier: Optional[int] = None,
        owner_domain: Optional[str] = None,
        limit: Optional[int] = None,
        now: Optional[float] = None,
    ) -> List[PresenceRecord]:
        """
        Return live records matching the filter, ordered by Agent-ID for a
        stable result (M1 has no ranking; ranking lands in M3).

        Filters compose (logical AND) across the partition dimensions we
        carry today — ``capability`` (via the derived index), ``tier``,
        and ``owner_domain`` — the M2 realization of multi-overlay
        membership. Expired records are swept lazily first. ``now`` is
        injectable for deterministic tests.
        """
        _now = time.time() if now is None else now
        with self._lock:
            self._sweep_locked(_now)
            if capability is None:
                candidates = list(self._records.values())
            else:
                ids = self._by_capability.get(capability.strip().lower(), set())
                candidates = [self._records[i] for i in ids if i in self._records]

            if tier is not None:
                candidates = [
                    r for r in candidates
                    if r.result_entry.get("trust_tier") == tier
                ]
            if owner_domain is not None:
                candidates = [r for r in candidates if r.owner_domain == owner_domain]

            candidates.sort(key=lambda r: r.agent_id)
            if limit is not None and limit >= 0:
                candidates = candidates[:limit]
            return candidates

    # -- anti-entropy (gossip) ---------------------------------------

    def all_records(self, *, now: Optional[float] = None) -> List[PresenceRecord]:
        """Every live record (expired swept first). Used to build a
        gossip push set."""
        _now = time.time() if now is None else now
        with self._lock:
            self._sweep_locked(_now)
            return list(self._records.values())

    def digest(self, *, now: Optional[float] = None) -> Dict[str, float]:
        """``{agent_id: announced_at_epoch}`` for every live record — the
        compact summary a peer diffs against to decide what it needs."""
        _now = time.time() if now is None else now
        with self._lock:
            self._sweep_locked(_now)
            return {aid: rec.announced_at_epoch for aid, rec in self._records.items()}

    def merge_record(self, record: PresenceRecord) -> bool:
        """
        Merge a record received from a peer. Keeps the most recent by
        ``announced_at_epoch`` (last-writer-wins on announce time, per the
        PDD conflict rule). Returns True if the local view changed.
        """
        with self._lock:
            existing = self._records.get(record.agent_id)
            if existing is not None and existing.announced_at_epoch >= record.announced_at_epoch:
                return False
            # announce() rebuilds the capability index for this agent_id.
            self._drop_indexes(record.agent_id)
            self._records[record.agent_id] = record
            for cap in record.result_entry.get("capabilities", []):
                self._by_capability.setdefault(cap, set()).add(record.agent_id)
            return True

    def records_peer_needs(
        self, remote_digest: Dict[str, float], *, now: Optional[float] = None
    ) -> List[PresenceRecord]:
        """
        The live records a peer is missing or holds a staler copy of, given
        the peer's digest. This is the delta returned to a gossip sender.
        """
        _now = time.time() if now is None else now
        with self._lock:
            self._sweep_locked(_now)
            out = []
            for aid, rec in self._records.items():
                remote_epoch = remote_digest.get(aid)
                if remote_epoch is None or rec.announced_at_epoch > remote_epoch:
                    out.append(rec)
            return out

    def sweep_expired(self, now: Optional[float] = None) -> int:
        """
        Evict every record past its TTL. Returns the count evicted. Called
        lazily on read and, in coordinator mode, from a background thread.
        """
        _now = time.time() if now is None else now
        with self._lock:
            return self._sweep_locked(_now)

    def _sweep_locked(self, now: float) -> int:
        expired = [
            aid for aid, rec in self._records.items() if rec.is_expired(now)
        ]
        for aid in expired:
            self._drop_indexes(aid)
            self._records.pop(aid, None)
        return len(expired)

    def count(self, *, now: Optional[float] = None) -> int:
        """Live population size (expired records swept first)."""
        _now = time.time() if now is None else now
        with self._lock:
            self._sweep_locked(_now)
            return len(self._records)

    # -- record construction -----------------------------------------

    def build_record(
        self,
        agent_doc: AgentDocument,
        *,
        relay_endpoint: Optional[str] = None,
        visibility: Optional[Visibility] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> PresenceRecord:
        """
        Build a :class:`PresenceRecord` from a hosted AgentDocument.

        The ``result_entry`` is the discovery-safe projection: identity,
        derived capabilities, supported methods, and trust posture — and
        crucially **no network address**. ``manifest_uri`` is the bare
        Agent-ID URI (``agtp://<agent-id>``), which resolves through the
        coordinator/relay rather than to an agent-held endpoint.

        Used both by boot-time self-announce (coordinator mode) and by the
        ANNOUNCE handler.
        """
        caps = sorted(_scopes.derive_capabilities(agent_doc))
        entry = {
            "agent_id": agent_doc.agent_id,
            "manifest_uri": f"agtp://{agent_doc.agent_id}",
            "name": agent_doc.name,
            "supported_methods": list(agent_doc.requires.methods),
            "capabilities": caps,
            "trust_tier": agent_doc.trust_tier,
            "verification_path": agent_doc.verification_path,
        }
        if agent_doc.trust_score is not None:
            entry["behavioral_trust_score"] = agent_doc.trust_score
        if agent_doc.trust_warning:
            entry["trust_warning"] = agent_doc.trust_warning
        if agent_doc.owner_id:
            entry["owner_id"] = agent_doc.owner_id

        attachment = (
            {"mode": "relay-mediated", "relay": relay_endpoint}
            if relay_endpoint
            else None
        )
        return PresenceRecord(
            agent_id=agent_doc.agent_id,
            result_entry=entry,
            scopes=_scopes.default_scopes(agent_doc),
            visibility=visibility or Visibility(),
            owner_domain=agent_doc.owner_id or None,
            attachment=attachment,
            ttl_seconds=ttl_seconds,
        )
