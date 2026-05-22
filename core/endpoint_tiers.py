"""
AGTP endpoint tier classification.

Every (method, path) endpoint on an AGTP server falls into one of
three tiers. The protocol uses this distinction throughout — for
authority gating, override semantics, RCNS dispatch, and
debugability — so the classification needs a single source of
truth.

Tier A — Native (protocol)
~~~~~~~~~~~~~~~~~~~~~~~~~~

Endpoints the daemon implements directly. They are guaranteed by
every conformant AGTP server, cannot be turned off by operator
policy, and bypass operator gates (``[policies.methods]`` allow /
disallow / redirects don't apply). They are the protocol's
self-describing surface.

Examples shipping today:

  * ``DISCOVER /`` — the endpoint directory.
  * ``DISCOVER /agents`` — agents the server hosts.
  * ``DISCOVER /methods`` — endpoint inventory.
  * ``DISCOVER /tools`` — tools the server exposes.
  * ``DISCOVER /apis`` — external APIs the server fronts.
  * ``DISCOVER /genesis`` — the agent's Genesis (when loaded).
  * ``QUERY /proposals`` — §7 async PROPOSE poll.
  * Lifecycle methods (ACTIVATE / DEACTIVATE / REVOKE /
    REINSTATE / DEPRECATE) on Agent-ID targets.
  * INSPECT with ``target=`` families.
  * PROPOSE itself.

Tier B — Application (registry)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Endpoints declared by operator-authored TOML or contributed by
runtime / operational modules. They live in the
:class:`server.endpoint_registry.EndpointRegistry` and the
operator decides whether they ship, what their schema is, how
they authorize. They can be allowed / disallowed / redirected
through ``[policies.methods]``.

Examples:

  * ``DISCOVER /products`` from an operator's ``endpoints.toml``.
  * ``PURCHASE /checkout`` from ``mod_merchant``.
  * ``RECONCILE /accounts/{id}`` from a runtime module.

Tier C — RCNS (negotiated; future)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Endpoints that don't exist at request time but that the daemon
can synthesize on demand via the runtime contract-negotiation
substrate. Tier C is bound for the lifetime of a synthesis_id
and is otherwise identical to Tier B at execution time — same
authority gates, same audit trail. The *origin* differs: a Tier
C binding is created by the dispatcher in response to a request,
not at startup.

This module classifies an endpoint as Tier C only when an
explicit RCNS dispatch has produced a synthesis_id for the
(method, path) pair. Plain "no such endpoint" responses (404)
remain unregistered, not Tier C — Tier C is a positive claim
that the daemon negotiated a contract.

Classification function
~~~~~~~~~~~~~~~~~~~~~~~

:func:`classify_tier` is the canonical lookup. It consults this
module's reserved inventory first, then the supplied registry
(checking handlers for the ``__agtp_builtin__`` marker that
:mod:`server.builtins` attaches), then falls through to
``unregistered``. Callers wiring RCNS in later phases will pass
an additional synthesis lookup parameter to surface Tier C.
"""

from __future__ import annotations

from typing import Any, FrozenSet, Iterable, List, Optional, Tuple


#: Endpoints the daemon answers regardless of operator configuration.
#: This is the protocol-reserved surface. Items here have to be
#: implemented by every conformant AGTP server — adding to this set
#: is a protocol-level decision, not an operator choice.
#:
#: Some entries are dispatched directly by the daemon (the reserved
#: DISCOVER roots, lifecycle methods); others are registered into the
#: endpoint registry as built-ins by :mod:`server.builtins`. Both
#: sources count as Tier A — what makes them Tier A is the protocol
#: guarantee, not the implementation mechanism.
TIER_A_RESERVED_ENDPOINTS: FrozenSet[Tuple[str, str]] = frozenset({
    # DISCOVER directory + reserved roots (T4.1).
    ("DISCOVER", "/"),
    ("DISCOVER", "/methods"),
    ("DISCOVER", "/agents"),
    ("DISCOVER", "/tools"),
    ("DISCOVER", "/apis"),
    ("DISCOVER", "/genesis"),
    # RCNS-4 observability surfaces.
    ("DISCOVER", "/patterns"),
    ("DISCOVER", "/contracts"),
    # §7 PROPOSE async-poll surface.
    ("QUERY", "/proposals"),
})


#: Sentinel attribute :mod:`server.builtins` attaches to its handler
#: closures so this module can distinguish Tier A registrations from
#: Tier B ones at classification time.
BUILTIN_HANDLER_MARKER = "__agtp_builtin__"


# Tier labels — exported as constants so call sites don't sprinkle
# magic strings.
TIER_NATIVE         = "A"
TIER_APPLICATION    = "B"
TIER_RCNS           = "C"
TIER_UNREGISTERED   = "unregistered"


def _normalize(method: str, path: str) -> Tuple[str, str]:
    """Canonicalize a (method, path) key for inventory comparison.

    Methods uppercase per AGTP convention; paths are taken verbatim
    (the path grammar prevents trailing-slash ambiguity, so equality
    on the raw string is correct).
    """
    return (str(method).upper(), str(path))


def classify_tier(
    method: str,
    path: str,
    *,
    registry: Optional[Any] = None,
    synthesis_lookup: Optional[Any] = None,
) -> str:
    """Return the tier label for the given (method, path) pair.

    Lookup order:

      1. **Tier A reserved inventory** — if the pair matches a
         protocol-reserved entry, return ``"A"`` immediately.
         These are guaranteed regardless of operator config.
      2. **Tier A registered builtin** — if ``registry`` resolves
         the pair and the resolved handler carries the
         ``__agtp_builtin__`` marker, return ``"A"``.
      3. **Tier B registered endpoint** — if ``registry`` resolves
         the pair without the builtin marker, return ``"B"``.
      4. **Tier C RCNS contract (future)** — if ``synthesis_lookup``
         resolves the pair to a live synthesis, return ``"C"``.
         RCNS phases land this hook; pre-RCNS the parameter is
         always ``None`` and step 4 is a no-op.
      5. Otherwise return ``"unregistered"`` — the caller is free
         to treat that as 404 today and as RCNS-eligible later.

    ``registry`` is duck-typed: the function only needs ``lookup``
    returning either ``None`` or a ``(spec, handler)`` pair (the
    shape :class:`server.endpoint_registry.EndpointRegistry`
    provides).

    ``synthesis_lookup`` is reserved for RCNS-3. It must expose a
    ``resolve(method, path)`` method returning a synthesis record
    or ``None``. Until then the parameter is unused.
    """
    key = _normalize(method, path)

    if key in TIER_A_RESERVED_ENDPOINTS:
        return TIER_NATIVE

    if registry is not None:
        resolved = registry.lookup(*key)
        if resolved is not None:
            # ``lookup`` returns either a tuple (spec, handler) or a
            # single handler depending on registry version; pull the
            # handler out either way.
            handler = resolved
            if isinstance(resolved, tuple) and len(resolved) >= 2:
                handler = resolved[1]
            if hasattr(handler, BUILTIN_HANDLER_MARKER):
                return TIER_NATIVE
            return TIER_APPLICATION

    if synthesis_lookup is not None:
        try:
            record = synthesis_lookup.resolve(*key)
        except AttributeError:
            record = None
        if record is not None:
            return TIER_RCNS

    return TIER_UNREGISTERED


def tier_a_inventory(registry: Optional[Any] = None) -> List[Tuple[str, str]]:
    """Return the full Tier A inventory on this server.

    Combines :data:`TIER_A_RESERVED_ENDPOINTS` (protocol-reserved)
    with any registry entries whose handlers carry the
    ``__agtp_builtin__`` marker (Tier A registered builtins).

    Sorted by ``(method, path)`` for stable output suitable for
    DISCOVER index entries, tests, and operator inspection.
    """
    items = set(TIER_A_RESERVED_ENDPOINTS)
    if registry is not None and hasattr(registry, "all_endpoints"):
        try:
            entries: Iterable = registry.all_endpoints()
        except Exception:
            entries = ()
        for entry in entries:
            # Registry entries are either (spec, handler) tuples or
            # spec-only objects; handle both shapes defensively.
            spec = entry
            handler: Any = None
            if isinstance(entry, tuple):
                if len(entry) >= 1:
                    spec = entry[0]
                if len(entry) >= 2:
                    handler = entry[1]
            method = getattr(spec, "name", None) or getattr(spec, "method", "")
            path = getattr(spec, "path", "")
            if not method or not path:
                continue
            if handler is not None and hasattr(handler, BUILTIN_HANDLER_MARKER):
                items.add(_normalize(method, path))
    return sorted(items)


__all__ = [
    "BUILTIN_HANDLER_MARKER",
    "TIER_APPLICATION",
    "TIER_A_RESERVED_ENDPOINTS",
    "TIER_NATIVE",
    "TIER_RCNS",
    "TIER_UNREGISTERED",
    "classify_tier",
    "tier_a_inventory",
]
