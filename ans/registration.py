"""
Lifecycle-driven ANS registration (PDD §6.2).

The hosting daemon installs :func:`make_lifecycle_hook` as an
``AgentRegistry.lifecycle_hooks`` callback. On ACTIVATE it submits a
REGISTER (name + manifest summary) to every configured ANS endpoint; on a
transition to Suspended / Revoked / Deprecated it submits a DEREGISTER.

Because the hook fires synchronously inside the lifecycle transition,
deregistration happens immediately — well within the 60-second urgency the
spec requires. Submission is best-effort: an unreachable ANS is logged, not
fatal (the daemon's own state change is already authoritative).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, List

from client.core_client import send_method
from core import wire


# Statuses that mean "remove me from discovery" (PDD deregistration urgency).
_DEREGISTER_STATUSES = {"suspended", "revoked", "deprecated", "retired"}
_REGISTER_STATUSES = {"active"}


def manifest_summary(agent_doc) -> dict:
    """The manifest fields an ANS indexes for resolution and ranking."""
    summary = {
        "name": agent_doc.name,
        "supported_methods": list(agent_doc.requires.methods),
        "trust_tier": agent_doc.trust_tier,
        "verification_path": agent_doc.verification_path,
    }
    if agent_doc.trust_score is not None:
        summary["behavioral_trust_score"] = agent_doc.trust_score
    if agent_doc.owner_id:
        summary["owner_id"] = agent_doc.owner_id
    return summary


def make_lifecycle_hook(
    endpoints: List[str],
    *,
    use_tls: bool = True,
    insecure_skip_verify: bool = False,
) -> Callable[[Any, str, str], None]:
    """
    Build the lifecycle hook that keeps the configured ANS endpoints in
    sync with an agent's lifecycle.
    """

    def _hook(agent_doc, event_type: str, new_status: str) -> None:
        status = (new_status or "").strip().lower()
        if status in _REGISTER_STATUSES:
            body = {
                "agent_id": agent_doc.agent_id,
                "name": agent_doc.name,
                "manifest": manifest_summary(agent_doc),
            }
            _submit(endpoints, "REGISTER", body,
                    use_tls=use_tls, insecure_skip_verify=insecure_skip_verify)
        elif status in _DEREGISTER_STATUSES:
            body = {"agent_id": agent_doc.agent_id}
            _submit(endpoints, "DEREGISTER", body,
                    use_tls=use_tls, insecure_skip_verify=insecure_skip_verify)

    return _hook


def _submit(endpoints, method, body, *, use_tls, insecure_skip_verify) -> None:
    payload = json.dumps(body).encode("utf-8")
    for endpoint in endpoints:
        host, _, port_s = endpoint.rpartition(":")
        if not host or not port_s.isdigit():
            continue
        try:
            send_method(
                None, host, int(port_s), method,
                body=payload, body_content_type="application/json",
                use_tls=use_tls, insecure_skip_verify=insecure_skip_verify,
            )
        except (OSError, wire.WireFormatError) as exc:
            print(
                f"[server] ANS {method} to {endpoint} failed: {exc}",
                file=sys.stderr,
            )
