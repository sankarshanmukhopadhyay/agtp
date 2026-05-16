"""
Module-side gateway client for Python handlers.

A :class:`GatewayClient` connects to ``agtpd`` over the gateway
socket, performs the handshake, receives the daemon's endpoint
registration, resolves each ``handler_reference`` against
``agtp.registry``, then serves request frames in a synchronous loop.

The client is intentionally simple: one connection, one in-flight
request at a time. For higher concurrency, the operator runs N
``mod_python`` processes pointing at the same gateway socket — the
daemon round-robins requests across whichever connections are idle.
(Multi-connection concurrency is a M3 step (c) / M4 task; v1 single
connection per process is enough for the first deployments.)
"""

from __future__ import annotations

import importlib
import os
import socket
import sys
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse
from agtp.registry import HandlerRegistry, registry as global_registry
from core.gateway import (
    GATEWAY_VERSION,
    FrameDecodeError,
    FrameTooLargeError,
    GatewayError,
    read_frame,
    write_frame,
)


class ModuleError(Exception):
    """Raised when the module cannot operate (handshake failed, etc.)."""


# ---------------------------------------------------------------------------
# GatewayClient.
# ---------------------------------------------------------------------------


class GatewayClient:
    """One module-side gateway connection.

    Construct, optionally :meth:`load_module` Python modules that
    register handlers, then call :meth:`run`. ``run`` connects to the
    daemon, performs the handshake, resolves handler references
    against the local registry, then enters the request loop. The
    loop returns when the daemon sends ``goodbye``, when the socket
    closes, or when :meth:`stop` is called from another thread.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        registry: Optional[HandlerRegistry] = None,
        module_id: str = "mod_python",
        module_version: str = "0.1.0",
    ) -> None:
        self.socket_path = socket_path
        self.registry = registry if registry is not None else global_registry
        self.module_id = module_id
        self.module_version = module_version
        self._sock: Optional[socket.socket] = None
        self._reader = None
        self._writer = None
        # Map (method, path) -> resolved handler callable.
        self._bindings: Dict[Tuple[str, str], Callable[..., Any]] = {}
        self._stop = False

    # ----- Lifecycle -----

    def load_module(self, dotted_path: str) -> None:
        """Import a Python module so its ``@endpoint``-decorated handlers
        register themselves in :data:`agtp.registry`.

        Called before :meth:`run` so that when the daemon's
        ``register`` frame arrives, the named handler references
        resolve against an already-populated registry.
        """
        importlib.import_module(dotted_path)

    def run(self) -> None:
        """Connect, handshake, register, serve until disconnect/goodbye."""
        self._connect()
        try:
            self._handshake()
            self._serve_loop()
        finally:
            self._close()

    def stop(self) -> None:
        """Request an orderly shutdown of the request loop.

        The request loop checks ``_stop`` between frames; an in-flight
        request still completes. To force an immediate close, the
        caller closes the underlying socket.
        """
        self._stop = True

    # ----- Internals -----

    def _connect(self) -> None:
        if self.socket_path.startswith(("127.0.0.1:", "[::1]:", "localhost:")):
            host, _, port_str = self.socket_path.rpartition(":")
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.connect((host, int(port_str)))
        else:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.connect(self.socket_path)
        self._reader = self._sock.makefile("rb")
        self._writer = self._sock.makefile("wb")

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _handshake(self) -> None:
        assert self._reader is not None and self._writer is not None

        # 1. Send hello.
        write_frame(self._writer, {
            "type": "hello",
            "gateway_versions": [GATEWAY_VERSION],
            "module": {
                "id": self.module_id,
                "version": self.module_version,
                "runtime": (
                    f"CPython {sys.version_info.major}.{sys.version_info.minor}"
                ),
                "pid": os.getpid(),
            },
            "capabilities": ["registered_function"],
        })

        # 2. Read welcome.
        welcome = read_frame(self._reader)
        if welcome.get("type") == "error":
            raise ModuleError(
                f"daemon refused handshake: {welcome.get('code')}: "
                f"{welcome.get('message')}"
            )
        if welcome.get("type") != "welcome":
            raise ModuleError(
                f"expected welcome, got type={welcome.get('type')!r}"
            )
        chosen = welcome.get("gateway_version")
        if chosen != GATEWAY_VERSION:
            raise ModuleError(
                f"daemon chose gateway version {chosen!r}; "
                f"this module speaks {GATEWAY_VERSION!r}"
            )

        # 3. Read register.
        register = read_frame(self._reader)
        if register.get("type") != "register":
            raise ModuleError(
                f"expected register, got type={register.get('type')!r}"
            )

        # 4. Resolve every handler_reference against the local registry.
        endpoints = register.get("endpoints") or []
        resolved: List[str] = []
        errors: List[Dict[str, Any]] = []
        for ep in endpoints:
            method = str(ep.get("method") or "").upper()
            path = str(ep.get("path") or "/")
            ref = str(ep.get("handler_reference") or "")
            entry = self.registry.lookup(method, path)
            if entry is None:
                errors.append({
                    "endpoint": f"{method} {path}",
                    "reason": "handler_not_found",
                    "detail": (
                        f"no @endpoint registration matches ({method}, {path}); "
                        f"reference was {ref!r}"
                    ),
                })
                continue
            self._bindings[(method, path)] = entry.handler
            resolved.append(f"{method} {path}")

        # 5. Send register_ack.
        if errors:
            write_frame(self._writer, {
                "type": "register_ack",
                "ok": False,
                "errors": errors,
            })
            raise ModuleError(
                f"could not resolve {len(errors)} endpoint reference(s): {errors}"
            )
        write_frame(self._writer, {
            "type": "register_ack",
            "ok": True,
            "resolved": resolved,
        })

    def _serve_loop(self) -> None:
        assert self._reader is not None and self._writer is not None
        while not self._stop:
            try:
                frame = read_frame(self._reader)
            except (FrameDecodeError, FrameTooLargeError, GatewayError, OSError):
                # Socket closed or peer sent garbage. End the loop.
                return

            ftype = frame.get("type")
            if ftype == "goodbye":
                return
            if ftype == "ping":
                write_frame(self._writer, {
                    "type": "pong",
                    "nonce": str(frame.get("nonce") or ""),
                })
                continue
            if ftype != "request":
                # Phase violation. Surface and continue rather than
                # closing — production runtime would log this loudly.
                write_frame(self._writer, {
                    "type": "error",
                    "code": "phase_violation",
                    "message": f"unexpected frame type {ftype!r}",
                })
                continue

            self._handle_request(frame)

    def _handle_request(self, frame: Dict[str, Any]) -> None:
        assert self._writer is not None
        request_id = frame.get("request_id") or ""
        envelope = frame.get("envelope") or {}
        method = str(envelope.get("method") or "").upper()
        path = str(envelope.get("path") or "/")
        handler = self._bindings.get((method, path))
        if handler is None:
            write_frame(self._writer, {
                "type": "error",
                "request_id": request_id,
                "code": "handler_exception",
                "message": f"no handler bound for ({method}, {path})",
            })
            return

        ctx = EndpointContext(
            input=dict(envelope.get("input") or {}),
            agent_id=str(envelope.get("agent_id") or ""),
            principal_id=str(envelope.get("principal_id") or ""),
            agent_scopes=[],
            authority_scope=list(envelope.get("authority_scope") or []),
            session_id=envelope.get("session_id"),
            task_id=envelope.get("task_id"),
            request_id=str(envelope.get("request_id") or request_id),
            method=method,
            path=path,
            headers=dict(envelope.get("headers") or {}),
        )

        try:
            result = handler(ctx)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            write_frame(self._writer, {
                "type": "error",
                "request_id": request_id,
                "code": "handler_exception",
                "message": f"{type(exc).__name__}: {exc}",
                "details": {"exception_type": type(exc).__name__},
            })
            return

        if isinstance(result, EndpointResponse):
            response_envelope: Dict[str, Any] = {
                "body": dict(result.body or {}),
                "status": int(result.status),
            }
            if result.headers:
                response_envelope["headers"] = dict(result.headers)
            write_frame(self._writer, {
                "type": "response",
                "request_id": request_id,
                "envelope": response_envelope,
            })
            return
        if isinstance(result, EndpointError):
            write_frame(self._writer, {
                "type": "response",
                "request_id": request_id,
                "envelope": {
                    "endpoint_error": {
                        "code": result.code,
                        "message": result.message,
                        "details": result.details or None,
                    },
                },
            })
            return
        # Handler returned something it shouldn't have.
        write_frame(self._writer, {
            "type": "error",
            "request_id": request_id,
            "code": "handler_exception",
            "message": (
                f"handler returned {type(result).__name__}; expected "
                f"EndpointResponse or EndpointError"
            ),
        })


__all__ = ["GatewayClient", "ModuleError"]
