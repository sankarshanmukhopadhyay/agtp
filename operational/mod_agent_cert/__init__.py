"""
mod_agent_cert — Scope-Enforcement-Point gating from Agent Certificate extensions.

Loaded by ``agtpd`` via ``--load-module mod_agent_cert``. The module's
``install(server_state)`` function registers an :class:`AgentCertHook`
against the daemon's :class:`HookRegistry`. The hook runs before
every handler invocation and enforces two constraints derived from
the AGTP Agent Certificate (`draft-hood-agtp-agent-cert-01`)
extensions:

  * **Authority-Scope** — every token in the inbound
    ``Authority-Scope`` request header MUST be a member of the cert's
    ``authority-scope-commitment``. Tokens outside the commitment
    return 455 Scope Violation without the body ever being parsed.
  * **Governance-Zone** — when the cert carries a ``governance-zone``
    extension AND the request carries an ``AGTP-Zone-ID`` header, the
    two MUST match. Mismatches return 457 Zone Violation.

When the connection has no verified cert (mTLS disabled or
transport-only cert without AGTP extensions), the hook is a no-op —
the daemon's existing soft-deny gate continues to handle scope checks
against the agent's declared scopes.

Per ``draft-hood-agtp-agent-cert §5.2``, this is the operational
component that makes "zero-trust for agents at the transport layer"
real: a load balancer or Scope-Enforcement-Point in front of the
daemon can refuse requests at the wire layer, never opening the body.

Configuration: none in v1. The hook activates whenever a verified
Agent Certificate carrying the relevant extensions is present.
Operators who want to disable enforcement for a specific period
unload the module via configuration (omit it from ``--load-module``).
"""

from __future__ import annotations

from typing import Any

from mod_agent_cert.hook import AgentCertHook


__all__ = ["AgentCertHook", "install"]


def install(server_state: Any) -> None:
    """Boot hook: register the AgentCertHook against the daemon's
    HookRegistry. Called by agtpd after
    ``--load-module mod_agent_cert``.
    """
    hook = AgentCertHook()
    server_state.hook_registry.register(hook)
