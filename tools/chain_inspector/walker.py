"""
Chain walker — fetch and verify an audit chain backwards.

Given (agent_uri, audit_id), repeatedly:

  1. Send AGTP ``INSPECT {"target": "audit", "audit_id": ...}`` to
     the agent's daemon.
  2. Parse the returned JWS to extract header + payload + signature.
  3. Optionally verify the signature against a supplied public key.
  4. Enqueue every reachable predecessor:

       * ``payload.previous_audit_id`` — same-agent backward link.
       * ``payload.extra.prior_actions[]`` — cross-agent links,
         one per upstream agent. Each entry carries an ``agent_id``
         and ``audit_id`` and **optionally** an ``agent_uri``
         (preferred; self-describing). When ``agent_uri`` is absent,
         the walker consults the caller-supplied ``known_agents``
         map; if still unknown, the cross-agent step is recorded
         with a ``fetch_error`` and the branch stops.

The walker stops a branch when:

  * ``previous_audit_id`` is empty (the agent's first record), OR
  * a fetch returns 404 (record rotated out / unknown id), OR
  * a cross-agent reference can't be located, OR
  * ``max_steps`` is reached (defensive — prevents runaway loops on
    an attacker-supplied chain), OR
  * the (agent_id, audit_id) pair has already been visited (cycle
    detection across agents).

The walk is **breadth-first**. Output is a flat list of steps in
visit order; each step carries the list of step-IDs that point
into it via ``parent_step_ids`` so a renderer can rebuild the
tree shape. This handles diamonds (the same audit_id reached via
two different cross-agent paths) cleanly: each (agent_id, audit_id)
appears once in the output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from client.core_client import invoke_method
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from server.signing import (
    AttributionRecordError,
    parse_attribution_record,
    verify_attribution_record,
)


DEFAULT_MAX_STEPS = 256


@dataclass
class PriorActionRef:
    """A pointer from one audit record to another, possibly on a
    different agent. Surfaced via ``payload.extra.prior_actions``."""

    agent_id: str
    audit_id: str
    agent_uri: str = ""
    """Optional self-describing URI for the upstream agent. When
    present, the walker uses it directly. When absent, the walker
    falls back to the ``known_agents`` map."""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "agent_id": self.agent_id,
            "audit_id": self.audit_id,
        }
        if self.agent_uri:
            out["agent_uri"] = self.agent_uri
        return out


@dataclass
class ChainStep:
    """One record in a walked chain.

    Steps come back in BFS visit order. ``parent_step_ids`` lists the
    indices (into the output list) of every step that pointed to this
    one — empty for the starting step; non-empty for everything else.
    A step with two entries in ``parent_step_ids`` is a "diamond"
    where two upstream branches converge.
    """

    step_id: int
    audit_id: str
    agent_id: str = ""
    """The agent that owns this record. Derived from the payload's
    ``agent_id`` field, not from the lookup URI — they should agree
    but the payload is authoritative."""
    agent_uri: str = ""
    """The URI the walker used to fetch this record. Operator-friendly
    for debugging which daemon was queried."""
    jws: str = ""
    header: Dict[str, Any] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)
    previous_audit_id: str = ""
    prior_actions: List[PriorActionRef] = field(default_factory=list)
    """Cross-agent predecessors declared in the payload."""
    signed: bool = False
    """True when header.alg == 'EdDSA' (vs unsecured alg:none)."""
    verified: Optional[bool] = None
    """``True`` when a public key was supplied and the signature
    verified; ``False`` when verification failed; ``None`` when no
    public key was available (verification was skipped)."""
    fetch_error: str = ""
    """Non-empty when the fetch for this audit_id failed."""
    parent_step_ids: List[int] = field(default_factory=list)
    """Indices of the steps that pointed at this one. Empty for the
    starting step."""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "step_id": self.step_id,
            "audit_id": self.audit_id,
            "agent_id": self.agent_id,
            "agent_uri": self.agent_uri,
            "signed": self.signed,
            "previous_audit_id": self.previous_audit_id,
            "prior_actions": [p.to_dict() for p in self.prior_actions],
            "parent_step_ids": list(self.parent_step_ids),
        }
        if self.verified is not None:
            out["verified"] = self.verified
        if self.fetch_error:
            out["fetch_error"] = self.fetch_error
        else:
            out["header"] = self.header
            out["payload"] = self.payload
            out["jws"] = self.jws
        return out


def walk_chain(
    *,
    agent_uri: str,
    start_audit_id: str,
    issuer_public_key: Optional[Ed25519PublicKey] = None,
    known_agents: Optional[Dict[str, str]] = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    insecure: bool = False,
    insecure_skip_verify: bool = False,
) -> List[ChainStep]:
    """Walk an audit chain backwards from ``start_audit_id``.

    Cross-agent traversal: when a step's payload carries
    ``extra.prior_actions``, the walker enqueues each upstream
    reference. The upstream URI comes from (in order): the
    ``agent_uri`` field on the ``prior_actions`` entry, then
    ``known_agents.get(agent_id)``, then "unknown" (the step
    records a fetch_error and the branch stops there).

    Pass ``issuer_public_key`` to verify every JWS against a
    specific key — useful when all agents in the chain are issued
    by the same registrar. For multi-issuer chains, leave
    ``issuer_public_key=None`` and verify per-step at the caller.

    Pass ``insecure=True`` to disable TLS on the wire (test
    fixtures, dev daemons). ``insecure_skip_verify=True`` keeps TLS
    but skips chain validation (self-signed certs).
    """
    known_agents = {k.lower(): v for k, v in (known_agents or {}).items()}

    # Frontier: list of (agent_uri, audit_id, parent_step_id_or_None).
    # parent_step_id is None for the starting step.
    Frontier = List[Tuple[str, str, Optional[int]]]
    frontier: Frontier = [
        (agent_uri, start_audit_id.strip().lower(), None)
    ]

    # Visited: key on audit_id alone — audit_ids are sha256 of the
    # JWS, so collisions across agents are crypto-improbable. Cycle
    # detection catches a malicious upstream that returns the same
    # audit_id we already saw.
    visited: Dict[str, int] = {}  # audit_id -> step_id in output

    steps: List[ChainStep] = []

    while frontier and len(steps) < max_steps:
        target_uri, audit_id, parent_id = frontier.pop(0)
        audit_id = audit_id.strip().lower()

        # If we already visited this audit_id, just record the
        # additional parent edge — no re-fetch.
        if audit_id in visited:
            existing_step_id = visited[audit_id]
            if parent_id is not None:
                existing = steps[existing_step_id]
                if parent_id not in existing.parent_step_ids:
                    existing.parent_step_ids.append(parent_id)
            continue

        step_id = len(steps)
        if not target_uri:
            # Cross-agent reference with no known URI — record and
            # stop this branch.
            step = ChainStep(
                step_id=step_id,
                audit_id=audit_id,
                fetch_error=(
                    "cross-agent reference: no agent_uri available "
                    "(not on the prior_actions entry and not in "
                    "known_agents)"
                ),
            )
            if parent_id is not None:
                step.parent_step_ids.append(parent_id)
            steps.append(step)
            visited[audit_id] = step_id
            continue

        step = _fetch_one(
            agent_uri=target_uri,
            audit_id=audit_id,
            step_id=step_id,
            issuer_public_key=issuer_public_key,
            insecure=insecure,
            insecure_skip_verify=insecure_skip_verify,
        )
        if parent_id is not None:
            step.parent_step_ids.append(parent_id)
        steps.append(step)
        visited[audit_id] = step_id

        # Stop this branch on fetch failure.
        if step.fetch_error:
            continue

        # Enqueue same-agent predecessor.
        if step.previous_audit_id:
            frontier.append((target_uri, step.previous_audit_id, step_id))

        # Enqueue cross-agent predecessors.
        for ref in step.prior_actions:
            upstream_uri = ref.agent_uri or known_agents.get(
                ref.agent_id.lower(), "",
            )
            frontier.append((upstream_uri, ref.audit_id, step_id))

    return steps


def _fetch_one(
    *,
    agent_uri: str,
    audit_id: str,
    step_id: int,
    issuer_public_key: Optional[Ed25519PublicKey],
    insecure: bool,
    insecure_skip_verify: bool,
) -> ChainStep:
    result = invoke_method(
        agent_uri,
        "INSPECT",
        body={"target": "audit", "audit_id": audit_id},
        insecure=insecure,
        insecure_skip_verify=insecure_skip_verify,
    )
    if not result.ok or result.status_code != 200:
        msg = result.error or f"INSPECT returned {result.status_code}"
        return ChainStep(
            step_id=step_id, audit_id=audit_id, agent_uri=agent_uri,
            fetch_error=msg,
        )

    try:
        body = json.loads(result.body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ChainStep(
            step_id=step_id, audit_id=audit_id, agent_uri=agent_uri,
            fetch_error=f"could not parse INSPECT body: {exc}",
        )

    jws = body.get("jws") or ""
    if not jws:
        return ChainStep(
            step_id=step_id, audit_id=audit_id, agent_uri=agent_uri,
            fetch_error="INSPECT body missing 'jws' field",
        )

    try:
        header, payload, _sig = parse_attribution_record(jws)
    except AttributionRecordError as exc:
        return ChainStep(
            step_id=step_id, audit_id=audit_id, agent_uri=agent_uri,
            fetch_error=f"malformed JWS: {exc}",
        )

    signed = header.get("alg") == "EdDSA"
    verified: Optional[bool] = None
    if issuer_public_key is not None and signed:
        try:
            verify_attribution_record(jws, issuer_public_key)
            verified = True
        except AttributionRecordError:
            verified = False

    return ChainStep(
        step_id=step_id,
        audit_id=audit_id,
        agent_id=str(payload.get("agent_id") or ""),
        agent_uri=agent_uri,
        jws=jws,
        header=header,
        payload=payload,
        previous_audit_id=str(payload.get("previous_audit_id") or ""),
        prior_actions=_parse_prior_actions(payload),
        signed=signed,
        verified=verified,
    )


def _parse_prior_actions(payload: Dict[str, Any]) -> List[PriorActionRef]:
    """Extract ``extra.prior_actions`` from a JWS payload, tolerating
    a few legal shapes:

      * Missing / non-dict ``extra``: returns ``[]``.
      * ``extra.prior_actions`` missing or not a list: returns ``[]``.
      * Entries that aren't dicts or don't carry both ``agent_id``
        and ``audit_id``: silently skipped (defensive against
        attacker-supplied payloads).

    A defensive parse is appropriate here because the payload was
    handler-authored — we don't refuse to walk the chain just
    because one upstream entry is malformed.
    """
    extra = payload.get("extra")
    if not isinstance(extra, dict):
        return []
    raw = extra.get("prior_actions")
    if not isinstance(raw, list):
        return []
    out: List[PriorActionRef] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        aid = entry.get("agent_id")
        auid = entry.get("audit_id")
        if not isinstance(aid, str) or not isinstance(auid, str):
            continue
        if not aid or not auid:
            continue
        uri = entry.get("agent_uri") or ""
        out.append(PriorActionRef(
            agent_id=aid.lower(),
            audit_id=auid.lower(),
            agent_uri=str(uri) if uri else "",
        ))
    return out


__all__ = [
    "ChainStep",
    "DEFAULT_MAX_STEPS",
    "PriorActionRef",
    "walk_chain",
]
