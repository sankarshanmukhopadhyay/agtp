"""
AGTP Presence — substrate-level ambient discovery.

Presence inverts the pull-based discovery model: when an agent joins the
substrate it announces its intrinsic posture and becomes ambiently
visible to the relevant scope of peers, without a separate publishing
step. "Showing up is the registration" (the DHCP analogy from the PDD
§1.4).

This package is the M1 slice of that design (see the AGTP Agent
Discovery PDD, milestone M1):

  * :mod:`presence.records`  — the on-the-wire presence record shape.
  * :mod:`presence.scopes`   — scope tuples and capability derivation.
  * :mod:`presence.store`    — the in-memory coordinator state.
  * :mod:`presence.methods`  — ANNOUNCE / WITHDRAW / PROBE handlers and
                               the ``DISCOVER /population`` query.

M1 deliberately omits (deferred to later milestones):

  * the Kademlia DHT and gossip convergence (M2/M4) — a single
    coordinator makes routing trivial at demo scale;
  * TTL aging and WITHDRAW-driven eviction sweeps (M2);
  * the cryptographic visibility model and signature verification (M2/M3)
    — records carry a signature slot but the coordinator does not verify
    it yet, matching the v00 "zero crypto on this path" posture;
  * ANS brokering, ranking, and cross-scope resolution (M3/M4).

The coordinator is an ordinary AGTP server (``python -m server
--presence``) with a :class:`presence.store.PresenceStore` attached. The
server's connection handler routes presence verbs and the
``/population`` DISCOVER path to :func:`presence.methods.maybe_handle_presence`
only when that store is present, so a plain agent server is unaffected.
"""

from presence.records import PresenceRecord, Visibility
from presence.store import PresenceStore

__all__ = ["PresenceRecord", "Visibility", "PresenceStore"]
