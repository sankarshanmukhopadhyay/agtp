"""
AGTP path-grammar validator.

Paths and verbs are orthogonal in AGTP: the verb is the *action*,
the path is the *object*. The path grammar is deliberately minimal
— it rejects two failure modes and leaves the rest to operator
judgment:

  1. Structural: the path must begin with ``/`` and (except for the
     bare root) must not have a trailing slash.
  2. Verb-in-path: no path segment, after stripping ``-`` and ``_``
     and uppercasing, may match a verb in the AGTP catalog. Verbs
     belong in the method, not the path. ``/get/orders`` is wrong;
     ``GET /orders`` (or, post-AGTP, ``FETCH /orders``) is right.

Casing conventions, kebab-vs-snake-case, segment depth, and
parameter naming are *not* enforced — operators are trusted to
choose them.

Parameterized segments wrapped in braces (``{order_id}``) are
exempt from the verb-in-path check, since their text is variable.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from core.methods import APPROVED_VERBS, LEGACY_VERBS


#: Verbs the path grammar refuses to see in URI segments. Includes
#: legacy verbs even when the server policy has not opted into them
#: — a server that decides to admit ``GET`` later does not want
#: ``/get/orders`` paths to have shipped in the meantime.
PATH_PROTOCOL_VERBS = APPROVED_VERBS | LEGACY_VERBS


#: Reserved DISCOVER roots — protocol-level paths every AGTP daemon
#: must answer to identically. Custom DISCOVER endpoints (operator-
#: registered ``DISCOVER /products``, ``DISCOVER /projects``, etc.)
#: MUST NOT collide with these by starting with a reserved-root name
#: (per the T4.1 design). The rule blocks the obvious collisions
#: (``/agents``) plus prefix-shadowing (``/agents-products``,
#: ``/methodsv2``) that would invite confusion.
DISCOVER_RESERVED_ROOTS = frozenset({
    "agents", "methods", "tools", "apis", "genesis",
})


class PathGrammarError(ValueError):
    """
    Raised when a path violates AGTP path grammar.

    ``code`` is a stable error tag (``invalid-format`` or
    ``verb-in-path``) suitable for branching in dispatcher logic and
    surfacing in 460 response bodies. ``segment`` is the offending
    segment when the failure mode names one; otherwise ``None``.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        segment: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.segment = segment


def validate_path(path: str) -> None:
    """
    Validate an AGTP endpoint path. Returns ``None`` on success;
    raises :class:`PathGrammarError` on failure.

    Rules:

      * Path must begin with ``/``.
      * Path must not have a trailing slash unless the path is
        exactly ``/`` (the server-root form).
      * No path segment may match an approved or legacy AGTP verb,
        case-insensitive, after stripping ``-`` and ``_``.
        Parameterized segments (``{...}``) are exempt.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        raise PathGrammarError("invalid-format", "Path must begin with '/'")
    if path != "/" and path.endswith("/"):
        raise PathGrammarError(
            "invalid-format", "Path must not have a trailing slash"
        )

    # urlparse normalizes the path component; we only care about the
    # path itself, not query/fragment, but reject anything weird up
    # front.
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        raise PathGrammarError(
            "invalid-format",
            "Path must be a relative URI path, not an absolute URL",
        )
    canonical = parsed.path
    if canonical != path.split("?", 1)[0].split("#", 1)[0]:
        # parsed.path strips trailing chars only when fed a malformed
        # input. Keep things strict.
        pass

    segments = [s for s in canonical.split("/") if s]
    for segment in segments:
        # Parameterized segment ({order_id}) — content is variable;
        # operators choose the parameter name freely.
        if segment.startswith("{") and segment.endswith("}"):
            continue
        normalized = segment.upper().replace("-", "").replace("_", "")
        if normalized in PATH_PROTOCOL_VERBS:
            raise PathGrammarError(
                "verb-in-path",
                (
                    f"Path segment {segment!r} contains a recognized AGTP "
                    f"verb. Verbs belong in the method, not the path."
                ),
                segment=segment,
            )


def validate_discover_path(path: str) -> None:
    """Validate the path token on a custom (operator-registered)
    ``DISCOVER`` endpoint.

    Custom paths MAY use any first segment that does not start with
    one of the protocol-reserved roots (``agents``, ``methods``,
    ``tools``, ``apis``, ``genesis``). Exact-match reserved paths
    (``/agents``, ``/methods``, etc.) are handled by the daemon's
    hardcoded protocol routes and aren't passed through this
    validator — callers register only their custom endpoints.

    Examples that PASS:
      ``/products``, ``/projects``, ``/catalog``,
      ``/customers/active``

    Examples that FAIL with ``discover-reserved-prefix``:
      ``/agents-products``  (starts with ``agents``)
      ``/methodsv2``        (starts with ``methods``)
      ``/genesis-archive``  (starts with ``genesis``)

    Path-grammar invariants (start with ``/``, no trailing slash,
    no verb-in-path) are enforced first via :func:`validate_path`.
    """
    validate_path(path)
    segments = [s for s in path.split("/") if s]
    if not segments:
        return  # bare root — server-manifest case, daemon handles
    first = segments[0].lower()
    # Skip parameterized first segments — operators expressing
    # ``DISCOVER /{tenant_id}/products`` aren't shadowing a reserved
    # root; the variable is filled in at request time.
    if first.startswith("{") and first.endswith("}"):
        return
    # Exact-match reserved roots are protocol-owned. The DISCOVER
    # handler short-circuits them before the validator sees the path,
    # but accept them here too so the function is safe to call on any
    # candidate DISCOVER path.
    if first in DISCOVER_RESERVED_ROOTS:
        return
    for root in DISCOVER_RESERVED_ROOTS:
        if first.startswith(root):
            raise PathGrammarError(
                "discover-reserved-prefix",
                (
                    f"DISCOVER path segment {first!r} collides with "
                    f"reserved root {root!r}. Operators must choose a "
                    f"first segment that does not begin with any of: "
                    f"{sorted(DISCOVER_RESERVED_ROOTS)}."
                ),
                segment=first,
            )


__all__ = [
    "DISCOVER_RESERVED_ROOTS",
    "PATH_PROTOCOL_VERBS",
    "PathGrammarError",
    "validate_discover_path",
    "validate_path",
]
