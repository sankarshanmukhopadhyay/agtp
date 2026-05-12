"""
Method-name validation for AGTP.

The protocol's method vocabulary is the curated list at
``core/methods.json``. Validation is a list lookup; this module
exposes the small surface every dispatcher and CLI consults at
request time:

  * :func:`is_approved_verb` — name in the canonical AGTP set?
  * :func:`is_legacy_verb` — name is one of the recognized legacy
    HTTP verbs (GET / POST / PUT / DELETE / PATCH)?
  * :func:`is_embedded_verb` — name is one of the 12 embedded AGTP
    primitives?
  * :func:`categorize` — return the category list a verb belongs to.
  * :func:`get_legacy_preferred` — given a legacy verb, return its
    AGTP-canonical replacement (e.g., GET -> FETCH).
  * :func:`find_close_matches` — for unknown verbs, surface up to N
    close matches by Levenshtein distance against the approved set.

Catalog evolution (Phase 6):

  * :func:`catalog_version` — semver version of the loaded catalog.
  * :func:`catalog_versions_supported` — list of catalog versions
    this implementation can validate against. Today this is just
    ``[catalog_version()]``; multi-version support is future work.
  * :func:`is_deprecated` — verb is admitted but flagged as
    deprecated.
  * :func:`deprecation_metadata` — returns ``{deprecated_in,
    removed_in, successor}`` for a deprecated verb, or ``None``.
  * :class:`CatalogWarning` — warning category emitted by callers
    that observe a verb-from-catalog mismatch (e.g., a custom
    ``@method`` decorator referencing a removed verb).

A method either is or isn't in the list. The 9-pass validator and
composer machinery this module replaces have been retired in favor
of the curated vocabulary.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


_METHODS_PATH = Path(__file__).parent / "methods.json"


def _load_methods_doc() -> Dict[str, Any]:
    """Load and normalize the catalog document.

    The canonical file is ``core/methods.json`` (post-rename). Old
    catalogs used ``core/verbs.json`` and a top-level ``"verbs":``
    key; the loader accepts either top-level key shape so old
    catalog files keep loading after the rename. Going forward the
    build script emits ``"methods":``.
    """
    raw = json.loads(_METHODS_PATH.read_text(encoding="utf-8"))
    # Back-compat: pre-rename catalogs put the per-method dict
    # under the ``verbs`` top-level key. Normalize to ``methods``
    # so internal code has one canonical access path.
    if "methods" not in raw and "verbs" in raw:
        raw["methods"] = raw["verbs"]
    return raw


_METHODS_DOC = _load_methods_doc()

#: All AGTP-approved methods (everything in the curated catalog).
APPROVED_VERBS: Set[str] = set(_METHODS_DOC["methods"].keys())

#: The 12 embedded primitives.
EMBEDDED_VERBS: Set[str] = set(_METHODS_DOC["embedded"])

#: Recognized legacy HTTP methods. Servers admit them only by opt-in
#: via ``[policies.methods]``; the dispatcher otherwise refuses them with
#: 459 plus the preferred replacement in ``error.suggestions``.
LEGACY_VERBS: Set[str] = set(_METHODS_DOC["legacy"].keys())

#: Verbs the protocol layer recognizes regardless of server policy.
#: Servers that opt into legacy verbs add those names to their
#: per-request acceptance check; the registry contents here remain
#: stable.
ALL_PROTOCOL_VERBS: Set[str] = APPROVED_VERBS | EMBEDDED_VERBS


def is_approved_verb(name: str) -> bool:
    """True when ``name`` is in the canonical AGTP verb list."""
    return name.upper() in ALL_PROTOCOL_VERBS


def is_legacy_verb(name: str) -> bool:
    """True when ``name`` is one of the 5 legacy HTTP verbs."""
    return name.upper() in LEGACY_VERBS


def is_embedded_verb(name: str) -> bool:
    """True when ``name`` is one of the 12 embedded AGTP primitives."""
    return name.upper() in EMBEDDED_VERBS


def categorize(name: str) -> Optional[List[str]]:
    """
    Return the categories ``name`` belongs to, or ``None`` if the verb
    is not in the catalog. The list preserves the canonical-source
    ordering (so the first entry is the primary category).
    """
    entry = _METHODS_DOC["methods"].get(name.upper())
    if entry is None:
        return None
    return list(entry["categories"])


def describe(name: str) -> Optional[str]:
    """Return the canonical one-line description for ``name``."""
    entry = _METHODS_DOC["methods"].get(name.upper())
    if entry is None:
        return None
    return str(entry.get("description", ""))


def get_legacy_preferred(name: str) -> Optional[str]:
    """
    For a legacy verb, return its AGTP-canonical replacement (e.g.,
    ``GET -> FETCH``). Returns ``None`` for non-legacy names.
    """
    entry = _METHODS_DOC["legacy"].get(name.upper())
    if entry is None:
        return None
    return str(entry.get("preferred")) or None


# ---------------------------------------------------------------------------
# Typo-tolerant suggestions for 459 responses.
# ---------------------------------------------------------------------------


def _levenshtein(a: str, b: str) -> int:
    """Iterative Levenshtein distance; small inputs only."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                prev[j] + 1,         # deletion
                curr[j - 1] + 1,     # insertion
                prev[j - 1] + cost,  # substitution
            )
        prev = curr
    return prev[-1]


def find_close_matches(
    name: str,
    *,
    max_distance: int = 2,
    limit: int = 3,
) -> List[str]:
    """
    Return up to ``limit`` close matches for ``name`` from the
    approved-verb set, ranked by Levenshtein distance. Used by the
    459 response builder to nudge typos toward canonical verbs.

    For legacy verbs (``GET`` etc.), the preferred replacement is
    listed first regardless of distance — operators care that the
    suggestion mirrors the registry's intent.
    """
    upper = name.upper()
    out: List[str] = []
    if upper in LEGACY_VERBS:
        preferred = get_legacy_preferred(upper)
        if preferred:
            out.append(preferred)
    scored: List[tuple] = []
    universe = ALL_PROTOCOL_VERBS
    for cand in universe:
        if cand == upper:
            continue
        d = _levenshtein(upper, cand)
        if d <= max_distance:
            scored.append((d, cand))
    scored.sort()
    for _, cand in scored:
        if cand not in out:
            out.append(cand)
        if len(out) >= limit:
            break
    return out[:limit]


# ---------------------------------------------------------------------------
# Catalog version + per-verb deprecation (Phase 6).
# ---------------------------------------------------------------------------


class CatalogWarning(DeprecationWarning):
    """
    Emitted when something on the server side observes a verb-name
    mismatch against the loaded catalog: a custom-method decorator
    references a verb the catalog has removed, a ``[policies.methods]``
    directive names an unknown verb, etc. Subclasses
    :class:`DeprecationWarning` so callers can opt into stricter
    handling via ``warnings.filterwarnings("error", category=...)``.
    """


def catalog_version() -> str:
    """
    Return the semver version of the loaded catalog (e.g.
    ``"1.0.0"``). Always reads from ``_METHODS_DOC`` so tests that
    monkey-patch the document see their override.
    """
    return str(_METHODS_DOC.get("version") or "1.0.0")


def catalog_versions_supported() -> List[str]:
    """
    Return the list of catalog versions this implementation can
    validate against. Phase 6 ships single-version support — the
    list contains exactly one entry, the current
    :func:`catalog_version`. Multi-version support (a server that
    can validate against both ``1.x`` and ``2.x`` catalogs
    simultaneously during a migration) is future work; the field
    rides on the manifest now so clients can read it without
    breaking when that capability lands.
    """
    return [catalog_version()]


def is_deprecated(name: str) -> bool:
    """
    True when ``name`` is in the catalog AND carries deprecation
    metadata. A deprecated verb is still admitted by
    :func:`is_approved_verb` (deprecation does not remove a verb
    from the catalog); the dispatcher surfaces a warning header
    on each invocation so callers know to migrate.

    Returns ``False`` for verbs that aren't in the catalog at all
    — those are *removed*, not deprecated. Use
    :func:`is_approved_verb` to distinguish admission from
    deprecation status.
    """
    entry = _METHODS_DOC.get("methods", {}).get(name.upper())
    if not isinstance(entry, dict):
        return False
    return "deprecated_in" in entry


def deprecation_metadata(name: str) -> Optional[Dict[str, Optional[str]]]:
    """
    Return the deprecation metadata dict for ``name`` or ``None``.

    The dict shape::

        {
            "deprecated_in": "1.1.0",
            "removed_in": "2.0.0" | None,
            "successor":  "AUDIT" | None,
        }

    All three fields are present in the dict; values that aren't
    declared in the catalog come back as ``None`` so callers don't
    need to ``.get(...)`` defensively.
    """
    entry = _METHODS_DOC.get("methods", {}).get(name.upper())
    if not isinstance(entry, dict) or "deprecated_in" not in entry:
        return None
    return {
        "deprecated_in": str(entry["deprecated_in"]),
        "removed_in": (
            str(entry["removed_in"]) if entry.get("removed_in") else None
        ),
        "successor": (
            str(entry["successor"]).upper() if entry.get("successor") else None
        ),
    }


__all__ = [
    "ALL_PROTOCOL_VERBS",
    "APPROVED_VERBS",
    "CatalogWarning",
    "EMBEDDED_VERBS",
    "LEGACY_VERBS",
    "catalog_version",
    "catalog_versions_supported",
    "categorize",
    "deprecation_metadata",
    "describe",
    "find_close_matches",
    "get_legacy_preferred",
    "is_approved_verb",
    "is_deprecated",
    "is_embedded_verb",
    "is_legacy_verb",
]
