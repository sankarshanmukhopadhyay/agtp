"""Tests for the mod_audit operational module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse
from core.endpoint import EndpointSpec, SemanticBlock
from mod_audit.hook import AuditHook
from mod_audit.log import AuditLog


# ---------------------------------------------------------------------------
# AuditLog: JSONL writer.
# ---------------------------------------------------------------------------


def test_log_appends_jsonl(tmp_path: Path) -> None:
    log = AuditLog(str(tmp_path / "audit.log"))
    log.write({"a": 1})
    log.write({"b": 2})
    log.close()

    lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[1]) == {"b": 2}


def test_log_creates_parent_directory(tmp_path: Path) -> None:
    log_path = tmp_path / "nested" / "deeper" / "audit.log"
    log = AuditLog(str(log_path))
    log.write({"ok": True})
    log.close()
    assert log_path.exists()


def test_log_degrades_silently_on_io_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the first open() call inside AuditLog.write to raise.
    log = AuditLog(str(tmp_path / "audit.log"))
    import builtins as _builtins
    real_open = _builtins.open

    def failing_open(path, *args, **kwargs):
        if "audit.log" in str(path):
            raise OSError("disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)

    log.write({"a": 1})  # first attempt: triggers OSError, logs warning, _open_failed=True
    log.write({"b": 2})  # second attempt: silently skipped
    log.close()

    captured = capsys.readouterr()
    assert "mod_audit" in captured.err
    assert "disk full" in captured.err
    # Ensure only one warning surfaces, not one per write.
    assert captured.err.count("disk full") == 1


# ---------------------------------------------------------------------------
# AuditHook: writes the right entries.
# ---------------------------------------------------------------------------


def _spec() -> EndpointSpec:
    return EndpointSpec(
        name="BOOK",
        path="/room",
        description="Book a room.",
        semantic=SemanticBlock(
            intent="Book.",
            actor="agent",
            outcome="Reservation id.",
            capability="transaction",
            confidence=0.9,
            impact="reversible",
            is_idempotent=False,
        ),
    )


def _ctx() -> EndpointContext:
    return EndpointContext(
        input={"room_type": "double"},
        agent_id="agent-1",
        principal_id="chris@example.com",
        authority_scope=["booking:write"],
        session_id="sess-1",
        task_id="task-1",
        request_id="req-1",
        method="BOOK",
        path="/room",
    )


def test_hook_writes_success_entry(tmp_path: Path) -> None:
    log = AuditLog(str(tmp_path / "audit.log"))
    hook = AuditHook(log)
    hook.after_dispatch(
        _spec(),
        _ctx(),
        EndpointResponse(body={"reservation_id": "res-1"}, status=200),
        server_state=None,
    )
    log.close()

    entry = json.loads((tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()[0])
    assert entry["method"] == "BOOK"
    assert entry["path"] == "/room"
    assert entry["agent_id"] == "agent-1"
    assert entry["principal_id"] == "chris@example.com"
    assert entry["session_id"] == "sess-1"
    assert entry["task_id"] == "task-1"
    assert entry["authority_scope"] == ["booking:write"]
    assert entry["request_id"] == "req-1"
    assert entry["outcome"] == "ok"
    assert entry["status"] == 200
    # Defaults: input and body are NOT included.
    assert "input" not in entry
    assert "body" not in entry
    assert "timestamp" in entry


def test_hook_writes_error_entry(tmp_path: Path) -> None:
    log = AuditLog(str(tmp_path / "audit.log"))
    hook = AuditHook(log)
    hook.after_dispatch(
        _spec(),
        _ctx(),
        EndpointError(
            code="room_unavailable",
            message="no rooms",
            details={"hotel": "Grand"},
        ),
        server_state=None,
    )
    log.close()
    entry = json.loads((tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()[0])
    assert entry["outcome"] == "endpoint_error"
    assert entry["error_code"] == "room_unavailable"
    assert entry["error_message"] == "no rooms"
    assert entry["error_details"] == {"hotel": "Grand"}


def test_hook_includes_input_when_enabled(tmp_path: Path) -> None:
    log = AuditLog(str(tmp_path / "audit.log"))
    hook = AuditHook(log, include_input=True)
    hook.after_dispatch(
        _spec(),
        _ctx(),
        EndpointResponse(body={}, status=200),
        server_state=None,
    )
    log.close()
    entry = json.loads((tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()[0])
    assert entry["input"] == {"room_type": "double"}


def test_hook_includes_body_when_enabled(tmp_path: Path) -> None:
    log = AuditLog(str(tmp_path / "audit.log"))
    hook = AuditHook(log, include_body=True)
    hook.after_dispatch(
        _spec(),
        _ctx(),
        EndpointResponse(body={"reservation_id": "res-1"}, status=200),
        server_state=None,
    )
    log.close()
    entry = json.loads((tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()[0])
    assert entry["body"] == {"reservation_id": "res-1"}


def test_hook_has_no_before_dispatch_method() -> None:
    """Audit is observe-only — the hook MUST NOT define before_dispatch,
    so the HookRegistry's getattr(...,None) check skips it."""
    hook = AuditHook(AuditLog("/dev/null"))
    assert not hasattr(hook, "before_dispatch")


# ---------------------------------------------------------------------------
# install() boot path.
# ---------------------------------------------------------------------------


def test_install_registers_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGTP_AUDIT_ENABLED", "1")
    monkeypatch.setenv("AGTP_AUDIT_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv("AGTP_AUDIT_INCLUDE_INPUT", "1")
    monkeypatch.setenv("AGTP_AUDIT_INCLUDE_BODY", "0")

    from server.hooks import HookRegistry
    from mod_audit import install

    class _FakeState:
        hook_registry = HookRegistry()

    state = _FakeState()
    install(state)
    assert state.hook_registry.count() == 1
    hook = state.hook_registry.all()[0]
    assert isinstance(hook, AuditHook)
    assert hook.include_input is True
    assert hook.include_body is False


def test_install_respects_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGTP_AUDIT_ENABLED", "0")

    from server.hooks import HookRegistry
    from mod_audit import install

    class _FakeState:
        hook_registry = HookRegistry()

    state = _FakeState()
    install(state)
    assert state.hook_registry.count() == 0
