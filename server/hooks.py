"""
Dispatch-hook surface for operational modules.

Operational modules (``mod_cache``, ``mod_audit``, future ``mod_metrics``,
…) plug into the daemon's dispatch pipeline by registering a
:class:`DispatchHook` against the :class:`HookRegistry`. The
dispatcher consults the registry around each handler call.

Two callback shapes:

  * ``before_dispatch(spec, ctx, server_state)`` — returns an
    :class:`~agtp.handlers.EndpointResponse` or
    :class:`~agtp.handlers.EndpointError` to short-circuit dispatch,
    or ``None`` to pass through. Used by ``mod_cache`` to serve
    cached responses without invoking the handler.
  * ``after_dispatch(spec, ctx, result, server_state)`` — observe
    only; return value is ignored. Used by ``mod_audit`` to record
    handler outcomes.

Hooks run in registration order. The first ``before_dispatch`` that
returns a non-``None`` value wins; subsequent ``before_dispatch``
hooks are skipped, but ``after_dispatch`` runs for every registered
hook regardless of whether the response came from a hook or the
handler.

Unhandled exceptions in a hook are logged to stderr and ignored —
hooks are auxiliary; one misbehaving operational module must not
break dispatch. Operational modules that need to fail loudly should
raise explicit ``DispatchHookError`` instances, which the dispatcher
DOES propagate.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any, List, Optional, Protocol, Union

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse


HookOutcome = Optional[Union[EndpointResponse, EndpointError]]


class DispatchHookError(Exception):
    """Raised by a hook that wants to abort dispatch with a clear error.

    Propagates to the caller. Use this for misconfiguration or
    correctness failures; for normal soft-skip behavior, return
    ``None`` from ``before_dispatch`` instead.
    """


class DispatchHook(Protocol):
    """Protocol for an operational-module hook.

    Implementations may provide either or both methods. The dispatcher
    invokes whichever methods are defined; missing methods are
    treated as no-ops.
    """

    def before_dispatch(
        self,
        spec: Any,
        ctx: EndpointContext,
        server_state: Any,
    ) -> HookOutcome:
        """Optionally short-circuit dispatch.

        Return an EndpointResponse / EndpointError to skip the handler
        and use that result. Return ``None`` to pass through to the
        handler (or to subsequent hooks).
        """
        ...  # pragma: no cover - protocol declaration

    def after_dispatch(
        self,
        spec: Any,
        ctx: EndpointContext,
        result: Union[EndpointResponse, EndpointError],
        server_state: Any,
    ) -> None:
        """Observe the dispatch outcome. Return value is ignored."""
        ...  # pragma: no cover - protocol declaration


class HookRegistry:
    """Per-daemon ordered list of dispatch hooks.

    Construction-time empty; operational modules register their hooks
    during ``--load-module`` boot via :meth:`register`. The dispatcher
    invokes :meth:`run_before` and :meth:`run_after` around each
    handler call.
    """

    def __init__(self) -> None:
        self._hooks: List[Any] = []

    def register(self, hook: Any) -> None:
        """Append a hook to the registry. Order matters: first
        ``before_dispatch`` to return a response wins."""
        self._hooks.append(hook)

    def count(self) -> int:
        return len(self._hooks)

    def all(self) -> List[Any]:
        return list(self._hooks)

    def run_before(
        self,
        spec: Any,
        ctx: EndpointContext,
        server_state: Any,
    ) -> HookOutcome:
        """Call each hook's ``before_dispatch`` until one returns a
        non-``None`` outcome. Returns that outcome, or ``None`` if no
        hook short-circuited."""
        for hook in self._hooks:
            before = getattr(hook, "before_dispatch", None)
            if before is None:
                continue
            try:
                outcome = before(spec, ctx, server_state)
            except DispatchHookError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log_hook_failure(hook, "before_dispatch", exc)
                continue
            if outcome is not None:
                return outcome
        return None

    def run_after(
        self,
        spec: Any,
        ctx: EndpointContext,
        result: Union[EndpointResponse, EndpointError],
        server_state: Any,
    ) -> None:
        """Call every hook's ``after_dispatch``. Exceptions are caught
        and logged; never raised."""
        for hook in self._hooks:
            after = getattr(hook, "after_dispatch", None)
            if after is None:
                continue
            try:
                after(spec, ctx, result, server_state)
            except DispatchHookError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log_hook_failure(hook, "after_dispatch", exc)

    @staticmethod
    def _log_hook_failure(hook: Any, method: str, exc: Exception) -> None:
        hook_name = type(hook).__name__
        print(
            f"[server] dispatch hook {hook_name}.{method} raised "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)


__all__ = [
    "DispatchHook",
    "DispatchHookError",
    "HookOutcome",
    "HookRegistry",
]
