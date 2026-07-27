"""
ADS — Anticipatory Discovery Service (PDD §6.5, forward-looking).

ADS is the priming layer above search and discovery: it observes an agent's
operational signals, predicts the categories of agents it is likely to need
next, and preloads awareness (via cross-scope discovery) *before* the
explicit query arrives — shortening the path from need to match.

This is a **prototype**, not a normative specification. It exercises the
shape the draft sketches — signal capture, a minimal prediction interface,
and proactive preloading — over the discovery substrate built in M1–M4.

Two privacy invariants are built in, not bolted on:
  * **Within-visibility only.** A prediction resolves only to agents the
    observed agent could already see: preloading queries carry the agent's
    own identity, so the substrate's visibility filtering applies. ADS
    expands discovery *within* existing access; it never grants new access.
  * **No cross-agent reconstruction.** Signals and predictions are
    per-agent; the service never pools one agent's pattern into another's
    prediction.

Layout:
  * :mod:`ads.signals`   — the per-agent signal store (opt-in).
  * :mod:`ads.predictor` — co-occurrence + frequency prediction.
  * :mod:`ads.service`   — subscribe / observe / predict / preload.
"""

from ads.service import AnticipatoryDiscoveryService
from ads.signals import SignalStore

__all__ = ["AnticipatoryDiscoveryService", "SignalStore"]
