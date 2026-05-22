"""
AuditHook — observe-only dispatch hook that writes operator-readable
JSONL audit entries.

mod_audit's role after Tier 2.4 (T2.4 reconciliation): it's an
operator log, not a cryptographic record. The daemon's
:mod:`server.audit_records` store (Phase 6) holds the canonical
signed JWS per audit_id; INSPECT exposes that for verifiers. This
hook complements that by writing a flat, human-/jq-friendly stream
of dispatch metadata to a single log file.

Each entry captures ``(timestamp, method, path, agent_id,
principal_id, request_id, session_id, task_id, authority_scope,
outcome)``, plus optional request input / response body when the
operator opts in.

The old "signed envelope" mode (Ed25519 over canonical JSON,
``{kid, alg, signature, payload}``) is **retired**. The
``signing_service`` keyword argument still exists for back-compat
but is ignored with a one-shot stderr warning the first time it's
passed; the canonical signed record is in audit_records/.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse

from mod_audit.log import AuditLog


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


# Module-level guard so the deprecation warning fires once per
# process, not per request.
_SIGNING_DEPRECATION_WARNED = False


class AuditHook:
    """Dispatch hook that writes one JSONL entry per response.

    Only ``after_dispatch`` is implemented — the hook never
    short-circuits. ``before_dispatch`` is intentionally absent so
    the protocol can omit calling it (the HookRegistry uses
    ``getattr(..., None)`` to detect missing methods).

    Entries are always flat metadata (no signing envelope). The
    daemon's ``server.audit_records`` store holds the canonical
    signed JWS per audit_id; INSPECT
    (``target=audit, audit_id=...``) reads from it.
    """

    def __init__(
        self,
        log: AuditLog,
        *,
        include_input: bool = False,
        include_body: bool = False,
        signing_service: Optional[Any] = None,
    ) -> None:
        self.log = log
        self.include_input = include_input
        self.include_body = include_body
        # Retained for back-compat; ignored. The daemon's
        # audit_records store is the canonical signed source.
        if signing_service is not None:
            global _SIGNING_DEPRECATION_WARNED
            if not _SIGNING_DEPRECATION_WARNED:
                sys.stderr.write(
                    "[mod_audit] signing_service= argument is deprecated "
                    "and ignored. mod_audit now writes flat operator "
                    "metadata only; the canonical signed JWS per "
                    "audit_id lives in audit_records/ (Phase 6). Set "
                    "[audit].attribution_records_enabled = true in "
                    "agtp-server.toml to enable the signed store.\n"
                )
                _SIGNING_DEPRECATION_WARNED = True

    def after_dispatch(
        self,
        spec: Any,
        ctx: EndpointContext,
        result: Union[EndpointResponse, EndpointError],
        server_state: Any,
    ) -> None:
        entry: Dict[str, Any] = {
            "timestamp": _utc_now_iso(),
            "method": ctx.method,
            "path": ctx.path,
            "agent_id": ctx.agent_id,
            "request_id": ctx.request_id,
        }
        if ctx.principal_id:
            entry["principal_id"] = ctx.principal_id
        if ctx.session_id:
            entry["session_id"] = ctx.session_id
        if ctx.task_id:
            entry["task_id"] = ctx.task_id
        if ctx.authority_scope:
            entry["authority_scope"] = list(ctx.authority_scope)
        if self.include_input and ctx.input:
            entry["input"] = ctx.input

        if isinstance(result, EndpointResponse):
            entry["outcome"] = "ok"
            entry["status"] = result.status
            if self.include_body:
                entry["body"] = result.body
        elif isinstance(result, EndpointError):
            entry["outcome"] = "endpoint_error"
            entry["error_code"] = result.code
            entry["error_message"] = result.message
            if result.details:
                entry["error_details"] = result.details
        else:
            entry["outcome"] = "unknown"

        self.log.write(entry)
