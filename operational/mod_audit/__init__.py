"""
mod_audit — append-only audit log of every endpoint dispatch.

Loaded by ``agtpd`` via ``--load-module mod_audit``. The module's
``install(server_state)`` function registers an :class:`AuditHook`
against the daemon's :class:`HookRegistry`. The hook writes one
JSONL entry per dispatch to a configured log file.

The on-disk shape is JSON Lines. Receipts can now be **Ed25519
signed** when the daemon has a loaded ``SigningService``
(``[signing].enabled = true`` in ``agtp-server.toml``) and the
operator sets ``AGTP_AUDIT_SIGN_RECEIPTS=1``. Signed entries carry
``kid``, ``alg``, and ``signature`` fields alongside the payload
fields. The full AGTP-LOG specification (draft-hood-agtp-log-00)
calls for COSE_Sign1 receipts written to a SCITT-style transparency
log; the current Ed25519-over-canonical-JSON form is the bridge —
same key material, same signing service — until the COSE/SCITT
wrapper lands.

Configuration via environment variables:

  * ``AGTP_AUDIT_PATH`` — file path (default: ``./agtp-audit.log``)
  * ``AGTP_AUDIT_INCLUDE_INPUT`` — ``"1"`` to include request input
    in the log entry; ``"0"`` to omit (default: ``"0"``, omit by
    default since inputs may carry PII)
  * ``AGTP_AUDIT_INCLUDE_BODY`` — ``"1"`` to include response body
    (default: ``"0"``)
  * ``AGTP_AUDIT_SIGN_RECEIPTS`` — ``"1"`` to Ed25519-sign each
    receipt (requires daemon-side ``[signing]`` enabled)
"""

from __future__ import annotations

import os
from typing import Any

from mod_audit.hook import AuditHook
from mod_audit.log import AuditLog


__all__ = ["AuditHook", "AuditLog", "install"]


def install(server_state: Any) -> None:
    """Boot hook: open the audit log file and register the dispatch hook.

    When ``AGTP_AUDIT_SIGN_RECEIPTS=1`` AND the daemon has a loaded
    ``SigningService``, each receipt is Ed25519-signed.
    """
    if os.environ.get("AGTP_AUDIT_ENABLED", "1") == "0":
        return
    log = AuditLog(
        path=os.environ.get("AGTP_AUDIT_PATH", "./agtp-audit.log"),
    )
    sign_receipts = os.environ.get("AGTP_AUDIT_SIGN_RECEIPTS", "0") == "1"
    signing_service = getattr(server_state, "signing_service", None)
    if sign_receipts and signing_service is None:
        import sys as _sys
        print(
            "[mod_audit] AGTP_AUDIT_SIGN_RECEIPTS=1 but the daemon "
            "has no signing service configured. Receipts will be "
            "unsigned. Set [signing] in agtp-server.toml to enable.",
            file=_sys.stderr,
        )
        sign_receipts = False
    hook = AuditHook(
        log=log,
        include_input=os.environ.get("AGTP_AUDIT_INCLUDE_INPUT", "0") == "1",
        include_body=os.environ.get("AGTP_AUDIT_INCLUDE_BODY", "0") == "1",
        signing_service=signing_service if sign_receipts else None,
    )
    server_state.hook_registry.register(hook)
