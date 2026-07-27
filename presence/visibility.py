"""
The three-axis presence visibility model (PDD §6.1 "Visibility model").

An agent controls who can see it, and how much they see, along three
orthogonal axes:

  * **presence** — is the agent visible to this requester at all?
    ``public`` | ``tier-scoped`` | ``owner-domain`` | ``explicit-only`` |
    ``invisible``.
  * **disclosure** — how much of the agent's record is returned?
    ``full`` | ``capabilities`` | ``identity-only`` | ``existence-only``.
  * **audience** — a boolean expression over requester attributes
    (``tier:N``, ``owner-domain:X``, ``capability:X``, ``agent-id:X``,
    ``industry:X``, ``region:X``, ``governance-group:X``) combined with
    ``AND`` / ``OR`` / ``NOT`` and parentheses. Empty means "everyone".

The certificate-declared posture (:mod:`server.agent_cert_ext`
``presence-visibility``) is the **maximum envelope**; a runtime
``Presence-Mode`` header (or an ANNOUNCE body) may only *reduce* visibility
within it. :func:`bound_visibility` computes that intersection.

Evaluation is fail-closed: an unparseable audience expression or an
unknown predicate key denies visibility rather than granting it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional

from presence.records import Visibility


# Ordered most-visible -> least-visible. A larger rank is more
# restrictive, so "reduce within the envelope" is ``max(rank)``.
_PRESENCE_ORDER = ["public", "tier-scoped", "owner-domain", "explicit-only", "invisible"]
_DISCLOSURE_ORDER = ["full", "capabilities", "identity-only", "existence-only"]


def _presence_rank(mode: str) -> int:
    try:
        return _PRESENCE_ORDER.index(mode)
    except ValueError:
        return len(_PRESENCE_ORDER)  # unknown -> most restrictive


def _disclosure_rank(mode: str) -> int:
    try:
        return _DISCLOSURE_ORDER.index(mode)
    except ValueError:
        return len(_DISCLOSURE_ORDER)


@dataclass(frozen=True)
class RequesterContext:
    """
    What the coordinator knows about who is asking. Populated from the
    requester's verified certificate, else from a hosted-agent lookup of
    the Agent-ID header, else anonymous (all unknown).
    """

    agent_id: str = ""
    tier: Optional[int] = None
    owner_domain: Optional[str] = None
    capabilities: FrozenSet[str] = field(default_factory=frozenset)
    industry: Optional[str] = None
    region: Optional[str] = None
    governance_group: Optional[str] = None

    @property
    def is_anonymous(self) -> bool:
        return not self.agent_id


ANONYMOUS = RequesterContext()


# ---------------------------------------------------------------------------
# Audience expression evaluation.
# ---------------------------------------------------------------------------


class _AudienceError(ValueError):
    """Raised on a malformed audience expression (fails closed)."""


def _tokenize(expr: str) -> List[str]:
    tokens: List[str] = []
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "()":
            tokens.append(ch)
            i += 1
            continue
        # A bareword: an operator (AND/OR/NOT) or a key:value predicate.
        j = i
        while j < n and not expr[j].isspace() and expr[j] not in "()":
            j += 1
        tokens.append(expr[i:j])
        i = j
    return tokens


class _Parser:
    """Recursive-descent parser/evaluator for audience expressions.

    Grammar (case-insensitive operators)::

        or_expr  := and_expr ( OR and_expr )*
        and_expr := not_expr ( AND not_expr )*
        not_expr := NOT not_expr | atom
        atom     := '(' or_expr ')' | predicate
        predicate := key ':' value
    """

    def __init__(self, tokens: List[str], requester: RequesterContext):
        self.tokens = tokens
        self.pos = 0
        self.requester = requester

    def _peek(self) -> Optional[str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> Optional[str]:
        tok = self._peek()
        if tok is not None:
            self.pos += 1
        return tok

    def parse(self) -> bool:
        value = self._or()
        if self.pos != len(self.tokens):
            raise _AudienceError(f"trailing tokens at {self.pos}")
        return value

    def _or(self) -> bool:
        value = self._and()
        while (tok := self._peek()) is not None and tok.upper() == "OR":
            self._next()
            rhs = self._and()
            value = value or rhs
        return value

    def _and(self) -> bool:
        value = self._not()
        while (tok := self._peek()) is not None and tok.upper() == "AND":
            self._next()
            rhs = self._not()
            value = value and rhs
        return value

    def _not(self) -> bool:
        tok = self._peek()
        if tok is not None and tok.upper() == "NOT":
            self._next()
            return not self._not()
        return self._atom()

    def _atom(self) -> bool:
        tok = self._next()
        if tok is None:
            raise _AudienceError("unexpected end of expression")
        if tok == "(":
            value = self._or()
            close = self._next()
            if close != ")":
                raise _AudienceError("unbalanced parentheses")
            return value
        if tok == ")":
            raise _AudienceError("unexpected ')'")
        if tok.upper() in ("AND", "OR", "NOT"):
            raise _AudienceError(f"operator {tok!r} in operand position")
        return _eval_predicate(tok, self.requester)


def _eval_predicate(token: str, requester: RequesterContext) -> bool:
    if ":" not in token:
        raise _AudienceError(f"malformed predicate {token!r} (expected key:value)")
    key, _, value = token.partition(":")
    key = key.strip().lower()
    value = value.strip()

    if key == "tier":
        try:
            threshold = int(value)
        except ValueError as exc:
            raise _AudienceError(f"tier predicate needs an integer: {value!r}") from exc
        # tier:N passes for requesters that are N or more trusted (lower
        # tier number == higher trust), consistent with tier-scoped.
        return requester.tier is not None and requester.tier <= threshold
    if key == "owner-domain":
        return bool(requester.owner_domain) and requester.owner_domain == value
    if key == "capability":
        return value.lower() in requester.capabilities
    if key == "agent-id":
        return bool(requester.agent_id) and requester.agent_id == value
    if key == "industry":
        return bool(requester.industry) and requester.industry == value
    if key == "region":
        return bool(requester.region) and requester.region == value
    if key == "governance-group":
        return bool(requester.governance_group) and requester.governance_group == value
    # Unknown key: fail closed.
    raise _AudienceError(f"unknown audience predicate key {key!r}")


def audience_allows(audience_scope: str, requester: RequesterContext) -> bool:
    """
    Evaluate an audience expression against a requester. Empty expression
    means everyone. Any parse error denies (fail-closed).
    """
    expr = (audience_scope or "").strip()
    if not expr:
        return True
    try:
        tokens = _tokenize(expr)
        if not tokens:
            return True
        return _Parser(tokens, requester).parse()
    except _AudienceError:
        return False


def _explicit_allow(audience_scope: str) -> FrozenSet[str]:
    """The set of agent-ids named by ``agent-id:X`` predicates, used by
    the ``explicit-only`` presence gate."""
    allow = set()
    for tok in _tokenize(audience_scope or ""):
        if tok.lower().startswith("agent-id:"):
            allow.add(tok.partition(":")[2].strip())
    return frozenset(allow)


# ---------------------------------------------------------------------------
# Visibility decisions.
# ---------------------------------------------------------------------------


def _subject_tier(record) -> Optional[int]:
    val = record.result_entry.get("trust_tier")
    return val if isinstance(val, int) else None


def _subject_owner_domain(record) -> Optional[str]:
    # The subject's owner-domain, carried on the record if known.
    dom = getattr(record, "owner_domain", None)
    if dom:
        return dom
    return record.result_entry.get("owner_id")


def is_visible(record, requester: RequesterContext) -> bool:
    """
    True iff ``requester`` may see ``record`` at all, per the record's
    presence mode AND its audience expression.
    """
    vis = record.visibility
    mode = vis.presence_mode

    if mode == "invisible":
        return False

    if mode == "public":
        presence_ok = True
    elif mode == "tier-scoped":
        subject_tier = _subject_tier(record)
        presence_ok = (
            requester.tier is not None
            and subject_tier is not None
            and requester.tier <= subject_tier
        )
    elif mode == "owner-domain":
        subject_dom = _subject_owner_domain(record)
        presence_ok = bool(subject_dom) and requester.owner_domain == subject_dom
    elif mode == "explicit-only":
        presence_ok = requester.agent_id in _explicit_allow(vis.audience_scope)
    else:
        # Unknown presence mode: fail closed.
        return False

    if not presence_ok:
        return False
    return audience_allows(vis.audience_scope, requester)


def shape_entry(record, requester: RequesterContext) -> dict:
    """
    Project a record's discovery entry per its disclosure mode. Assumes
    the caller has already confirmed :func:`is_visible`.
    """
    entry = dict(record.result_entry)
    mode = record.visibility.disclosure_mode

    if mode in ("full", "capabilities"):
        # M2 carries no fields beyond the "capabilities" shape, so full
        # and capabilities coincide today. full gains richer fields when
        # the manifest does.
        return entry
    if mode == "identity-only":
        keep = {"agent_id", "manifest_uri", "name", "trust_tier", "verification_path"}
        return {k: v for k, v in entry.items() if k in keep}
    if mode == "existence-only":
        return {"agent_id": entry.get("agent_id")}
    # Unknown disclosure mode: fail closed to the least disclosure.
    return {"agent_id": entry.get("agent_id")}


def bound_visibility(envelope: Optional[Visibility], requested: Visibility) -> Visibility:
    """
    The effective visibility when a runtime ``requested`` posture is
    applied within a certificate-declared ``envelope``. Runtime may only
    *reduce* visibility: presence and disclosure take the more restrictive
    of the two; audience expressions are ANDed.
    """
    if envelope is None:
        return requested

    presence = (
        requested.presence_mode
        if _presence_rank(requested.presence_mode) >= _presence_rank(envelope.presence_mode)
        else envelope.presence_mode
    )
    disclosure = (
        requested.disclosure_mode
        if _disclosure_rank(requested.disclosure_mode) >= _disclosure_rank(envelope.disclosure_mode)
        else envelope.disclosure_mode
    )
    env_aud = (envelope.audience_scope or "").strip()
    req_aud = (requested.audience_scope or "").strip()
    if env_aud and req_aud:
        audience = f"({env_aud}) AND ({req_aud})"
    else:
        audience = env_aud or req_aud

    return Visibility(
        presence_mode=presence,
        disclosure_mode=disclosure,
        audience_scope=audience,
    )
