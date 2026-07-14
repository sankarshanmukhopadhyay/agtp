"""
Server configuration loaded from ``agtp-server.toml``.

The config declares the server's identity (server_id, operator,
contact), its policy posture (wildcards, anonymous discovery, scope
enforcement, synthesis, **method policy**), and how openly it
discloses the agents it hosts. This data feeds the Server Manifest
returned by server-level DISCOVER.

A missing config file is fine for local development. Defaults are
chosen so that ``python -m server 4480`` against an empty
directory produces a usable, public-disclosure manifest.

Method-policy authoring lives in this same file under
``[policies.methods]`` (post-§6) — there is no separate
``methods.txt`` document. Pre-§6 deployments with a ``methods.txt``
on disk no longer load; operators should move their ``Allow`` /
``Disallow`` / ``Legacy`` / ``Redirect`` directives into the
``[policies.methods]`` block of their TOML config.

Field-name compatibility: the loader accepts the pre-§5 key names
(``[server].issuer``, ``[policy]``, ``[[hosts_protocols]]``)
alongside the new names so existing deployments keep loading after
upgrade. Going forward, new TOMLs should use the §5/§6 names
(``server_id``, ``[policies]`` with ``[policies.methods]``,
``hosted_protocols``).
"""

from __future__ import annotations

import sys
import tomllib
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core._paths import normalize


CONFIG_FILENAME = "agtp-server.toml"

DISCLOSURE_LEVELS = {"public", "limited", "private"}


def _load_apis(blocks: list) -> list:
    """Convert raw TOML [[apis]] tables into APIEndpoint objects."""
    from server.manifest import APIEndpoint
    out = []
    for block in blocks:
        out.append(APIEndpoint(
            path=str(block.get("path", "")),
            methods=list(block.get("methods", [])),
            description=block.get("description"),
        ))
    return out


def _load_hosted_protocols(blocks: list) -> list:
    """Convert raw TOML [[hosted_protocols]] tables into HostedProtocol."""
    from server.manifest import HostedProtocol
    out = []
    for block in blocks:
        out.append(HostedProtocol(
            protocol=str(block.get("protocol", "")),
            version=str(block.get("version", "")),
            endpoint=str(block.get("endpoint", "")),
            catalog=block.get("catalog"),
        ))
    return out


@dataclass
class ServerInfo:
    """Identity declared by the server in its manifest.

    Three identity-related fields per ``agtp-api §7``:

      * ``server_id``  — canonical identifier (agtp:// URI or
                         domain like ``acme.tld``)
      * ``domain``     — operational hosting target (where to
                         actually connect). Optional; servers whose
                         ``server_id`` already names the host may
                         omit this.
      * ``operator``   — human-readable organization name.

    Plus ``contact`` (operator handle) and ``issued`` (when this
    server first came online, set at provisioning time; the
    manifest emit captures ``updated`` automatically at every
    regeneration).
    """

    server_id: str
    operator: str
    contact: str
    domain: Optional[str] = None
    issued: str = ""


# ---------------------------------------------------------------------------
# Method policy (per ``agtp-api §8``).
# ---------------------------------------------------------------------------


@dataclass
class MethodsPolicy:
    """
    The per-server method policy enforced at dispatch time.

    Per ``agtp-api §8`` this lives under ``[policies.methods]`` in
    the server's TOML config and surfaces in the manifest under
    ``policies.methods``. The class carries the parsed in-memory
    representation plus the two runtime checks the dispatcher
    consults on every request.

    Resolution order at request time:

      1. If ``method`` is in :attr:`disallow`, refuse with 405.
      2. If ``method`` is in :attr:`legacy`, accept (legacy
         HTTP verbs only become acceptable through this opt-in).
      3. If :attr:`allow_all` is True or ``method`` is in
         :attr:`allow`, accept.
      4. Otherwise, refuse with 405.

    Redirects are evaluated *after* acceptance but *before* dispatch,
    so the rewrite target is what the dispatcher actually invokes.
    """

    allow: Set[str] = field(default_factory=set)
    allow_all: bool = False
    disallow: Set[str] = field(default_factory=set)
    legacy: Set[str] = field(default_factory=set)
    #: Keys are ``"METHOD"`` for method-only redirects or
    #: ``"METHOD /path"`` for method+path redirects. Values are
    #: ``("NEW_METHOD", new_path_or_None)``.
    redirects: Dict[str, Tuple[str, Optional[str]]] = field(default_factory=dict)
    #: RCNS-5: verb aliases. Resolved at the dispatcher gate before
    #: method validation, so a caller-supplied verb that's not in
    #: the AGTP catalog still flows through when an operator
    #: declares it as an alias. Default seed includes the five legacy
    #: HTTP verbs (GET, POST, PUT, DELETE, PATCH) mapped to their
    #: AGTP-canonical replacements (FETCH, CREATE, REPLACE, REMOVE,
    #: MODIFY). Operators add or override via
    #: ``[policies.methods.aliases]`` in agtp-server.toml.
    #:
    #: Keys are uppercased source-side verb names; values are
    #: uppercased target verbs. Distinct from :attr:`redirects` —
    #: redirects rewrite ``(method, path)`` tuples after registry
    #: resolution; aliases rewrite the verb itself ahead of catalog
    #: validation.
    aliases: Dict[str, str] = field(default_factory=dict)

    # ----- runtime checks (formerly free functions in
    # server/methods_policy.py) -----

    def is_method_allowed(self, method: str) -> bool:
        """True when this policy admits ``method``.

        Disallow has highest precedence. Legacy opt-in admits the
        five HTTP-method names without their needing to be in the
        approved-set. ``allow_all`` admits everything else;
        otherwise the method must be named in :attr:`allow`.
        """
        upper = method.upper()
        if upper in self.disallow:
            return False
        if upper in self.legacy:
            return True
        if self.allow_all:
            return True
        return upper in self.allow

    def resolve_alias(self, method: str) -> Optional[str]:
        """RCNS-5: resolve a verb alias.

        Returns the canonical target verb when ``method`` is an
        alias on this policy, or ``None`` when no alias applies.
        Resolution is single-hop: aliases never chain through each
        other (an operator declaring ``A -> B`` and ``B -> C`` gets
        ``A -> B`` only, not ``A -> C``). This keeps alias loops
        impossible and the behavior easy to reason about.
        """
        upper = method.upper()
        target = self.aliases.get(upper)
        if target is None or target == upper:
            return None
        return target

    def resolve_redirect(
        self, method: str, path: str,
    ) -> Optional[Tuple[str, str]]:
        """Look up a redirect for ``(method, path)``.

        Method+path redirects are checked first (more specific);
        method-only redirects fall through. Returns the rewritten
        ``(method, path)`` or ``None`` when no redirect matches.
        """
        upper = method.upper()
        specific = f"{upper} {path}"
        if specific in self.redirects:
            new_method, new_path = self.redirects[specific]
            return (new_method, new_path or path)
        if upper in self.redirects:
            new_method, new_path = self.redirects[upper]
            return (new_method, new_path or path)
        return None

    # ----- manifest exposure -----

    def to_wire(self) -> Dict[str, Any]:
        """Render the manifest's ``policies.methods`` sub-block.

        Allow is rendered as ``"*"`` (when ``allow_all``) or as a
        sorted list of names. Disallow / legacy are sorted lists.
        Redirects are an array of objects: each carries
        ``from_method``, optional ``from_path``, ``to_method``,
        and optional ``to_path``.
        """
        out: Dict[str, Any] = {}
        if self.allow_all:
            out["allow"] = "*"
        elif self.allow:
            out["allow"] = sorted(self.allow)
        else:
            out["allow"] = []
        if self.disallow:
            out["disallow"] = sorted(self.disallow)
        if self.legacy:
            out["legacy"] = sorted(self.legacy)
        if self.redirects:
            wire_redirects: List[Dict[str, str]] = []
            for key, (dst_method, dst_path) in self.redirects.items():
                # Key is either ``"METHOD"`` or ``"METHOD /path"``.
                parts = key.split(" ", 1)
                entry: Dict[str, str] = {"from_method": parts[0]}
                if len(parts) == 2:
                    entry["from_path"] = parts[1]
                entry["to_method"] = dst_method
                if dst_path:
                    entry["to_path"] = dst_path
                wire_redirects.append(entry)
            # Sort for stable output (method, then path).
            wire_redirects.sort(
                key=lambda e: (e.get("from_method", ""), e.get("from_path", ""))
            )
            out["redirects"] = wire_redirects
        if self.aliases:
            # Sort for stable manifest output. Callers compare
            # against the table to know which verbs the server will
            # accept beyond the AGTP catalog.
            out["aliases"] = dict(sorted(self.aliases.items()))
        return out


def _legacy_alias_seed() -> Dict[str, str]:
    """RCNS-5: seed the alias table with the five legacy HTTP verbs
    mapped to their AGTP-canonical replacements.

    The mapping comes from :data:`core.methods.LEGACY_VERBS` via
    :func:`core.methods.get_legacy_preferred` so the seed never goes
    out of sync with ``core/methods.json``. Operators can override
    any of these via ``[policies.methods.aliases]`` (e.g. a server
    that wants HTTP ``GET`` to be DISCOVER rather than FETCH).
    """
    from core.methods import LEGACY_VERBS, get_legacy_preferred
    seed: Dict[str, str] = {}
    for verb in LEGACY_VERBS:
        canonical = get_legacy_preferred(verb)
        if canonical:
            seed[verb.upper()] = canonical.upper()
    return seed


def default_methods_policy() -> MethodsPolicy:
    """Allow-all, no opt-ins, no redirects. The freshly-booted default.

    RCNS-5 seeds the alias table with the five legacy HTTP verbs so
    a fresh server admits ``GET /products`` as ``FETCH /products``
    without operator configuration. Disable by declaring an empty
    ``[policies.methods.aliases]`` block (``aliases = {}``).
    """
    return MethodsPolicy(allow_all=True, aliases=_legacy_alias_seed())


def _normalize_method_name(name: Any) -> str:
    return str(name).strip().upper()


def methods_policy_from_table(
    table: Dict[str, Any],
    *,
    source: str = "agtp-server.toml",
) -> MethodsPolicy:
    """
    Build a :class:`MethodsPolicy` from a parsed ``[policies.methods]``
    TOML table.

    The expected shape (per ``agtp-api §8``)::

        [policies.methods]
        allow    = "*" | ["VERB", ...]
        disallow = ["VERB", ...]
        legacy   = "*" | "NONE" | ["GET", ...]

        [[policies.methods.redirects]]
        from_method = "BOOK"
        from_path   = "/room"      # optional
        to_method   = "RESERVE"
        to_path     = "/room"      # optional

    Catalog-graceful: directives that reference verbs not in the
    current AGTP method catalog emit a :class:`core.methods.CatalogWarning`
    and are skipped — the same behavior the pre-§6 methods.txt loader
    had. ``source`` is used in the warning text so an operator can
    find the offending entry.
    """
    policy = MethodsPolicy()

    if not isinstance(table, dict):
        return policy

    # Allow.
    raw_allow = table.get("allow")
    if raw_allow == "*":
        policy.allow_all = True
    elif isinstance(raw_allow, list):
        for name in raw_allow:
            verb = _normalize_method_name(name)
            if not _verb_in_catalog(verb):
                _warn_unknown_verb("allow", verb, source)
                continue
            policy.allow.add(verb)
    elif raw_allow is None:
        # Missing ``allow`` defaults to allow-all so a [policies.methods]
        # block with only ``disallow`` / ``legacy`` / ``redirects``
        # behaves intuitively.
        policy.allow_all = True
    else:
        raise ValueError(
            f"{source}: policies.methods.allow must be '*' or a list of "
            f"verb names (got {raw_allow!r})"
        )

    # Disallow.
    raw_disallow = table.get("disallow") or []
    if not isinstance(raw_disallow, list):
        raise ValueError(
            f"{source}: policies.methods.disallow must be a list "
            f"(got {raw_disallow!r})"
        )
    for name in raw_disallow:
        verb = _normalize_method_name(name)
        # Disallow on a legacy verb is a deliberate override; admit
        # legacy names alongside catalog names.
        if not _verb_in_catalog(verb, allow_legacy=True):
            _warn_unknown_verb("disallow", verb, source)
            continue
        policy.disallow.add(verb)

    # Legacy.
    raw_legacy = table.get("legacy")
    from core.methods import LEGACY_VERBS as _LEGACY
    if raw_legacy == "*":
        policy.legacy.update(_LEGACY)
    elif raw_legacy is None or (
        isinstance(raw_legacy, str) and raw_legacy.strip().upper() == "NONE"
    ):
        pass  # no legacy opt-in
    elif isinstance(raw_legacy, list):
        for name in raw_legacy:
            verb = _normalize_method_name(name)
            if verb not in _LEGACY:
                raise ValueError(
                    f"{source}: policies.methods.legacy entry {verb!r} is "
                    f"not a recognized legacy HTTP verb (expected one of "
                    f"{sorted(_LEGACY)})"
                )
            policy.legacy.add(verb)
    else:
        raise ValueError(
            f"{source}: policies.methods.legacy must be '*', 'NONE', or a "
            f"list of legacy HTTP verb names (got {raw_legacy!r})"
        )

    # Redirects.
    raw_redirects = table.get("redirects") or []
    if not isinstance(raw_redirects, list):
        raise ValueError(
            f"{source}: policies.methods.redirects must be an array of "
            f"tables (got {raw_redirects!r})"
        )
    for entry in raw_redirects:
        if not isinstance(entry, dict):
            raise ValueError(
                f"{source}: each [[policies.methods.redirects]] entry must be "
                f"a table (got {entry!r})"
            )
        from_method = _normalize_method_name(entry.get("from_method") or "")
        to_method = _normalize_method_name(entry.get("to_method") or "")
        if not from_method or not to_method:
            raise ValueError(
                f"{source}: each redirect entry requires from_method and "
                f"to_method (got {entry!r})"
            )
        from_path = entry.get("from_path")
        to_path = entry.get("to_path")
        if (from_path is None) != (to_path is None):
            raise ValueError(
                f"{source}: redirect must include from_path AND to_path, "
                f"or neither (got {entry!r})"
            )
        # Catalog-graceful: skip the redirect if either side names
        # a verb the catalog has removed. Legacy HTTP verbs are
        # admitted on either side so ``GET -> FETCH`` keeps working.
        if not _verb_in_catalog(from_method, allow_legacy=True):
            _warn_unknown_verb("redirect (from)", from_method, source)
            continue
        if not _verb_in_catalog(to_method, allow_legacy=True):
            _warn_unknown_verb("redirect (to)", to_method, source)
            continue
        key = (
            from_method if from_path is None
            else f"{from_method} {from_path}"
        )
        policy.redirects[key] = (
            to_method,
            str(to_path) if to_path is not None else None,
        )

    # RCNS-5: aliases. Seed defaults first (the legacy HTTP table)
    # then layer the operator's declarations on top. An operator who
    # wants a clean slate declares ``[policies.methods.aliases]`` as
    # an empty table — the default seed is wiped before any
    # operator-supplied entries are applied.
    raw_aliases = table.get("aliases")
    if raw_aliases is None:
        # No block declared → keep the legacy seed.
        policy.aliases = _legacy_alias_seed()
    elif isinstance(raw_aliases, dict):
        # Operator declared a block — start fresh so explicit empty
        # tables disable the legacy seed entirely.
        policy.aliases = {}
        for source, target in raw_aliases.items():
            src_v = _normalize_method_name(source)
            tgt_v = _normalize_method_name(target)
            if not src_v or not tgt_v:
                raise ValueError(
                    f"{source}: each alias entry needs a non-empty "
                    f"source and target ({source!r} -> {target!r})"
                )
            # Target verb must be in the catalog (or legacy) so the
            # rest of the pipeline can still validate it.
            if not _verb_in_catalog(tgt_v, allow_legacy=True):
                _warn_unknown_verb("alias (target)", tgt_v, source)
                continue
            policy.aliases[src_v] = tgt_v
    else:
        raise ValueError(
            f"{source}: policies.methods.aliases must be a table "
            f"(got {raw_aliases!r})"
        )

    return policy


def _verb_in_catalog(name: str, *, allow_legacy: bool = False) -> bool:
    """True when ``name`` is a verb the loaded catalog recognizes.

    ``allow_legacy=True`` admits the five legacy HTTP names — used by
    Disallow and Redirect endpoints where the operator may
    legitimately write the source side of ``GET -> FETCH``.
    """
    from core.methods import is_approved_verb, is_legacy_verb
    upper = name.upper()
    if is_approved_verb(upper):
        return True
    if allow_legacy and is_legacy_verb(upper):
        return True
    return False


def _warn_unknown_verb(directive: str, name: str, source: str) -> None:
    """Catalog-graceful skip: a policy directive references a verb the
    catalog has removed (or that was always a typo). Skip the entry;
    warn loudly so the operator finds it in their boot logs."""
    from core.methods import CatalogWarning
    warnings.warn(
        f"{source}: policies.methods.{directive} {name!r} references a "
        f"verb not in the current catalog. Entry skipped.",
        CatalogWarning,
        stacklevel=3,
    )
    print(
        f"[server] {source}: policies.methods.{directive} {name!r} "
        f"references a verb not in the current catalog. Entry skipped.",
        file=sys.stderr,
    )


@dataclass
class ServerPolicy:
    """Operational policy advertised in the manifest.

    Five operational toggles per ``agtp-api §7`` plus the
    ``methods`` sub-block from ``agtp-api §8``:

      * ``wildcards_accepted``
      * ``anonymous_discovery``
      * ``scope_required_for_invocation``
      * ``synthesis_enabled`` — when ``False``, the dispatcher
        refuses PROPOSE with reason ``synthesis-disabled``.
      * ``max_synthesis_depth`` — maximum number of plan steps a
        composition may produce; deeper plans are refused at
        runtime. Default ``10``.
      * ``methods``  — per-server method admission policy
        (allow / disallow / legacy / redirects). See
        :class:`MethodsPolicy`.

    ``negotiable`` is retained as a pre-§5 alias for whether the
    server engages negotiation flows at all; new configs SHOULD
    use ``synthesis_enabled`` instead.
    """

    wildcards_accepted: bool = True
    anonymous_discovery: bool = True
    scope_required_for_invocation: bool = True
    synthesis_enabled: bool = True
    max_synthesis_depth: int = 10
    methods: MethodsPolicy = field(default_factory=default_methods_policy)
    negotiable: bool = True


@dataclass
class SynthesisConfig:
    """
    PROPOSE-time composition policy configuration.

    ``policies`` lists composition strategies in evaluation order.
    Today only ``"recipes"`` is shipping; future deployments may add
    ``"graph"`` or ``"llm"`` once those policies land. The runtime
    always appends a final ``"passthrough"`` fallback so the v1
    accept-on-exact-match behavior is preserved.

    ``recipes_file`` resolves relative to the server's working
    directory if the path is not absolute.

    §7 duration fields (used by
    :func:`server.synthesis_duration.compute_expiration`):

      * ``session_duration``            — TTL for non-persistent
                                          syntheses (default 24h).
      * ``persistent_default_duration`` — granted duration when the
                                          agent requests ``persistent:
                                          true`` without naming a
                                          duration (default 7d).
      * ``persistent_max_duration``     — hard cap regardless of what
                                          the agent requests (default
                                          30d).

    ``async_evaluation_enabled`` opts the server into the 261
    Negotiation In Progress flow: PROPOSE returns 261 with a
    ``proposal_id`` and agents poll ``QUERY /proposals/{proposal_id}``
    for the final outcome. Default ``False`` (every PROPOSE
    evaluates synchronously).
    """

    policies: List[str] = field(default_factory=lambda: ["recipes"])
    recipes_file: str = "agtp-recipes.toml"
    session_duration: str = "24h"
    persistent_default_duration: str = "7d"
    persistent_max_duration: str = "30d"
    async_evaluation_enabled: bool = False
    max_evaluation_duration: str = "10m"


@dataclass
class RcnsConfig:
    """
    RCNS — Runtime Contract Negotiation Substrate, RCNS-3.

    When ``enabled = true``, the dispatcher escalates would-be 404
    responses into a runtime negotiation for callers that satisfy
    three additional locks: the request carries ``Allow-RCNS: true``
    (or ``optimistic``), the agent's scopes include
    ``rcns:negotiate``, and the agent's resolved trust tier is at
    least as strong as ``min_trust_tier``.

    ``min_trust_tier`` semantics follow the rest of the trust model:
    lower numbers mean stronger trust. A value of ``1`` admits only
    Tier 1 agents; ``2`` admits Tier 1 and Tier 2; ``3`` admits any
    declared trust posture. Default ``1`` — the safest posture, the
    operator opts callers in deliberately.

    ``max_negotiations_per_minute`` is a per-agent rolling rate
    limit. Distinct from the ordinary request rate limit because
    negotiations are expensive (composition policies run on every
    one) and the abuse pattern is asymmetric: a single misbehaving
    agent should not be able to consume the server's negotiation
    budget by spraying random paths.

    ``idempotency_window_seconds`` controls how long an
    ``RCNS-Idempotency-Key`` retains its negotiated synthesis_id.
    Same key + same agent within the window returns the same
    synthesis_id; outside the window the cached entry is evicted
    and a fresh negotiation runs.

    ``on_policy_change`` governs the default mode of the
    operator-fired ``REVOKE target=stale-contracts`` sweep
    (RCNS-4 follow-up). The sweep walks active contracts and
    compares each contract's captured ``recipe_version`` against
    the current recipe set; mismatches are reported (``grandfather``,
    read-only) or evicted with an ``rcns_release`` audit event
    carrying ``reason = "policy-change-invalidation"``
    (``invalidate``). An operator can override the default per-call
    by passing ``mode`` in the REVOKE body. Default
    ``"grandfather"`` so a stale recipe edit doesn't unexpectedly
    evict callers.

    ``require_verified_identity`` — default ``True``. When ``True``,
    RCNS refuses to negotiate for a request whose Agent-ID arrived
    without a verified mTLS client certificate
    (``request.verified_cert is None``), returning **464** with
    ``reason = "identity-unverified"`` rather than treating the
    header-asserted Agent-ID as sufficient. This closes a specific
    abuse path: under the default ``[mtls].mode = "disabled"``
    posture, ``Agent-ID`` is a plain client-supplied header, so the
    per-agent negotiation rate limit and idempotency cache (both
    keyed on ``agent_id``) are trivially bypassed by rotating the
    header on every request — the *documented* per-agent ceiling
    doesn't actually bind under that posture. Setting this to
    ``True`` is meaningful only when the server also runs with
    ``[mtls].mode`` set to ``optional`` or ``required``; with mTLS
    fully disabled server-wide, every RCNS request will be refused,
    which is intentional (there is no verified identity to key
    anything on) but worth knowing before flipping this on.
    """

    enabled: bool = False
    min_trust_tier: int = 1
    max_negotiations_per_minute: int = 10
    idempotency_window_seconds: int = 60
    on_policy_change: str = "grandfather"
    require_verified_identity: bool = False

    def __post_init__(self) -> None:
        if self.min_trust_tier not in (1, 2, 3):
            raise ValueError(
                f"rcns.min_trust_tier must be 1, 2, or 3 (got "
                f"{self.min_trust_tier!r})"
            )
        if self.max_negotiations_per_minute < 0:
            raise ValueError(
                f"rcns.max_negotiations_per_minute must be >= 0 (got "
                f"{self.max_negotiations_per_minute!r})"
            )
        if self.idempotency_window_seconds < 0:
            raise ValueError(
                f"rcns.idempotency_window_seconds must be >= 0 (got "
                f"{self.idempotency_window_seconds!r})"
            )
        if self.on_policy_change not in ("grandfather", "invalidate"):
            raise ValueError(
                f"rcns.on_policy_change must be 'grandfather' or "
                f"'invalidate' (got {self.on_policy_change!r})"
            )


@dataclass
class OAuthConfig:
    """
    Pattern 2 OAuth composition (see ``docs/oauth-composition.md``).

    AGTP identifies *which agent* is making the call (the wire
    layer's Agent-ID / cert); an OAuth bearer token carried in the
    standard HTTP ``Authorization: Bearer <token>`` header identifies
    *which principal* the agent is acting on behalf of (the
    application layer). The two are orthogonal — Agent-ID answers
    "who is asking?", OAuth principal answers "for whom?".

    Default posture is OFF: ``enabled = false`` means existing
    Pattern 1 deployments (Agent-ID + cert + Authority-Scope, no
    external IdP) keep working unchanged. The dispatcher skips OAuth
    extraction and validation entirely when this is off.

    When on, ``required_on_methods`` lists the methods that MUST
    carry an Authorization header. Empty list means the token is
    accepted-but-optional on every method — useful for transitional
    rollouts where some callers have updated their clients to send
    a token and others haven't yet. Servers return **401
    Unauthorized** with ``error.reason: oauth-required`` when the
    header is missing on a required method.

    ``validator`` names the validator class (registered with
    :func:`server.oauth_context.register_validator`). Ships with
    ``noop`` (accepts anything; for sanity-test and early
    integration only — emit a warning at boot when paired with
    ``enabled = true``) and ``jwt`` (Ed25519 / RSA JWT signature +
    standard ``exp`` / ``nbf`` time bounds).

    ``validator_config`` is passed verbatim to the validator's
    constructor. The ``jwt`` validator needs at minimum a
    ``public_key`` (PEM or base64url-of-raw-bytes Ed25519); optional
    ``allowed_algs``, ``expected_issuer``, ``expected_audience``,
    ``leeway_seconds``.

    ``principal_id_claim`` names the JWT claim the dispatcher lifts
    into ``request.acting_principal_id`` on successful validation.
    Defaults to ``sub`` (the standard subject claim). The lifted
    claim rides into the Attribution-Record's ``extra`` block as
    ``acting_principal_id``; the token itself MUST NOT appear in
    the record.

    ``allow_noop_validator`` — defaults ``False``. When ``enabled =
    true`` and ``validator = "noop"``, the server refuses to boot
    unless this is explicitly set ``true``. The ``noop`` validator
    accepts any non-empty bearer token, so leaving OAuth "on" with
    it in place is equivalent to no authentication at all; the
    previous posture (a stderr warning at boot) was easy to miss in
    a container/orchestrator log pipeline and did nothing at request
    time. Set this to ``true`` for local development or CI fixtures
    that intentionally use the no-op validator; production
    deployments should configure ``validator = "jwt"`` (or a
    registered custom validator) instead of setting this flag.
    """

    enabled: bool = False
    required_on_methods: List[str] = field(default_factory=list)
    validator: str = "noop"
    validator_config: Dict[str, Any] = field(default_factory=dict)
    principal_id_claim: str = "sub"
    allow_noop_validator: bool = False

    def __post_init__(self) -> None:
        # Normalize method names to uppercase so the dispatcher's
        # "is this method required?" check is case-insensitive.
        self.required_on_methods = [
            str(m).strip().upper() for m in self.required_on_methods
        ]


@dataclass
class AgentsConfig:
    """How openly the server lists the agents it hosts."""

    disclosure: str = "public"

    def __post_init__(self) -> None:
        if self.disclosure not in DISCLOSURE_LEVELS:
            raise ValueError(
                f"agents.disclosure must be one of {sorted(DISCLOSURE_LEVELS)}, "
                f"got {self.disclosure!r}"
            )


@dataclass
class AuditConfig:
    """
    Audit-log sink configuration (§7) plus §10 attribution-record
    opt-in.

    ``path`` is one of:

      * ``"stderr"`` (default) — write to ``sys.stderr``.
      * ``"none"`` / ``""``    — disable audit logging entirely.
      * any other string       — filesystem path; entries append
                                 as JSONL.

    Format is intentionally fixed (JSON lines) so log aggregators
    can index without parser-specific configuration.

    ``attribution_records_enabled`` (§10) controls whether every
    response carries an ``Attribution-Record`` header. The record
    is emitted as JWS Compact Serialization (RFC 7515); when
    ``[signing].enabled`` is true the daemon signs with EdDSA, and
    otherwise emits an ``alg: none`` unsecured JWS so the shape
    stays consistent for verifiers. The companion ``Audit-ID``
    header carries ``sha256(jws)`` and chains via
    ``previous_audit_id`` in the next response's payload.

    ``chain_head_root`` is the filesystem directory the daemon uses
    to persist per-agent chain heads. Empty string selects the
    platform default
    (``~/.agtp/audit/chain_heads/`` on POSIX,
    ``%APPDATA%\\agtp\\audit\\chain_heads\\`` on Windows). Operators
    who run multiple daemons on one host MUST set this explicitly so
    chains don't collide.

    ``records_root`` is the filesystem directory the daemon uses to
    persist per-audit-id JWS records (the Phase-6 INSPECT read
    surface). Empty string selects the platform default
    (``~/.agtp/audit/records/`` on POSIX,
    ``%APPDATA%\\agtp\\audit\\records\\`` on Windows). Sharded by
    a 2-char hex prefix to keep directory sizes manageable.

    ``lifecycle_root`` is the filesystem directory the daemon uses
    to persist per-agent lifecycle event streams (the Phase-8
    ACTIVATE/DEACTIVATE/REVOKE history). Empty string selects the
    platform default (``~/.agtp/audit/lifecycle/`` on POSIX,
    ``%APPDATA%\\agtp\\audit\\lifecycle\\`` on Windows). One JWS per
    line, one file per agent — append-only.

    ``mode`` controls the receipt format for lifecycle events.

      * ``"jws"`` (default) — Ed25519-signed JWS Compact, identical
        in shape to Attribution-Record. Verifiable with any JWS
        library.
      * ``"scitt"`` — RFC 9943 SCITT-style COSE_Sign1 receipts
        (shipped T4.2). Same Ed25519 key signs both forms; the
        per-line prefix (``cose:`` for COSE; bare for JWS) lets
        readers disambiguate mixed-format streams across a mode
        flip. INSPECT lifecycle parses both transparently.
    """

    path: str = "stderr"
    attribution_records_enabled: bool = False
    chain_head_root: str = ""
    records_root: str = ""
    lifecycle_root: str = ""
    mode: str = "jws"
    # Phase-6 INSPECT read surface access control.
    #
    #   * ``"public"`` (default) — anyone can INSPECT any audit
    #     record or chain head. The intended posture for compliance /
    #     regulator-facing deployments: receipts are designed to be
    #     publicly verifiable.
    #   * ``"agent_only"`` — only the agent the record was emitted
    #     under (matched against the inbound ``Agent-ID``, or against
    #     the verified mTLS cert when present) can INSPECT it.
    #   * ``"operator_only"`` — only requests presenting an mTLS cert
    #     whose key matches an entry in
    #     ``[audit].read_acl_operator_keys`` may INSPECT. For
    #     internal-only audit deployments.
    #
    # The ACL applies to INSPECT (``target=audit`` / ``chain_head`` /
    # ``lifecycle``) only. Attribution-Record headers continue to
    # ride on every response regardless — the ACL gates the read
    # surface, not the write surface.
    read_acl: str = "public"
    # Hex-encoded SHA-256 fingerprints of cert public keys (32 raw
    # bytes hashed) authorized to INSPECT when ``read_acl =
    # operator_only``. Empty list with operator_only refuses every
    # request — useful for fail-safe deployments.
    read_acl_operator_keys: List[str] = field(default_factory=list)
    # Phase 8 T2.3 — lifecycle method authorization.
    #
    #   * ``"open"`` (default) — any caller can invoke ACTIVATE /
    #     DEACTIVATE / REVOKE / REINSTATE / DEPRECATE on any agent.
    #     The lifecycle audit stream is the accountability mechanism.
    #     Matches the rollout-friendly Phase 8 posture.
    #   * ``"genesis_issuer"`` — the caller MUST present a verified
    #     mTLS cert whose public key matches the target agent's
    #     Genesis ``issuer_public_key``. Only the registrar that
    #     issued the agent can transition its lifecycle. Agents
    #     without a loaded Genesis (transport-only identity) cannot
    #     be lifecycle-managed under this mode.
    #
    # The check is a no-op when mTLS isn't configured — there's no
    # way to authenticate the caller's identity then. Operators
    # who set ``genesis_issuer`` SHOULD also set ``[mtls].mode =
    # "required"``.
    lifecycle_auth: str = "open"


@dataclass
class GatewayConfig:
    """
    Gateway socket configuration (M3 step b/c).

    ``socket`` is the path or ``host:port`` the daemon binds for
    accepting runtime-module connections. When empty, gateway mode is
    off and registered_function handlers resolve in-daemon (the
    legacy path documented in
    ``server.handler_resolution.resolve_registered_function``).

    The ``--gateway-socket`` command-line flag overrides whatever is
    set here. Operators who want gateway mode to be the default for
    their deployment set ``socket`` in their ``agtp-server.toml``;
    transient overrides at boot use the flag.
    """

    socket: str = ""


@dataclass
class MtlsConfig:
    """
    Mutual-TLS (Agent-Cert) configuration.

    ``mode`` controls how the daemon's TLS listener treats client
    certificates:

      * ``"disabled"`` (default) — no client-cert verification. The
        Agent-ID header is the only identity signal; ``trust.method``
        on the gateway request frame stays ``"agent_id_header"``.
      * ``"optional"`` — clients MAY present a cert. When presented,
        the cert is verified against ``ca_bundle_path`` and the
        derived Agent-ID becomes authoritative. When absent, the
        connection proceeds with header-only identity (graceful
        migration path for sites rolling out mTLS).
      * ``"required"`` — clients MUST present a verified cert. The
        TLS handshake fails for missing certs.

    ``ca_bundle_path`` is a PEM file with one or more trusted CA
    certs. Required when mode is not ``"disabled"``.

    ``require_agent_id_match`` (default true): when a request carries
    BOTH a verified cert AND an Agent-ID header, the header value
    MUST match the cert-derived Agent-ID. Mismatches return 401.
    """

    mode: str = "disabled"
    ca_bundle_path: str = ""
    require_agent_id_match: bool = True


@dataclass
class SigningConfig:
    """
    Ed25519 signing configuration.

    When ``enabled`` is true and ``key_path`` resolves to a readable
    PEM file, the daemon loads the key at boot via
    :class:`server.signing.SigningService` and uses it to sign:

      * Attribution-Record headers on every response (replaces the
        pre-§5 placeholder).
      * Audit log receipts (when ``mod_audit`` is loaded with
        ``AGTP_AUDIT_SIGN_RECEIPTS=1``).
      * Future: Server Manifest at DISCOVER, AGTP-LOG entries.

    Generate a key pair with ``tools/generate_signing_key.py``. The
    daemon refuses to boot when signing is enabled and the key file
    is missing or malformed.

    ``key_id`` is an optional stable identifier embedded in signed
    payloads. When omitted the service derives it from the public
    key's raw bytes (``ed25519-<sha256 prefix>``).
    """

    enabled: bool = False
    key_path: str = ""
    key_id: str = ""


@dataclass
class ServerConfig:
    """Top-level configuration object."""

    server: ServerInfo
    policy: ServerPolicy = field(default_factory=ServerPolicy)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    synthesis: SynthesisConfig = field(default_factory=SynthesisConfig)
    rcns: RcnsConfig = field(default_factory=RcnsConfig)
    oauth: OAuthConfig = field(default_factory=OAuthConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    signing: SigningConfig = field(default_factory=SigningConfig)
    mtls: MtlsConfig = field(default_factory=MtlsConfig)
    apis: list = field(default_factory=list)
    hosted_protocols: list = field(default_factory=list)
    source_path: Optional[Path] = None

    @property
    def is_default(self) -> bool:
        return self.source_path is None


def _default_server_id(host: Optional[str]) -> str:
    """Pick a reasonable server_id for a missing config."""
    if host and host not in ("0.0.0.0", "::", ""):
        return host
    return "localhost"


def default_config(host: Optional[str] = None) -> ServerConfig:
    """Construct a sensible default config when no file is present."""
    return ServerConfig(
        server=ServerInfo(
            server_id=_default_server_id(host),
            operator="local development",
            contact="",
        ),
        policy=ServerPolicy(),
        agents=AgentsConfig(disclosure="public"),
        source_path=None,
    )


def load(path: Optional[Path], *, host: Optional[str] = None) -> ServerConfig:
    """
    Load a TOML config from ``path`` if given, else look for
    ``agtp-server.toml`` in the current working directory. Falls back
    to ``default_config(host)`` when no file exists.

    Accepts both the new §5/§6 key names and the pre-§5 names. A new
    deployment should use the new names exclusively; older deployments
    keep loading without edits.
    """
    candidate = (
        normalize(path) if path is not None
        else (Path.cwd() / CONFIG_FILENAME).resolve()
    )

    if not candidate.exists():
        if path is not None:
            raise FileNotFoundError(f"config file not found: {candidate}")
        return default_config(host)

    with candidate.open("rb") as f:
        data = tomllib.load(f)

    server_block = data.get("server", {})
    # Back-compat: pre-§5 configs used ``issuer`` for the server's
    # canonical identifier. Accept either key.
    server_id = server_block.get("server_id") or server_block.get("issuer")
    if not server_id:
        raise ValueError(
            f"{candidate}: [server].server_id is required when a config file "
            f"is present (legacy ``issuer`` is also accepted)"
        )

    server = ServerInfo(
        server_id=server_id,
        operator=server_block.get("operator", "unspecified"),
        contact=server_block.get("contact", ""),
        domain=server_block.get("domain"),
        issued=server_block.get("issued", ""),
    )

    # Back-compat: pre-§5 configs used ``[policy]`` (singular). New
    # configs use ``[policies]``. Prefer the new key when both are
    # present.
    policy_block = data.get("policies") or data.get("policy") or {}

    # §6 method-policy block: ``[policies.methods]`` (or the back-compat
    # ``[policy.methods]``). Missing block falls through to the
    # allow-all default.
    methods_table = policy_block.get("methods") or {}
    methods_policy_obj = methods_policy_from_table(
        methods_table, source=str(candidate),
    ) if methods_table else default_methods_policy()

    policy = ServerPolicy(
        wildcards_accepted=bool(policy_block.get("wildcards_accepted", True)),
        anonymous_discovery=bool(
            policy_block.get("anonymous_discovery", True)
        ),
        scope_required_for_invocation=bool(
            policy_block.get("scope_required_for_invocation", True)
        ),
        synthesis_enabled=bool(
            policy_block.get("synthesis_enabled", True)
        ),
        max_synthesis_depth=int(
            policy_block.get("max_synthesis_depth", 10)
        ),
        methods=methods_policy_obj,
        negotiable=bool(policy_block.get("negotiable", True)),
    )

    agents_block = data.get("agents", {})
    agents = AgentsConfig(
        disclosure=agents_block.get("disclosure", "public"),
    )

    # ``[synthesis]`` historically carried the policy list and
    # recipes file. §7 added duration / async fields; new
    # deployments author them under ``[policies.synthesis]`` for
    # consistency with the rest of the policies block, but the
    # legacy top-level ``[synthesis]`` table also keeps loading.
    policies_synthesis = (policy_block.get("synthesis") or {})
    synthesis_block = {
        **(data.get("synthesis", {}) or {}),
        **policies_synthesis,  # [policies.synthesis] wins on conflict
    }
    synthesis = SynthesisConfig(
        policies=list(synthesis_block.get("policies") or ["recipes"]),
        recipes_file=str(
            synthesis_block.get("recipes_file") or "agtp-recipes.toml"
        ),
        session_duration=str(
            synthesis_block.get("session_duration") or "24h"
        ),
        persistent_default_duration=str(
            synthesis_block.get("persistent_default_duration") or "7d"
        ),
        persistent_max_duration=str(
            synthesis_block.get("persistent_max_duration") or "30d"
        ),
        async_evaluation_enabled=bool(
            synthesis_block.get("async_evaluation_enabled", False)
        ),
        max_evaluation_duration=str(
            synthesis_block.get("max_evaluation_duration") or "10m"
        ),
    )

    # RCNS-3: ``[policies.rcns]`` block. Default is fully off — the
    # operator opts callers into runtime negotiation deliberately by
    # setting ``enabled = true``. Pre-RCNS-3 configs without the
    # block load cleanly via the dataclass defaults.
    rcns_block = policy_block.get("rcns") or {}
    rcns = RcnsConfig(
        enabled=bool(rcns_block.get("enabled", False)),
        min_trust_tier=int(rcns_block.get("min_trust_tier", 1)),
        max_negotiations_per_minute=int(
            rcns_block.get("max_negotiations_per_minute", 10)
        ),
        idempotency_window_seconds=int(
            rcns_block.get("idempotency_window_seconds", 60)
        ),
        on_policy_change=str(
            rcns_block.get("on_policy_change") or "grandfather"
        ),
        require_verified_identity=bool(
            rcns_block.get("require_verified_identity", True)
        ),
    )

    # OAuth composition: [policies.oauth] block, defaults to off so
    # existing deployments keep working unchanged.
    oauth_block = policy_block.get("oauth") or {}
    oauth_enabled = bool(oauth_block.get("enabled", False))
    oauth_validator = str(oauth_block.get("validator") or "noop")
    oauth_allow_noop = bool(oauth_block.get("allow_noop_validator", False))
    if oauth_enabled and oauth_validator == "noop" and not oauth_allow_noop:
        raise ValueError(
            "[policies.oauth] enabled = true with validator = \"noop\" "
            "refuses to boot without allow_noop_validator = true. The "
            "noop validator accepts any non-empty bearer token, which "
            "is equivalent to no authentication. Set "
            "[policies.oauth].validator to \"jwt\" (or a registered "
            "custom validator) for production use, or set "
            "allow_noop_validator = true to explicitly opt into the "
            "no-op validator for local development / CI."
        )
    oauth = OAuthConfig(
        enabled=oauth_enabled,
        required_on_methods=list(
            oauth_block.get("required_on_methods") or []
        ),
        validator=oauth_validator,
        validator_config=dict(oauth_block.get("validator_config") or {}),
        principal_id_claim=str(
            oauth_block.get("principal_id_claim") or "sub"
        ),
        allow_noop_validator=oauth_allow_noop,
    )

    audit_block = data.get("audit", {}) or {}
    audit = AuditConfig(
        path=str(audit_block.get("path") or "stderr"),
        attribution_records_enabled=bool(
            audit_block.get("attribution_records_enabled", False)
        ),
        chain_head_root=str(audit_block.get("chain_head_root") or ""),
        records_root=str(audit_block.get("records_root") or ""),
        lifecycle_root=str(audit_block.get("lifecycle_root") or ""),
        mode=str(audit_block.get("mode") or "jws"),
        read_acl=str(audit_block.get("read_acl") or "public"),
        read_acl_operator_keys=[
            str(k).strip().lower()
            for k in (audit_block.get("read_acl_operator_keys") or [])
        ],
        lifecycle_auth=str(audit_block.get("lifecycle_auth") or "open"),
    )
    if audit.read_acl not in ("public", "agent_only", "operator_only"):
        raise ValueError(
            f"[audit].read_acl must be 'public', 'agent_only', or "
            f"'operator_only'; got {audit.read_acl!r}"
        )
    if audit.lifecycle_auth not in ("open", "genesis_issuer"):
        raise ValueError(
            f"[audit].lifecycle_auth must be 'open' or "
            f"'genesis_issuer'; got {audit.lifecycle_auth!r}"
        )

    gateway_block = data.get("gateway", {}) or {}
    gateway = GatewayConfig(
        socket=str(gateway_block.get("socket") or ""),
    )

    signing_block = data.get("signing", {}) or {}
    signing = SigningConfig(
        enabled=bool(signing_block.get("enabled", False)),
        key_path=str(signing_block.get("key_path") or ""),
        key_id=str(signing_block.get("key_id") or ""),
    )

    mtls_block = data.get("mtls", {}) or {}
    mtls_mode = str(mtls_block.get("mode") or "disabled").lower()
    if mtls_mode not in ("disabled", "optional", "required"):
        raise ValueError(
            f"[mtls].mode must be one of disabled/optional/required, "
            f"got {mtls_mode!r}"
        )
    mtls = MtlsConfig(
        mode=mtls_mode,
        ca_bundle_path=str(mtls_block.get("ca_bundle_path") or ""),
        require_agent_id_match=bool(
            mtls_block.get("require_agent_id_match", True)
        ),
    )

    apis = _load_apis(data.get("apis", []) or [])
    # Back-compat: pre-§5 configs used ``[[hosts_protocols]]``. Accept
    # either array key.
    hosted_protocols_raw = (
        data.get("hosted_protocols") or data.get("hosts_protocols") or []
    )
    hosted_protocols = _load_hosted_protocols(hosted_protocols_raw)

    return ServerConfig(
        server=server,
        policy=policy,
        synthesis=synthesis,
        rcns=rcns,
        oauth=oauth,
        agents=agents,
        audit=audit,
        gateway=gateway,
        signing=signing,
        mtls=mtls,
        apis=apis,
        hosted_protocols=hosted_protocols,
        source_path=candidate,
    )


__all__ = [
    "AgentsConfig",
    "AuditConfig",
    "DISCLOSURE_LEVELS",
    "GatewayConfig",
    "MethodsPolicy",
    "MtlsConfig",
    "ServerConfig",
    "ServerInfo",
    "OAuthConfig",
    "RcnsConfig",
    "ServerPolicy",
    "SigningConfig",
    "SynthesisConfig",
    "default_config",
    "default_methods_policy",
    "load",
    "methods_policy_from_table",
    "CONFIG_FILENAME",
]
