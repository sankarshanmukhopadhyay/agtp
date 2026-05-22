"""
Tests for mod_audit's reconciliation (Tier 2.4).

After T2.4, mod_audit is the operator-readable log. The signed
record per audit_id lives in the daemon's audit_records store. This
test file used to exercise mod_audit's own Ed25519 signing path;
that path is retired. These tests now confirm:

  1. AuditHook always writes flat metadata entries (no envelope).
  2. The deprecated ``signing_service`` argument is accepted for
     back-compat and ignored with a one-shot warning.
  3. The retired ``AGTP_AUDIT_SIGN_RECEIPTS`` env var triggers a
     stderr message on install.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from agtp.handlers import EndpointContext, EndpointResponse
from core.endpoint import EndpointSpec, SemanticBlock
from mod_audit.hook import AuditHook
from mod_audit.log import AuditLog


def _spec() -> EndpointSpec:
    return EndpointSpec(
        name="BOOK",
        path="/room",
        description="Book.",
        semantic=SemanticBlock(
            intent="Book.",
            actor="agent",
            outcome="Done.",
            capability="transaction",
            confidence=0.9,
            impact="reversible",
            is_idempotent=False,
        ),
    )


def _ctx() -> EndpointContext:
    return EndpointContext(
        input={},
        agent_id="agent-1",
        method="BOOK",
        path="/room",
    )


def _result() -> EndpointResponse:
    return EndpointResponse(body={"ok": True}, status=200)


def _read_entries(path: Path) -> list:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines()]


# ---------------------------------------------------------------------------
# Flat-metadata shape (canonical path).
# ---------------------------------------------------------------------------


def test_audit_entry_is_flat_metadata(tmp_path: Path) -> None:
    log = AuditLog(str(tmp_path / "audit.log"))
    hook = AuditHook(log)
    hook.after_dispatch(_spec(), _ctx(), _result(), None)
    log.close()

    entries = _read_entries(tmp_path / "audit.log")
    assert len(entries) == 1
    entry = entries[0]
    # Flat shape — no envelope wrapping.
    assert "kid" not in entry
    assert "alg" not in entry
    assert "signature" not in entry
    assert "payload" not in entry
    # Metadata fields at top level.
    assert entry["method"] == "BOOK"
    assert entry["path"] == "/room"
    assert entry["agent_id"] == "agent-1"
    assert entry["outcome"] == "ok"
    assert entry["status"] == 200


# ---------------------------------------------------------------------------
# Back-compat: signing_service= argument is accepted and ignored.
# ---------------------------------------------------------------------------


def test_signing_service_argument_is_accepted_and_warned(tmp_path: Path) -> None:
    """Passing signing_service= no longer produces a signed envelope
    — the daemon's audit_records is the canonical signed store. The
    hook accepts the kwarg for back-compat and emits a one-shot
    stderr warning the first time it's seen."""
    # Reset the module's one-shot guard so this test sees the warning.
    import mod_audit.hook as _hook_mod
    _hook_mod._SIGNING_DEPRECATION_WARNED = False

    log = AuditLog(str(tmp_path / "audit.log"))
    captured = io.StringIO()
    with mock.patch.object(sys, "stderr", captured):
        hook = AuditHook(log, signing_service=object())  # any non-None
    msg = captured.getvalue()
    assert "deprecated" in msg.lower()
    assert "audit_records" in msg

    # Subsequent constructions don't repeat the warning.
    captured2 = io.StringIO()
    with mock.patch.object(sys, "stderr", captured2):
        AuditHook(log, signing_service=object())
    assert captured2.getvalue() == ""

    # And the output is still flat — no signed envelope.
    hook.after_dispatch(_spec(), _ctx(), _result(), None)
    log.close()
    entries = _read_entries(tmp_path / "audit.log")
    assert "signature" not in entries[0]


# ---------------------------------------------------------------------------
# install() — AGTP_AUDIT_SIGN_RECEIPTS triggers retirement notice.
# ---------------------------------------------------------------------------


def test_install_warns_on_retired_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from mod_audit import install
    from server.hooks import HookRegistry

    monkeypatch.setenv("AGTP_AUDIT_SIGN_RECEIPTS", "1")
    monkeypatch.setenv("AGTP_AUDIT_PATH", str(tmp_path / "audit.log"))

    class FakeState:
        def __init__(self) -> None:
            self.hook_registry = HookRegistry()
            self.signing_service = None

    state = FakeState()
    captured = io.StringIO()
    with mock.patch.object(sys, "stderr", captured):
        install(state)
    msg = captured.getvalue()
    assert "retired" in msg.lower()
    assert "records_root" in msg or "audit_records" in msg
    # Hook still registers — operator metadata logging continues.
    assert state.hook_registry.count() == 1


def test_install_silent_without_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from mod_audit import install
    from server.hooks import HookRegistry

    monkeypatch.delenv("AGTP_AUDIT_SIGN_RECEIPTS", raising=False)
    monkeypatch.setenv("AGTP_AUDIT_PATH", str(tmp_path / "audit.log"))

    class FakeState:
        def __init__(self) -> None:
            self.hook_registry = HookRegistry()
            self.signing_service = None

    state = FakeState()
    captured = io.StringIO()
    with mock.patch.object(sys, "stderr", captured):
        install(state)
    # No retirement message in the common path.
    assert "retired" not in captured.getvalue().lower()
