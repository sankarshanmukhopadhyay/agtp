"""
Dispatch-pipeline hook integration tests.

Exercises the HookRegistry and the wiring in
server.methods._serve_endpoint that consults hooks before and after
each handler call.
"""

from __future__ import annotations

from typing import Any, Optional, Union

import pytest

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse
from server.hooks import (
    DispatchHook,
    DispatchHookError,
    HookRegistry,
)


# ---------------------------------------------------------------------------
# HookRegistry mechanics.
# ---------------------------------------------------------------------------


class _RecordingHook:
    """Test hook that records every call."""

    def __init__(
        self,
        *,
        short_circuit: Optional[Union[EndpointResponse, EndpointError]] = None,
        raise_before: Optional[Exception] = None,
        raise_after: Optional[Exception] = None,
    ) -> None:
        self.short_circuit = short_circuit
        self.raise_before = raise_before
        self.raise_after = raise_after
        self.before_calls = 0
        self.after_calls = 0

    def before_dispatch(self, spec, ctx, server_state):
        self.before_calls += 1
        if self.raise_before is not None:
            raise self.raise_before
        return self.short_circuit

    def after_dispatch(self, spec, ctx, result, server_state):
        self.after_calls += 1
        if self.raise_after is not None:
            raise self.raise_after


def _ctx() -> EndpointContext:
    return EndpointContext(input={}, method="QUERY", path="/", agent_id="a", request_id="r")


def test_run_before_returns_first_short_circuit() -> None:
    """First hook to return non-None wins; subsequent hooks are skipped."""
    reg = HookRegistry()
    sentinel = EndpointResponse(body={"from": "hook-2"})
    hook1 = _RecordingHook()  # returns None
    hook2 = _RecordingHook(short_circuit=sentinel)
    hook3 = _RecordingHook(short_circuit=EndpointResponse(body={"from": "hook-3"}))
    reg.register(hook1)
    reg.register(hook2)
    reg.register(hook3)
    outcome = reg.run_before(None, _ctx(), None)
    assert outcome is sentinel
    assert hook1.before_calls == 1
    assert hook2.before_calls == 1
    assert hook3.before_calls == 0  # short-circuited by hook2


def test_run_before_returns_none_when_no_hook_short_circuits() -> None:
    reg = HookRegistry()
    reg.register(_RecordingHook())
    reg.register(_RecordingHook())
    assert reg.run_before(None, _ctx(), None) is None


def test_run_after_calls_every_hook() -> None:
    reg = HookRegistry()
    hooks = [_RecordingHook() for _ in range(3)]
    for h in hooks:
        reg.register(h)
    reg.run_after(None, _ctx(), EndpointResponse(body={}), None)
    for h in hooks:
        assert h.after_calls == 1


def test_hook_before_failure_is_logged_and_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hook that throws non-DispatchHookError doesn't break dispatch."""
    reg = HookRegistry()
    reg.register(_RecordingHook(raise_before=RuntimeError("boom")))
    good = _RecordingHook(short_circuit=EndpointResponse(body={"ok": True}))
    reg.register(good)
    outcome = reg.run_before(None, _ctx(), None)
    assert isinstance(outcome, EndpointResponse)
    assert outcome.body == {"ok": True}
    assert "boom" in capsys.readouterr().err


def test_hook_after_failure_is_logged_and_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reg = HookRegistry()
    reg.register(_RecordingHook(raise_after=RuntimeError("boom")))
    good = _RecordingHook()
    reg.register(good)
    reg.run_after(None, _ctx(), EndpointResponse(body={}), None)
    assert good.after_calls == 1
    assert "boom" in capsys.readouterr().err


def test_dispatch_hook_error_propagates() -> None:
    """A hook raising DispatchHookError aborts dispatch with that error."""
    reg = HookRegistry()
    reg.register(_RecordingHook(raise_before=DispatchHookError("misconfigured")))
    with pytest.raises(DispatchHookError, match="misconfigured"):
        reg.run_before(None, _ctx(), None)


def test_missing_method_treated_as_noop() -> None:
    """Hooks may implement only one of before/after; the other is skipped."""
    reg = HookRegistry()

    class _BeforeOnly:
        def before_dispatch(self, spec, ctx, server_state):
            return None

    class _AfterOnly:
        def after_dispatch(self, spec, ctx, result, server_state):
            self.called = True

    after = _AfterOnly()
    reg.register(_BeforeOnly())
    reg.register(after)

    reg.run_before(None, _ctx(), None)
    reg.run_after(None, _ctx(), EndpointResponse(body={}), None)
    assert getattr(after, "called", False) is True


# ---------------------------------------------------------------------------
# Wiring: AgentRegistry has a hook_registry slot.
# ---------------------------------------------------------------------------


def test_agent_registry_has_hook_registry_slot(tmp_path) -> None:
    from server.main import AgentRegistry

    reg = AgentRegistry(tmp_path)
    assert hasattr(reg, "hook_registry")
    assert isinstance(reg.hook_registry, HookRegistry)
    assert reg.hook_registry.count() == 0
