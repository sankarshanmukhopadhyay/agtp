"""
Scope tuples and capability derivation.

Two jobs, both drawn from the PDD §6.1 "Capability derivation" and §7.5
"Scope tuple":

1. **Capability derivation.** The capability a presence query filters on
   is *derived from the method library*, not self-declared. An agent may
   only claim a capability it has actually bound the constituent methods
   for. The filtering vocabulary and the partition vocabulary are the same
   vocabulary (the versioned catalog at ``core/methods.json``), so drift
   between "what you can be found under" and "what you can do" is
   structurally impossible. This is also the M3 index-poisoning defense,
   built once here: an unbound capability cannot join an overlay, so a
   false capability claim cannot inflate match rates.

2. **Scope tuples.** A scope is a composite key over partition dimensions
   (tier, owner-domain, capability, industry, region). The tuple
   canonicalizes to a stable overlay identifier. M1 runs a single
   coordinator so the overlay id is not yet used for routing, but records
   already carry their scope strings so M2/M4 partitioning is a pure
   additive change.
"""

from __future__ import annotations

import hashlib
from typing import Dict, List, Optional, Set

from core import methods as _catalog
from core.identity import AgentDocument


#: The partition dimensions, in canonical order (PDD §7.5). ``capability``
#: is the derived dimension; the others are attributes carried on the
#: agent's identity/certificate.
SCOPE_DIMENSIONS = ("tier", "owner-domain", "capability", "industry", "region")


def derive_capabilities_from_methods(methods, *, wildcards: bool = False) -> Set[str]:
    """
    Derive capability tokens from a list of bound method names. A token is
    the lowercase method name plus the lowercase catalog categories that
    method belongs to. Wildcards derive the full catalog vocabulary.

    Shared by :func:`derive_capabilities` (AgentDocument) and the ANS
    REGISTER path (a submitted manifest's ``supported_methods``).
    """
    if wildcards:
        caps = {v.lower() for v in _catalog.APPROVED_VERBS}
        caps |= _catalog_categories()
        return caps

    caps: Set[str] = set()
    for verb in methods or []:
        token = str(verb).strip().lower()
        if not token:
            continue
        caps.add(token)
        for cat in _catalog.categorize(str(verb).strip().upper()):
            caps.add(cat.lower())
    return caps


def derive_capabilities(agent_doc: AgentDocument) -> Set[str]:
    """
    The set of capability tokens ``agent_doc`` may legitimately be found
    under, derived from the methods it has bound (see
    :func:`derive_capabilities_from_methods`). Matching a method name lets
    a query say ``capability=validate``; matching a category lets a coarser
    query say ``capability=discovery``. Wildcard agents derive the full
    catalog vocabulary.
    """
    return derive_capabilities_from_methods(
        agent_doc.requires.methods, wildcards=agent_doc.requires.wildcards
    )


def agent_may_claim(agent_doc: AgentDocument, capability: str) -> bool:
    """True iff ``agent_doc`` has bound the methods backing ``capability``."""
    return capability.strip().lower() in derive_capabilities(agent_doc)


def _catalog_categories() -> Set[str]:
    cats: Set[str] = set()
    for verb in _catalog.APPROVED_VERBS:
        for cat in _catalog.categorize(verb):
            cats.add(cat.lower())
    return cats


def scope_tuple(
    *,
    tier: Optional[int] = None,
    owner_domain: Optional[str] = None,
    capability: Optional[str] = None,
    industry: Optional[str] = None,
    region: Optional[str] = None,
) -> str:
    """
    Render a partition tuple in the canonical string form used in
    presence records (PDD §7.5), e.g. ``{capability: booking, region: us}``.

    Only the dimensions supplied are included; ordering follows
    :data:`SCOPE_DIMENSIONS` so the same logical scope always renders
    identically (and therefore hashes identically in :func:`overlay_id`).
    """
    values: Dict[str, str] = {}
    if tier is not None:
        values["tier"] = str(tier)
    if owner_domain:
        values["owner-domain"] = owner_domain.lower()
    if capability:
        values["capability"] = capability.strip().lower()
    if industry:
        values["industry"] = industry.strip().lower()
    if region:
        values["region"] = region.strip().lower()

    parts = [
        f"{dim}: {values[dim]}"
        for dim in SCOPE_DIMENSIONS
        if dim in values
    ]
    return "{" + ", ".join(parts) + "}"


def overlay_id(scope: str) -> str:
    """
    Stable overlay identifier for a scope tuple: the hex SHA-256 of its
    canonical string form. Not consulted for routing in M1 (single
    coordinator); defined now so M2/M4 partitioning keys off it.
    """
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()


def default_scopes(agent_doc: AgentDocument) -> List[str]:
    """
    The scope tuples an agent joins by default at announce time: one
    per derived capability, plus a tier-only scope. M1 uses these only to
    populate the record's ``scopes`` field; M2 uses them for overlay
    partitioning.
    """
    scopes = [scope_tuple(tier=agent_doc.trust_tier)]
    for cap in sorted(derive_capabilities(agent_doc)):
        scopes.append(scope_tuple(capability=cap))
    return scopes
