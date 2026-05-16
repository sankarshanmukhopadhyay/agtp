"""
Shared gateway-protocol primitives used by both the daemon side
(``server.gateway``) and the module side (``mod_python.client``).

The on-wire shape and lifecycle are documented in
``docs/architecture/gateway-protocol-v1.md``; this package is the
executable form of that document.
"""

from __future__ import annotations

from core.gateway.protocol import (
    GATEWAY_VERSION,
    MAX_FRAME_SIZE,
    FrameDecodeError,
    FrameTooLargeError,
    GatewayError,
    read_frame,
    write_frame,
)

__all__ = [
    "GATEWAY_VERSION",
    "MAX_FRAME_SIZE",
    "FrameDecodeError",
    "FrameTooLargeError",
    "GatewayError",
    "read_frame",
    "write_frame",
]
