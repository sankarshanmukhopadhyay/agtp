"""
Client-side matching handshake.

Compares an Agent Document's ``requires.methods`` against the methods
universe a Server Manifest advertises and reports one of three
outcomes:

  * ``full``   - every required method is present on the server.
  * ``partial`` - some required methods are present, some are missing.
  * ``none``   - no required method is present.

This is a pre-flight check used by clients (and elemen) to decide
whether to invoke at all, to fall back to PROPOSE, or to escalate to
the human principal. The handshake itself does not invoke anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List

from core.identity import AgentDocument
from server.manifest import ServerManifest


@dataclass
class MatchOutcome:
    """Result of comparing an agent's requires against a manifest."""

    kind: str                              # "full" | "partial" | "none"
    matched: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    server_methods_universe: List[str] = field(default_factory=list)
    agent_wants_wildcards: bool = False
    server_accepts_wildcards: bool = True

    @property
    def is_actionable(self) -> bool:
        """True when at least one required method is reachable."""
        return self.kind in ("full", "partial")

    def summary_line(self) -> str:
        return (
            f"Match: {self.kind.upper()}  "
            f"(matched={len(self.matched)} missing={len(self.missing)} "
            f"universe={len(self.server_methods_universe)})"
        )


def _names_from_manifest(manifest: ServerManifest) -> List[str]:
    """Flatten embedded + custom names into a sorted unique list."""
    names: set[str] = set()
    for entry in manifest.embedded_methods:
        names.add(entry["name"])
    for entry in manifest.custom_methods:
        names.add(entry["name"])
    return sorted(names)


def _match_for_wildcards(
    agent_methods: List[str],
    universe: List[str],
    *,
    agent_wildcards: bool,
    server_accepts_wildcards: bool,
) -> MatchOutcome:
    """
    Special case: if the agent declares wildcards and the server
    accepts them, every server-exposed method is reachable. Outcome
    is ``full`` regardless of the explicit ``requires.methods`` list.
    If the server refuses wildcards, the agent's explicit list is the
    only thing it can reach, and we fall through to the normal path.
    """
    if not agent_wildcards or not server_accepts_wildcards:
        return _explicit_match(
            agent_methods,
            universe,
            agent_wildcards=agent_wildcards,
            server_accepts_wildcards=server_accepts_wildcards,
        )
    return MatchOutcome(
        kind="full",
        matched=list(universe),
        missing=[],
        server_methods_universe=list(universe),
        agent_wants_wildcards=True,
        server_accepts_wildcards=True,
    )


def _explicit_match(
    agent_methods: List[str],
    universe: List[str],
    *,
    agent_wildcards: bool,
    server_accepts_wildcards: bool,
) -> MatchOutcome:
    universe_set = set(universe)
    needs = list(dict.fromkeys(agent_methods))  # de-dup, preserve order
    matched = sorted(m for m in needs if m in universe_set)
    missing = sorted(m for m in needs if m not in universe_set)

    if not needs:
        # Edge case: an agent that declares no methods. Treat as
        # vacuously full when the server has anything to offer.
        kind = "full" if universe else "none"
    elif not missing:
        kind = "full"
    elif not matched:
        kind = "none"
    else:
        kind = "partial"

    return MatchOutcome(
        kind=kind,
        matched=matched,
        missing=missing,
        server_methods_universe=list(universe),
        agent_wants_wildcards=agent_wildcards,
        server_accepts_wildcards=server_accepts_wildcards,
    )


def match(agent: AgentDocument, manifest: ServerManifest) -> MatchOutcome:
    """
    Compare ``agent.requires.methods`` against the manifest's method
    universe and return an outcome object suitable for human display
    or programmatic decisions.
    """
    universe = _names_from_manifest(manifest)
    return _match_for_wildcards(
        list(agent.requires.methods),
        universe,
        agent_wildcards=bool(agent.requires.wildcards),
        server_accepts_wildcards=bool(
            manifest.policies.wildcards_accepted
            if manifest.policies is not None
            else False
        ),
    )


def match_from_manifest_dict(
    agent: AgentDocument, manifest: dict
) -> MatchOutcome:
    """
    Convenience for clients that already hold the manifest as parsed
    JSON (the wire form). Avoids reconstructing the ServerManifest
    dataclass when only the matching outcome is needed.

    Accepts both the post-§5 wire shape (top-level
    ``embedded_methods`` / ``custom_methods`` / ``policies``) and the
    pre-§5 shape (nested ``methods.embedded`` / ``methods.custom`` /
    ``policy``) so a client running against an older server still
    resolves a useful outcome.
    """
    # Method universe: prefer the top-level arrays; fall back to the
    # legacy nested ``methods`` block.
    embedded = manifest.get("embedded_methods")
    custom = manifest.get("custom_methods")
    if embedded is None and custom is None:
        legacy_methods = manifest.get("methods", {}) or {}
        embedded = legacy_methods.get("embedded", []) or []
        custom = legacy_methods.get("custom", []) or []
    universe = sorted(
        {e["name"] for e in (embedded or [])}
        | {e["name"] for e in (custom or [])}
    )
    policies = manifest.get("policies") or manifest.get("policy") or {}
    return _match_for_wildcards(
        list(agent.requires.methods),
        universe,
        agent_wildcards=bool(agent.requires.wildcards),
        server_accepts_wildcards=bool(policies.get("wildcards_accepted", True)),
    )


def format_outcome(outcome: MatchOutcome) -> str:
    """Multi-line human-readable rendering of a MatchOutcome."""
    lines = [outcome.summary_line()]
    if outcome.matched:
        lines.append(
            f"Matched ({len(outcome.matched)}): "
            f"{', '.join(outcome.matched)}"
        )
    if outcome.missing:
        lines.append(
            f"Missing ({len(outcome.missing)}): "
            f"{', '.join(outcome.missing)}"
        )
    if outcome.server_methods_universe:
        lines.append(
            f"Server has ({len(outcome.server_methods_universe)}): "
            f"{', '.join(outcome.server_methods_universe)}"
        )
    if outcome.agent_wants_wildcards and not outcome.server_accepts_wildcards:
        lines.append(
            "Note: agent declares wildcards but server policy refuses them; "
            "non-embedded invocations will return 462."
        )
    return "\n".join(lines)


__all__ = [
    "MatchOutcome",
    "format_outcome",
    "match",
    "match_from_manifest_dict",
]
