"""
mod_audit — append-only operator-readable audit log.

Loaded by ``agtpd`` via ``--load-module mod_audit``. The module's
``install(server_state)`` function registers an :class:`AuditHook`
against the daemon's :class:`HookRegistry`. The hook writes one
JSONL entry per dispatch to a configured log file.

**Scope (after Tier 2.4 reconciliation):**
mod_audit is the **operator-readable** audit surface — flat JSON
metadata you can ``tail -f``, ``jq``, or feed into Loki/Splunk.
The **cryptographically signed** record per audit_id lives in
the daemon's ``audit_records/`` store (Phase 6), readable via
INSPECT (``target=audit, audit_id=...``). Don't use mod_audit for
verifiability; use it for visibility.

The on-disk shape is JSON Lines. One entry per dispatch with
``timestamp``, ``method``, ``path``, ``agent_id``, ``request_id``,
optional ``principal_id`` / ``session_id`` / ``task_id`` /
``authority_scope``, and an ``outcome`` (``ok`` /
``endpoint_error`` / ``unknown``) plus status / error fields.

Configuration via environment variables:

  * ``AGTP_AUDIT_ENABLED`` — set to ``"0"`` to skip registration
    without unloading the module (default: ``"1"``)
  * ``AGTP_AUDIT_PATH`` — file path (default: ``./agtp-audit.log``)
  * ``AGTP_AUDIT_INCLUDE_INPUT`` — ``"1"`` to include request input
    in the log entry; ``"0"`` to omit (default: ``"0"``; inputs may
    carry PII)
  * ``AGTP_AUDIT_INCLUDE_BODY`` — ``"1"`` to include response body
    (default: ``"0"``)

The legacy ``AGTP_AUDIT_SIGN_RECEIPTS`` environment variable is
retired; the signed-record path moved to the daemon's
``audit_records`` store. mod_audit ignores the env var with a
one-shot stderr warning if set.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from mod_audit.hook import AuditHook
from mod_audit.log import AuditLog


__all__ = ["AuditHook", "AuditLog", "install"]


def install(server_state: Any) -> None:
    """Boot hook: open the audit log file and register the dispatch
    hook. No-op when ``AGTP_AUDIT_ENABLED=0``.
    """
    if os.environ.get("AGTP_AUDIT_ENABLED", "1") == "0":
        return
    if os.environ.get("AGTP_AUDIT_SIGN_RECEIPTS"):
        sys.stderr.write(
            "[mod_audit] AGTP_AUDIT_SIGN_RECEIPTS is retired. The "
            "canonical signed-JWS audit store lives in the daemon's "
            "[audit].records_root directory; enable it via "
            "[audit].attribution_records_enabled = true. mod_audit "
            "continues writing flat operator metadata.\n"
        )
    log = AuditLog(
        path=os.environ.get("AGTP_AUDIT_PATH", "./agtp-audit.log"),
    )
    hook = AuditHook(
        log=log,
        include_input=os.environ.get("AGTP_AUDIT_INCLUDE_INPUT", "0") == "1",
        include_body=os.environ.get("AGTP_AUDIT_INCLUDE_BODY", "0") == "1",
    )
    server_state.hook_registry.register(hook)
