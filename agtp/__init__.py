"""
AGTP — Agent Transfer Protocol reference implementation.

This package collects the v1 reference implementation behind a single
import root. Modules:

    agtp.ids         Agent ID and URI parsing.
    agtp.identity    Agent Document schema and serialization.
    agtp.wire        AGTP/1.0 wire format.
    agtp.render      HTML identity card renderer.
    agtp.methods     Embedded 12-method registry (AMG foundation).
    agtp.server      Agent server (runnable via `python -m agtp.server`).
    agtp.client      CLI client (runnable via `python -m agtp`).
    agtp.registry    Registry server (runnable via `python -m agtp.registry`).
"""

from __future__ import annotations

__version__ = "0.2.0"

DEFAULT_REGISTRY_URL = "https://registry.agtp.io"
