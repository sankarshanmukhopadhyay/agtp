"""
Reserved and prohibited method names.

Three lists, each a frozenset for O(1) lookup:

  * ``HTTP_METHODS``     - cannot be used by AGTP at all. Reusing
                           HTTP verbs would silently conflate two
                           different protocols' semantics.
  * ``EMBEDDED_METHODS`` - the 12 verbs baked into AGTP itself. No
                           user-defined (source=amg/1.0) method may
                           register one of these names.
  * ``STOPLIST``         - heuristic noun/adjective/state filter.
                           Names on this list are not action-intent
                           verbs, so they fail the semantic intent
                           check at Pass 4.

The ecosystem catalog (future work) is the authoritative extension
point for STOPLIST and EMBEDDED_METHODS. The lists in this module
are the seed.
"""

from __future__ import annotations

from typing import Optional


HTTP_METHODS = frozenset({
    "GET", "POST", "PUT", "DELETE", "PATCH",
    "HEAD", "OPTIONS", "CONNECT", "TRACE",
})


EMBEDDED_METHODS = frozenset({
    # Cognitive (six)
    "QUERY", "DISCOVER", "DESCRIBE", "SUMMARIZE", "PLAN", "EXECUTE",
    # Mechanics (six)
    "DELEGATE", "ESCALATE", "CONFIRM", "SUSPEND", "PROPOSE", "NOTIFY",
})


STOPLIST = frozenset({
    "AVAILABLE", "ACTIVE", "EXISTS", "STATUS", "DATA", "INFO",
    "PRESENT", "READY", "ENABLED", "VALID", "TRUE", "FALSE",
    "OBJECT", "ENTITY", "ITEM", "RECORD",
})


# Suggestion text used by the validator when a name is rejected at
# Pass 4. Keyed on the offending name; falls back to a generic hint.
STOPLIST_SUGGESTIONS = {
    "STATUS":    "Consider an action verb like CHECK or REPORT.",
    "DATA":      "Consider an action verb like FETCH or RETRIEVE.",
    "INFO":      "Consider an action verb like DESCRIBE or SUMMARIZE.",
    "AVAILABLE": "Consider an action verb like CHECK or QUERY.",
    "ACTIVE":    "Consider an action verb like ACTIVATE or VERIFY.",
    "EXISTS":    "Consider an action verb like CHECK or VERIFY.",
    "PRESENT":   "Consider an action verb like CHECK or DETECT.",
    "READY":     "Consider an action verb like PREPARE or CONFIRM.",
    "ENABLED":   "Consider an action verb like ENABLE or ACTIVATE.",
    "VALID":     "Consider an action verb like VALIDATE or VERIFY.",
    "TRUE":      "Boolean values are not method verbs.",
    "FALSE":     "Boolean values are not method verbs.",
    "OBJECT":    "Method names should be verbs, not nouns.",
    "ENTITY":    "Method names should be verbs, not nouns.",
    "ITEM":      "Method names should be verbs, not nouns.",
    "RECORD":    "Method names should be verbs, not nouns (or use REGISTER if that's the intent).",
}


def is_reserved(name: str) -> Optional[str]:
    """
    Return a human-readable reason ``name`` is reserved, or ``None``
    when the name is free.

    The reason strings are stable; tools key off them.
    """
    upper = (name or "").upper()
    if upper in HTTP_METHODS:
        return f"reserved as an HTTP method ({upper})"
    if upper in EMBEDDED_METHODS:
        return f"reserved as an embedded AGTP method ({upper})"
    return None


def stoplist_suggestion(name: str) -> str:
    """Pick a human-readable hint for a name that landed on the stoplist."""
    upper = (name or "").upper()
    return STOPLIST_SUGGESTIONS.get(
        upper,
        "Method names should be action-intent verbs, not nouns or states.",
    )


__all__ = [
    "EMBEDDED_METHODS",
    "HTTP_METHODS",
    "STOPLIST",
    "STOPLIST_SUGGESTIONS",
    "is_reserved",
    "stoplist_suggestion",
]
