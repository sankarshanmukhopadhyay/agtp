"""
Daemon-side gateway server.

Binds a Unix-domain socket (or TCP loopback), accepts connections
from runtime modules (``mod_python``, ``mod_php``, ...), performs
the handshake/registration handshake documented in
``docs/architecture/gateway-protocol-v1.md``, and dispatches AGTP
requests over the gateway to a connected module.

This module is opt-in: ``server.main`` only constructs a
:class:`GatewayServer` when the operator passes ``--gateway-socket``.
When gateway mode is off, the daemon behaves exactly as before —
``registered_function`` handlers are imported and called in-process.

Concurrency model in v1: one connected module at a time. Multiple
in-flight AGTP requests serialize through a single connection lock
(matches the singleplex contract from the gateway spec). Multi-
connection pooling lands in step (c) or M4 once a real concurrency
case shows up.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Tuple

from agtp.handlers import EndpointContext, EndpointError, EndpointResponse
from core.endpoint import EndpointSpec
from core.gateway import (
    GATEWAY_VERSION,
    FrameDecodeError,
    FrameTooLargeError,
    GatewayError,
    read_frame,
    write_frame,
)


# ---------------------------------------------------------------------------
# Endpoint declaration: what the daemon pushes to a module at register time.
# ---------------------------------------------------------------------------


@dataclass
class _GatewayEndpoint:
    """One endpoint the daemon will declare to connected modules."""

    method: str
    path: str
    handler_reference: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    errors: List[str] = field(default_factory=list)
    required_scopes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Module connection state.
# ---------------------------------------------------------------------------


@dataclass
class _ModuleConnection:
    """A connected module's connection state.

    The connection lock serializes outbound requests so concurrent
    AGTP traffic does not interleave frames on the singleplex socket.
    """

    sock: socket.socket
    reader: BinaryIO
    writer: BinaryIO
    address: str = ""
    module_id: str = ""
    module_version: str = ""
    capabilities: List[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class HandshakeError(GatewayError):
    """The module-side handshake failed (version, registration, malformed)."""


# ---------------------------------------------------------------------------
# Gateway server.
# ---------------------------------------------------------------------------


def _canonical_json_hash(payload: Dict[str, Any]) -> str:
    """Produce a stable ``sha256:...`` hash of ``payload``.

    Uses ``sort_keys=True`` and the compact JSON separators. Not strict
    RFC 8785 (number serialization differs at the edges) but stable
    enough for our payload set, which contains strings, ints, bools,
    nulls, and nested objects — no floats. The gateway spec calls for
    RFC 8785; tightening lands when an actual cross-language drift
    case shows up.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class GatewayServer:
    """Owns the gateway socket, the module connection, and dispatch.

    Lifecycle:

      * Operator constructs the server with a socket path.
      * Endpoint loader calls :meth:`register_endpoint` for each
        registered-function binding while loading TOML.
      * Operator calls :meth:`start` after all endpoints are
        registered. The accept loop runs in a background thread.
      * AGTP requests call :meth:`dispatch` synchronously.
      * Operator calls :meth:`stop` on shutdown.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        server_id: str = "",
        daemon_version: str = "",
        catalog_version: str = "",
    ) -> None:
        self.socket_path = socket_path
        self.server_id = server_id
        self.daemon_version = daemon_version
        self.catalog_version = catalog_version

        self._endpoints: Dict[Tuple[str, str], _GatewayEndpoint] = {}
        self._sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._module: Optional[_ModuleConnection] = None
        self._module_lock = threading.Lock()
        self._module_ready = threading.Event()
        # Phase C capabilities. ``server.main.run()`` attaches the
        # signing service after the daemon loads its key. The AGTP
        # client is imported lazily in the outbound handler so
        # daemons without ``client.core_client`` available
        # (stripped test fixtures) gracefully refuse outbound
        # requests instead of failing at boot.
        self.signing_service: Optional[Any] = None
        self.outbound_enabled: bool = True

    # ----- Endpoint registration (pre-start) -----

    def register_endpoint(
        self,
        spec: EndpointSpec,
        *,
        input_schema: Dict[str, Any],
        output_schema: Dict[str, Any],
    ) -> None:
        """Add an endpoint that should be routed via the gateway.

        Called by the endpoint-loader during ``configure_endpoints``
        for each ``registered_function`` binding when gateway mode is
        on. The schemas are the daemon's pre-built JSON Schemas
        (from ``server.schema_validation``).
        """
        if not spec.handler or spec.handler.type != "registered_function":
            raise ValueError(
                "gateway register_endpoint only accepts registered_function bindings; "
                f"got {spec.handler.type if spec.handler else None!r}"
            )
        handler_ref = spec.handler.function or ""
        if not handler_ref:
            raise ValueError(
                "registered_function binding has no function reference"
            )
        path = spec.path or "/"
        self._endpoints[(spec.method, path)] = _GatewayEndpoint(
            method=spec.method,
            path=path,
            handler_reference=handler_ref,
            input_schema=input_schema,
            output_schema=output_schema,
            errors=list(spec.errors or []),
            required_scopes=list(spec.required_scopes or []),
        )

    def has_endpoint(self, method: str, path: str) -> bool:
        return (method.upper(), path or "/") in self._endpoints

    def endpoint_count(self) -> int:
        return len(self._endpoints)

    # ----- Lifecycle -----

    def start(self) -> None:
        """Bind the socket and start the accept loop in a background thread."""
        if self._sock is not None:
            raise RuntimeError("GatewayServer already started")
        self._stop_event.clear()
        self._sock = self._bind_socket()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="agtpd-gateway-accept",
            daemon=True,
        )
        self._accept_thread.start()

    def stop(self) -> None:
        """Close the listener and any active module connection."""
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        with self._module_lock:
            mod = self._module
            self._module = None
            self._module_ready.clear()
        if mod is not None:
            mod.close()
        # Best-effort: remove the unix socket file so a restart can rebind.
        try:
            if os.path.exists(self.socket_path) and not self.socket_path.startswith(
                ("127.0.0.1:", "0.0.0.0:")
            ):
                os.unlink(self.socket_path)
        except OSError:
            pass

    def wait_for_module(self, timeout: float = 5.0) -> bool:
        """Block until at least one module has completed registration.

        Returns True if a module is connected within ``timeout`` seconds,
        False on timeout. Useful for tests; production callers typically
        do not need to wait, since 503s during the gap are acceptable.
        """
        return self._module_ready.wait(timeout)

    # ----- Dispatch (called per AGTP request) -----

    def dispatch(self, ctx: EndpointContext) -> Any:
        """Send an AGTP request via the gateway and return the result.

        Returns an :class:`EndpointResponse` or :class:`EndpointError`.
        Returns a synthesized :class:`EndpointError` with code
        ``gateway_unavailable`` when no module is connected; the
        dispatcher translates that into a 503 wire response.
        """
        with self._module_lock:
            mod = self._module
        if mod is None or mod.closed:
            return EndpointError(
                code="gateway_unavailable",
                message=(
                    "no runtime module is currently connected to the gateway "
                    "socket; the request cannot be served"
                ),
                details={"socket": self.socket_path},
            )

        request_id = f"req-{uuid.uuid4().hex[:12]}"
        envelope = self._build_request_envelope(ctx)
        trust = self._build_trust_block(ctx)
        frame = {
            "type": "request",
            "request_id": request_id,
            "envelope": envelope,
            "trust": trust,
        }

        try:
            with mod.lock:
                write_frame(mod.writer, frame)
                response_frame = self._read_until_response(mod, request_id)
        except (FrameDecodeError, FrameTooLargeError, OSError, GatewayError) as exc:
            # Connection-level failure. Drop the module and surface a 503.
            self._drop_module(mod)
            return EndpointError(
                code="gateway_unavailable",
                message=f"gateway connection failed mid-request: {exc}",
                details={"socket": self.socket_path},
            )

        return self._decode_response_frame(response_frame, request_id)

    def _read_until_response(
        self, mod: _ModuleConnection, request_id: str,
    ) -> Dict[str, Any]:
        """Read frames in a loop, servicing module-initiated requests
        (sign_request, outbound_request) until the final `response`
        (or matching `error`) arrives. Phase C bidirectional read-loop.
        """
        while True:
            next_frame = read_frame(mod.reader)
            ftype = next_frame.get("type")
            if ftype == "response":
                return next_frame
            if ftype == "error" and next_frame.get("request_id") == request_id:
                # Module reported an error against the request itself.
                return next_frame
            if ftype == "sign_request":
                self._handle_sign_request(mod, next_frame)
                continue
            if ftype == "outbound_request":
                self._handle_outbound_request(mod, next_frame)
                continue
            # Unexpected frame type — surface as a protocol error.
            write_frame(mod.writer, {
                "type": "error",
                "code": "phase_violation",
                "message": f"unexpected frame type {ftype!r} during dispatch",
            })
            return {
                "type": "error",
                "request_id": request_id,
                "code": "handler_exception",
                "message": f"module sent unexpected frame type {ftype!r}",
            }

    def _handle_sign_request(
        self, mod: _ModuleConnection, frame: Dict[str, Any],
    ) -> None:
        """Service a module's sign_request frame. Signs the supplied
        bytes with the daemon's SigningService and replies."""
        import base64 as _base64

        op_id = str(frame.get("operation_id") or "")
        if self.signing_service is None:
            write_frame(mod.writer, {
                "type": "sign_error",
                "operation_id": op_id,
                "code": "signing_unavailable",
                "message": (
                    "daemon has no signing service configured; "
                    "set [signing].enabled in agtp-server.toml"
                ),
            })
            return
        data_b64 = str(frame.get("data_b64") or "")
        try:
            padded = data_b64 + "=" * (-len(data_b64) % 4)
            data = _base64.urlsafe_b64decode(padded)
        except (ValueError, TypeError) as exc:
            write_frame(mod.writer, {
                "type": "sign_error",
                "operation_id": op_id,
                "code": "sign_failure",
                "message": f"could not decode data_b64: {exc}",
            })
            return
        try:
            signature = self.signing_service.sign(data)
        except Exception as exc:  # noqa: BLE001
            write_frame(mod.writer, {
                "type": "sign_error",
                "operation_id": op_id,
                "code": "sign_failure",
                "message": f"{type(exc).__name__}: {exc}",
            })
            return
        write_frame(mod.writer, {
            "type": "sign_response",
            "operation_id": op_id,
            "kid": self.signing_service.key_id,
            "alg": "Ed25519",
            "signature_b64": _base64.urlsafe_b64encode(signature)
                .rstrip(b"=").decode("ascii"),
        })

    def _handle_outbound_request(
        self, mod: _ModuleConnection, frame: Dict[str, Any],
    ) -> None:
        """Service a module's outbound_request frame. Issues the
        outbound AGTP call via ``client.core_client`` and replies."""
        import json as _json

        op_id = str(frame.get("operation_id") or "")
        uri = str(frame.get("uri") or "")
        method = str(frame.get("method") or "QUERY").upper()
        path = str(frame.get("path") or "/")
        headers = dict(frame.get("headers") or {})
        body = frame.get("body")
        if not uri:
            write_frame(mod.writer, {
                "type": "outbound_error",
                "operation_id": op_id,
                "code": "outbound_failure",
                "message": "outbound_request requires a non-empty uri",
            })
            return
        try:
            from client.core_client import fetch as _fetch
        except ImportError as exc:
            write_frame(mod.writer, {
                "type": "outbound_error",
                "operation_id": op_id,
                "code": "outbound_failure",
                "message": f"AGTP client unavailable: {exc}",
            })
            return
        body_bytes = (
            _json.dumps(body).encode("utf-8")
            if isinstance(body, (dict, list))
            else (body.encode("utf-8") if isinstance(body, str) else b"")
        )
        try:
            result = _fetch(
                uri, method=method, path=path,
                headers=headers, body=body_bytes,
            )
        except Exception as exc:  # noqa: BLE001
            write_frame(mod.writer, {
                "type": "outbound_error",
                "operation_id": op_id,
                "code": "upstream_unreachable",
                "message": f"upstream call failed: {exc}",
            })
            return
        status = getattr(result, "status_code", None) or getattr(result, "status", 0) or 0
        upstream_body_bytes = (
            getattr(result, "body_bytes", None)
            or getattr(result, "body", b"")
            or b""
        )
        if isinstance(upstream_body_bytes, str):
            upstream_body_bytes = upstream_body_bytes.encode("utf-8")
        parsed_body: Any = None
        if upstream_body_bytes:
            try:
                parsed_body = _json.loads(upstream_body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, _json.JSONDecodeError):
                write_frame(mod.writer, {
                    "type": "outbound_error",
                    "operation_id": op_id,
                    "code": "upstream_malformed",
                    "message": "upstream returned non-JSON body",
                    "details": {"status": status, "uri": uri},
                })
                return
        upstream_headers = dict(getattr(result, "headers", {}) or {})
        write_frame(mod.writer, {
            "type": "outbound_response",
            "operation_id": op_id,
            "status": int(status) if status else 0,
            "headers": upstream_headers,
            "body": parsed_body,
        })

    # ----- Internals -----

    def _bind_socket(self) -> socket.socket:
        if self.socket_path.startswith(("127.0.0.1:", "[::1]:", "localhost:")):
            host, _, port_str = self.socket_path.rpartition(":")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, int(port_str)))
        else:
            # Unix domain socket. Remove a stale path if present.
            try:
                if os.path.exists(self.socket_path):
                    os.unlink(self.socket_path)
            except OSError:
                pass
            parent = os.path.dirname(self.socket_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(self.socket_path)
            try:
                # 0660: owner + group rw, others none. Operators put the
                # module's uid in the daemon's group rather than running
                # the module as the daemon's uid.
                os.chmod(self.socket_path, 0o660)
            except OSError:
                pass
        sock.listen(8)
        return sock

    def _accept_loop(self) -> None:
        assert self._sock is not None
        sock = self._sock
        while not self._stop_event.is_set():
            try:
                client, address = sock.accept()
            except OSError:
                # Listener closed; exit cleanly.
                return
            # Per-connection thread keeps the accept loop responsive even
            # though we only honor one connected module at a time.
            threading.Thread(
                target=self._handle_connection,
                args=(client, address),
                name="agtpd-gateway-conn",
                daemon=True,
            ).start()

    def _handle_connection(self, client: socket.socket, address: Any) -> None:
        # Treat the socket peer as a string for logging (Unix sockets give "").
        addr_str = repr(address) if address else self.socket_path
        try:
            mod = _ModuleConnection(
                sock=client,
                reader=client.makefile("rb"),
                writer=client.makefile("wb"),
                address=addr_str,
            )
        except OSError as exc:
            print(
                f"[gateway] failed to open streams on new connection: {exc}",
                file=sys.stderr,
            )
            try:
                client.close()
            except OSError:
                pass
            return

        try:
            self._do_handshake(mod)
        except HandshakeError as exc:
            print(f"[gateway] handshake refused from {addr_str}: {exc}", file=sys.stderr)
            mod.close()
            return
        except (FrameDecodeError, FrameTooLargeError, GatewayError, OSError) as exc:
            print(
                f"[gateway] handshake aborted from {addr_str}: {exc}",
                file=sys.stderr,
            )
            mod.close()
            return

        # Replace any prior module; new connections win in v1.
        with self._module_lock:
            prior = self._module
            self._module = mod
            self._module_ready.set()
        if prior is not None and prior is not mod:
            prior.close()

        print(
            f"[gateway] module connected: {mod.module_id} "
            f"v{mod.module_version} ({mod.endpoint_count if hasattr(mod, 'endpoint_count') else len(self._endpoints)} endpoints)",
            file=sys.stderr,
        )

    def _do_handshake(self, mod: _ModuleConnection) -> None:
        """Run hello / welcome / register{,_resume} / register_ack on a new connection."""
        # 1. Read hello.
        hello = read_frame(mod.reader)
        if hello.get("type") != "hello":
            raise HandshakeError(
                f"expected hello frame, got type={hello.get('type')!r}"
            )
        versions = hello.get("gateway_versions") or []
        if GATEWAY_VERSION not in versions:
            self._send_error(
                mod,
                code="gateway_version_unsupported",
                message=(
                    f"module supports {versions!r}; daemon speaks "
                    f"{GATEWAY_VERSION}"
                ),
            )
            raise HandshakeError(
                f"version negotiation failed (module versions {versions!r})"
            )
        module_block = hello.get("module") or {}
        mod.module_id = str(module_block.get("id") or "")
        mod.module_version = str(module_block.get("version") or "")
        mod.capabilities = list(hello.get("capabilities") or [])
        cached_hash = str(hello.get("cached_manifest_hash") or "")

        # 2. Send welcome.
        # Build the daemon's capability list. Always-on:
        # registered_function. sign_request lights up when the daemon
        # has a signing service loaded; outbound_call lights up unless
        # the operator explicitly disabled it. Modules that don't
        # claim a capability in `hello` won't have its frames serviced.
        daemon_caps = ["registered_function"]
        if self.signing_service is not None:
            daemon_caps.append("sign_request")
        if self.outbound_enabled:
            daemon_caps.append("outbound_call")
        welcome: Dict[str, Any] = {
            "type": "welcome",
            "gateway_version": GATEWAY_VERSION,
            "daemon": {
                "version": self.daemon_version or "agtpd",
                "server_id": self.server_id or "",
            },
            "capabilities": daemon_caps,
        }
        if self.catalog_version:
            welcome["daemon"]["catalog_version"] = self.catalog_version
        write_frame(mod.writer, welcome)

        # 3. Send register or register_resume. Resume when the module's
        # cached_manifest_hash matches our current hash exactly; otherwise
        # send the full register with inline schemas. See gateway spec §6.4.
        endpoints_block, schemas_block = self._build_register_blocks()
        manifest_hash = _canonical_json_hash(
            {"endpoints": endpoints_block, "schemas": schemas_block}
        )
        if cached_hash and cached_hash == manifest_hash:
            write_frame(mod.writer, {
                "type": "register_resume",
                "manifest_hash": manifest_hash,
            })
        else:
            write_frame(mod.writer, {
                "type": "register",
                "manifest_hash": manifest_hash,
                "endpoints": endpoints_block,
                "schemas": schemas_block,
            })

        # 4. Read register_ack.
        ack = read_frame(mod.reader)
        if ack.get("type") != "register_ack":
            raise HandshakeError(
                f"expected register_ack, got type={ack.get('type')!r}"
            )
        if not ack.get("ok"):
            raise HandshakeError(
                f"module refused registration: {ack.get('errors') or ack!r}"
            )

    def _build_register_blocks(
        self,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """Build the endpoints array + inline schemas block for `register`."""
        endpoints: List[Dict[str, Any]] = []
        schemas: Dict[str, Dict[str, Any]] = {}
        for ep in self._endpoints.values():
            in_ref = f"{ep.method}_{ep.path}.input".replace("/", "_")
            out_ref = f"{ep.method}_{ep.path}.output".replace("/", "_")
            schemas[in_ref] = ep.input_schema
            schemas[out_ref] = ep.output_schema
            endpoints.append({
                "method": ep.method,
                "path": ep.path,
                "handler_reference": ep.handler_reference,
                "input_schema_ref": f"#/schemas/{in_ref}",
                "output_schema_ref": f"#/schemas/{out_ref}",
                "errors": list(ep.errors),
                "required_scopes": list(ep.required_scopes),
            })
        return endpoints, schemas

    def _send_error(
        self,
        mod: _ModuleConnection,
        *,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        frame: Dict[str, Any] = {
            "type": "error",
            "code": code,
            "message": message,
        }
        if details is not None:
            frame["details"] = details
        try:
            write_frame(mod.writer, frame)
        except (OSError, GatewayError):
            pass

    def _drop_module(self, mod: _ModuleConnection) -> None:
        with self._module_lock:
            if self._module is mod:
                self._module = None
                self._module_ready.clear()
        mod.close()

    def _build_request_envelope(self, ctx: EndpointContext) -> Dict[str, Any]:
        return {
            "method": ctx.method,
            "path": ctx.path,
            "agent_id": ctx.agent_id,
            "principal_id": ctx.principal_id,
            "authority_scope": list(ctx.authority_scope),
            "session_id": ctx.session_id,
            "task_id": ctx.task_id,
            "request_id": ctx.request_id,
            "headers": dict(ctx.headers),
            "input": dict(ctx.input),
        }

    def _build_trust_block(self, ctx: EndpointContext) -> Dict[str, Any]:
        # Phase B: when the daemon verified an Agent Certificate
        # during the TLS handshake, EndpointContext.agent_verified is
        # true and agent_cert_fingerprint is set. The trust block
        # reflects that — ``method`` becomes ``agent_cert_mtls`` and
        # the fingerprint rides through to the module so it can be
        # logged / audited without re-verifying.
        if ctx.agent_verified and ctx.agent_cert_fingerprint:
            return {
                "verified": True,
                "agent_id": ctx.agent_id,
                "agent_cert_fingerprint": ctx.agent_cert_fingerprint,
                "method": "agent_cert_mtls",
            }
        return {
            "verified": bool(ctx.agent_id),
            "agent_id": ctx.agent_id,
            "agent_cert_fingerprint": None,
            "method": "agent_id_header",
        }

    def _decode_response_frame(
        self, frame: Dict[str, Any], request_id: str,
    ) -> Any:
        ftype = frame.get("type")
        if ftype == "error":
            return EndpointError(
                code=str(frame.get("code") or "handler_exception"),
                message=str(frame.get("message") or "module reported an error"),
                details=frame.get("details") or None,
            )
        if ftype != "response":
            return EndpointError(
                code="handler_exception",
                message=f"unexpected frame type from module: {ftype!r}",
            )
        if frame.get("request_id") != request_id:
            return EndpointError(
                code="handler_exception",
                message=(
                    f"module response had mismatched request_id "
                    f"({frame.get('request_id')!r} != {request_id!r})"
                ),
            )
        envelope = frame.get("envelope") or {}
        if "endpoint_error" in envelope:
            err = envelope["endpoint_error"] or {}
            return EndpointError(
                code=str(err.get("code") or "handler_exception"),
                message=str(err.get("message") or ""),
                details=err.get("details") or None,
            )
        return EndpointResponse(
            body=dict(envelope.get("body") or {}),
            status=int(envelope.get("status") or 200),
            headers=envelope.get("headers") or None,
        )


__all__ = [
    "GatewayServer",
    "HandshakeError",
]
