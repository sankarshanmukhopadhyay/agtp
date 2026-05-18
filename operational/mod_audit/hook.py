"""
AuditHook — observe-only dispatch hook that writes audit entries.

Each entry captures the (method, path, agent_id, principal_id,
outcome) plus optional request input and response body. The shape
mirrors what AGTP-LOG's COSE_Sign1 receipts will carry once full
SCITT support lands; today's signed envelope is Ed25519 over
canonical JSON, which is the bridge — same key material, same
signing service — until the COSE/SCITT wrapper arrives.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse

from mod_audit.log import AuditLog


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


class AuditHook:
    """Dispatch hook that writes one JSONL entry per response.

    Only ``after_dispatch`` is implemented — the hook never
    short-circuits. ``before_dispatch`` is intentionally absent so
    the protocol can omit calling it (the HookRegistry uses
    ``getattr(..., None)`` to detect missing methods).

    When ``signing_service`` is supplied, each receipt is signed
    with Ed25519 over its canonical-JSON payload. Signed envelopes
    carry ``kid``, ``alg``, ``signature``, and ``payload`` fields;
    unsigned receipts (no signing_service) flatten the payload to
    the top level the way M9's v1 shape did.
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
        self.signing_service = signing_service

    def after_dispatch(
        self,
        spec: Any,
        ctx: EndpointContext,
        result: Union[EndpointResponse, EndpointError],
        server_state: Any,
    ) -> None:
        payload: Dict[str, Any] = {
            "timestamp": _utc_now_iso(),
            "method": ctx.method,
            "path": ctx.path,
            "agent_id": ctx.agent_id,
            "request_id": ctx.request_id,
        }
        if ctx.principal_id:
            payload["principal_id"] = ctx.principal_id
        if ctx.session_id:
            payload["session_id"] = ctx.session_id
        if ctx.task_id:
            payload["task_id"] = ctx.task_id
        if ctx.authority_scope:
            payload["authority_scope"] = list(ctx.authority_scope)
        if self.include_input and ctx.input:
            payload["input"] = ctx.input

        if isinstance(result, EndpointResponse):
            payload["outcome"] = "ok"
            payload["status"] = result.status
            if self.include_body:
                payload["body"] = result.body
        elif isinstance(result, EndpointError):
            payload["outcome"] = "endpoint_error"
            payload["error_code"] = result.code
            payload["error_message"] = result.message
            if result.details:
                payload["error_details"] = result.details
        else:
            payload["outcome"] = "unknown"

        if self.signing_service is not None:
            signature_bytes = self.signing_service.sign_canonical(payload)
            entry: Dict[str, Any] = {
                "kid": self.signing_service.key_id,
                "alg": "Ed25519",
                "signature": base64.urlsafe_b64encode(signature_bytes)
                    .rstrip(b"=").decode("ascii"),
                "payload": payload,
            }
        else:
            # Unsigned: payload fields at top level (v1 shape).
            entry = payload

        self.log.write(entry)
