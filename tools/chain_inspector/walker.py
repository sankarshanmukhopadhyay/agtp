"""
Chain walker — fetch and verify an audit chain backwards.

Given (agent_uri, audit_id), repeatedly:

  1. Send AGTP ``INSPECT {"target": "audit", "audit_id": ...}`` to
     the agent's daemon.
  2. Parse the returned JWS to extract header + payload + signature.
  3. Optionally verify the signature against a supplied public key.
  4. Read ``previous_audit_id`` from the payload and continue.

The walker stops when:

  * ``previous_audit_id`` is empty (we've reached the agent's first
    record), OR
  * a fetch returns 404 (record rotated out / unknown id), OR
  * ``max_steps`` is reached (defensive — prevents runaway loops on
    an attacker-supplied chain).

Cross-agent traversal is intentionally out of scope for v1. When a
payload's ``extra.prior_actions`` references another agent's
audit_id, the walker surfaces it on the chain step as a hint
without following — the caller restarts the walker against the
other agent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from client.core_client import invoke_method
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from server.signing import (
    AttributionRecordError,
    parse_attribution_record,
    verify_attribution_record,
)


DEFAULT_MAX_STEPS = 256


@dataclass
class ChainStep:
    """One record in a walked chain.

    A walked chain is a list of ChainSteps, newest first (the index-0
    entry is the audit_id the caller asked about; the last entry is
    the agent's first ever record, or the point at which the walker
    stopped).
    """

    audit_id: str
    jws: str = ""
    header: Dict[str, Any] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)
    previous_audit_id: str = ""
    signed: bool = False
    """True when header.alg == 'EdDSA' (vs unsecured alg:none)."""
    verified: Optional[bool] = None
    """``True`` when a public key was supplied and the signature
    verified; ``False`` when verification failed; ``None`` when no
    public key was available (verification was skipped)."""
    fetch_error: str = ""
    """Non-empty when the fetch for this audit_id failed; the rest
    of the fields are empty in that case."""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "audit_id": self.audit_id,
            "signed": self.signed,
            "previous_audit_id": self.previous_audit_id,
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
    max_steps: int = DEFAULT_MAX_STEPS,
    insecure: bool = False,
    insecure_skip_verify: bool = False,
) -> List[ChainStep]:
    """Walk the agent's chain backwards from ``start_audit_id``.

    Returns the steps newest-first. When ``issuer_public_key`` is
    supplied, every JWS is signature-verified against it and the
    result lands on ``ChainStep.verified``.

    Pass ``insecure=True`` to disable TLS on the wire (test
    fixtures, dev daemons). ``insecure_skip_verify=True`` keeps TLS
    but skips chain validation (self-signed certs).
    """
    steps: List[ChainStep] = []
    audit_id = start_audit_id.strip().lower()
    seen = set()

    while audit_id and len(steps) < max_steps:
        if audit_id in seen:
            # Defensive — a malicious chain that loops back would
            # otherwise hang the walker. Mark and stop.
            steps.append(ChainStep(
                audit_id=audit_id,
                fetch_error="chain contains a cycle; refusing to recurse",
            ))
            break
        seen.add(audit_id)

        step = _fetch_one(
            agent_uri=agent_uri,
            audit_id=audit_id,
            issuer_public_key=issuer_public_key,
            insecure=insecure,
            insecure_skip_verify=insecure_skip_verify,
        )
        steps.append(step)
        if step.fetch_error or not step.previous_audit_id:
            break
        audit_id = step.previous_audit_id.strip().lower()

    return steps


def _fetch_one(
    *,
    agent_uri: str,
    audit_id: str,
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
        return ChainStep(audit_id=audit_id, fetch_error=msg)

    try:
        body = json.loads(result.body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ChainStep(
            audit_id=audit_id,
            fetch_error=f"could not parse INSPECT body: {exc}",
        )

    jws = body.get("jws") or ""
    if not jws:
        return ChainStep(
            audit_id=audit_id,
            fetch_error="INSPECT body missing 'jws' field",
        )

    try:
        header, payload, _sig = parse_attribution_record(jws)
    except AttributionRecordError as exc:
        return ChainStep(
            audit_id=audit_id,
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
        audit_id=audit_id,
        jws=jws,
        header=header,
        payload=payload,
        previous_audit_id=str(payload.get("previous_audit_id") or ""),
        signed=signed,
        verified=verified,
    )


__all__ = [
    "ChainStep",
    "DEFAULT_MAX_STEPS",
    "walk_chain",
]
