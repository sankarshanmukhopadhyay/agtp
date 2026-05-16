"""
Handler registration for AGTP Python applications.

This is the handler-author-facing registration surface. Decorate a
function with :func:`endpoint` and it is registered in a process-wide
:class:`HandlerRegistry` keyed by ``(method, path)``.

::

    from agtp import EndpointContext, EndpointResponse, endpoint

    @endpoint(method="BOOK", path="/room", errors=["room_unavailable"])
    def book_room(ctx: EndpointContext):
        return EndpointResponse(body={"reservation_id": "..."})

Today, this registry is forward-compatible scaffolding. The daemon
still dispatches through ``server.methods.REGISTRY`` and the TOML
endpoint registry — those keep working unchanged.

When ``mod_python`` lands in M3 step (b), it will read from this
registry on connection startup and feed the resolved (method, path,
function) tuples back to ``agtpd`` so the daemon's ``register`` frame
binds correctly. Step (c) will remove the in-process dispatch path
and leave this as the only handler-registration surface for Python.

The decorator accepts ``errors`` and ``required_scopes`` because they
describe what the handler offers. The operator manifest may surface
these declarations or may declare its own — the daemon's ``register``
frame is the authoritative dispatch contract regardless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from agtp.handlers import EndpointContext, HandlerResult


HandlerFn = Callable[[EndpointContext], HandlerResult]


@dataclass
class RegisteredHandler:
    """One entry in the :class:`HandlerRegistry`.

    Pairs the (method, path) routing key with the handler function and
    the handler's self-declared contract (errors, scopes, description).
    """

    method: str
    path: str
    handler: HandlerFn
    errors: List[str] = field(default_factory=list)
    required_scopes: List[str] = field(default_factory=list)
    description: str = ""


class HandlerRegistry:
    """Process-wide registry of handlers keyed by ``(method, path)``.

    Most users interact with the module-level :data:`registry` instance
    and the :func:`endpoint` decorator. Direct instantiation is useful
    for tests that want isolation and for ``mod_python`` to build its
    own per-connection registry from the daemon's ``register`` frame.
    """

    def __init__(self) -> None:
        self._handlers: Dict[Tuple[str, str], RegisteredHandler] = {}

    def register(
        self,
        handler: HandlerFn,
        *,
        method: str,
        path: str,
        errors: Optional[List[str]] = None,
        required_scopes: Optional[List[str]] = None,
        description: str = "",
    ) -> RegisteredHandler:
        """Register ``handler`` for the given ``(method, path)``.

        Raises ``RuntimeError`` on duplicate registration. Method
        names are normalized to uppercase to match the AGTP catalog.
        """
        key = (method.upper(), path)
        if key in self._handlers:
            existing = self._handlers[key].handler
            raise RuntimeError(
                f"handler already registered for ({method.upper()}, {path}); "
                f"previous: {getattr(existing, '__qualname__', existing)!r}"
            )
        entry = RegisteredHandler(
            method=method.upper(),
            path=path,
            handler=handler,
            errors=list(errors or []),
            required_scopes=list(required_scopes or []),
            description=description,
        )
        self._handlers[key] = entry
        return entry

    def lookup(self, method: str, path: str) -> Optional[RegisteredHandler]:
        return self._handlers.get((method.upper(), path))

    def all(self) -> List[RegisteredHandler]:
        return list(self._handlers.values())

    def clear(self) -> None:
        """Remove every registered handler. Intended for test isolation."""
        self._handlers.clear()

    def __len__(self) -> int:
        return len(self._handlers)

    def __contains__(self, key: Tuple[str, str]) -> bool:
        method, path = key
        return (method.upper(), path) in self._handlers


#: Process-wide registry. Handlers decorated with :func:`endpoint`
#: land here. Tests that need isolation can call ``registry.clear()``
#: in a fixture, or instantiate their own :class:`HandlerRegistry`.
registry = HandlerRegistry()


def endpoint(
    *,
    method: str,
    path: str,
    errors: Optional[List[str]] = None,
    required_scopes: Optional[List[str]] = None,
    description: str = "",
) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator that registers a function as an AGTP endpoint handler.

    ::

        from agtp import EndpointContext, EndpointResponse, endpoint

        @endpoint(
            method="BOOK",
            path="/room",
            errors=["room_unavailable", "invalid_dates"],
        )
        def book_room(ctx: EndpointContext):
            return EndpointResponse(body={"reservation_id": "..."})

    The decorator returns the original function unchanged so it remains
    importable and unit-testable on its own.
    """

    def decorator(fn: HandlerFn) -> HandlerFn:
        registry.register(
            fn,
            method=method,
            path=path,
            errors=errors,
            required_scopes=required_scopes,
            description=description,
        )
        return fn

    return decorator


__all__ = [
    "HandlerFn",
    "HandlerRegistry",
    "RegisteredHandler",
    "endpoint",
    "registry",
]
