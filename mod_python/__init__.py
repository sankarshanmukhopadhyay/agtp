"""
``mod_python`` — Python runtime module for AGTP.

Connects to ``agtpd`` over the gateway socket (see
``docs/architecture/gateway-protocol-v1.md``), receives the daemon's
endpoint registration, and serves AGTP requests by dispatching to
handlers registered through :mod:`agtp.registry`.

Run as a process::

    python -m mod_python --gateway-socket /var/run/agtpd/gateway.sock \\
        --load-module myapp.handlers

The ``--load-module`` flag imports a Python module so its
``@endpoint``-decorated handlers register themselves in
:data:`agtp.registry`. The module then resolves each
``handler_reference`` in the daemon's ``register`` frame against that
local registry.
"""

from __future__ import annotations

from mod_python.client import GatewayClient, ModuleError

__all__ = ["GatewayClient", "ModuleError"]
