"""
AgentCertHook — before_dispatch gate driven by Agent Certificate extensions.

The hook runs once per request before the handler. It reads the
Agent Cert extensions surfaced on :class:`EndpointContext` (populated
by the dispatcher from the verified peer cert) and refuses requests
that exceed the cert-bound authority or cross a governance-zone
boundary.

Cost: O(scopes) per request, where ``scopes`` is the number of tokens
in the inbound Authority-Scope header — typically 1-3. The cert's
scope commitment is parsed once at session-establishment time (the
TLS handshake) and held in a set for membership lookup. Zone check
is a single string equality.

This is the Phase-3 operational component referenced by AGTP-CERT
§5.2: a Scope-Enforcement-Point sitting in front of an agent's
handler can refuse requests at the wire layer without ever invoking
the body parser.
"""

from __future__ import annotations

from typing import Any, Optional

from agtp.handlers import EndpointContext
from core import status as _status
from core import wire


class AgentCertHook:
    """Dispatch hook that gates requests against Agent-Cert extensions.

    Only ``before_dispatch`` is implemented. The hook either passes
    through (returns ``None``) or short-circuits with a wire response
    carrying the appropriate AGTP-specific status code (455 / 457).
    """

    def before_dispatch(
        self,
        spec: Any,
        ctx: EndpointContext,
        server_state: Any,
    ) -> Optional[wire.AGTPResponse]:
        """Enforce Agent-Cert-derived constraints.

        Returns ``None`` to pass through (no verified cert / cert has
        no relevant extensions / all checks passed). Returns a
        :class:`wire.AGTPResponse` on scope or zone violation.
        """
        # No verified cert → no extension-based enforcement to apply.
        # The daemon's soft-deny gate still enforces scope against the
        # agent's declared scopes; this hook is purely additive.
        if not ctx.agent_verified:
            return None

        extensions = ctx.agent_cert_extensions or {}
        if not extensions:
            return None

        # Authority-Scope check. The cert commits to a set of scope
        # tokens via the authority-scope-commitment extension. Every
        # token claimed in the inbound Authority-Scope header MUST be
        # a member of that committed set.
        committed_scopes = extensions.get("authority_scopes")
        if committed_scopes is not None:
            committed_set = set(committed_scopes)
            claimed = list(ctx.authority_scope or [])
            outside = [s for s in claimed if s not in committed_set]
            if outside:
                return _status._build(
                    _status.SCOPE_VIOLATION,
                    body={
                        "error": {
                            "code": "scope-outside-commitment",
                            "message": (
                                f"Authority-Scope token(s) {outside!r} are "
                                f"not covered by the agent's certificate "
                                f"authority-scope-commitment"
                            ),
                            "outside_commitment": outside,
                            "claimed": claimed,
                            "committed": sorted(committed_set),
                            "agent_id": ctx.agent_id,
                        }
                    },
                )

        # Governance-Zone check. When the cert pins the agent to a
        # specific zone AND the request declares one, they MUST match.
        # Requests without AGTP-Zone-ID inherit the cert's zone
        # implicitly; we don't refuse those because zone declaration
        # is opt-in at the request layer.
        cert_zone = extensions.get("governance_zone")
        if cert_zone:
            request_zone = (
                ctx.headers.get("agtp-zone-id", "") if ctx.headers else ""
            )
            if request_zone and request_zone != cert_zone:
                return _status.zone_violation(
                    target_zone=cert_zone,
                    request_zone=request_zone,
                    explanation=(
                        f"AGTP-Zone-ID header {request_zone!r} does not "
                        f"match the agent's certificate governance-zone "
                        f"{cert_zone!r}"
                    ),
                )

        return None
