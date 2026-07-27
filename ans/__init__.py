"""
AGTP Agent Name Service (ANS) — governed name-to-Agent-ID resolution.

ANS is architecturally analogous to DNS for the web: it resolves
human-readable names to Canonical Agent-IDs and brokers capability
queries, enforcing trust-tier and behavioral-trust floors as a
Scope-Enforcement Point. An ANS server is itself an AGTP agent with its
own identity and governance signing key.

In this implementation an ANS server is a presence coordinator (it holds a
:class:`presence.store.PresenceStore`) *plus* a :class:`ans.store.NameStore`.
That composition lets the ANS reuse the entire presence DISCOVER pipeline —
partition filters, trust filters, composite ranking, and the
``ans_signature`` envelope — for its brokered capability queries, while the
name store adds name↔Agent-ID resolution on top.

Registration is a consequence of ACTIVATE (PDD §6.2): the hosting daemon's
lifecycle hook submits a REGISTER to its configured ANS endpoints, and
deregisters urgently (within 60s — synchronously, here) on a transition to
Suspended / Revoked / Deprecated. Manual end-user registration is not a
path; REGISTER is the platform's automated submission channel.

Deferred (later slices): cross-ANS federation (M4), and DESCRIBE-driven
index refresh over the wire (the freshness bookkeeping exists here; the
24h wire-refresh loop is scaffolded, not run).
"""

from ans.store import NameBinding, NameStore

__all__ = ["NameBinding", "NameStore"]
