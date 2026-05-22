"""
tools.chain_inspector — AGTP Attribution-Record chain inspector.

The "follow the receipt" tool. Given an agent URI and an audit_id,
the inspector calls that agent's daemon over AGTP INSPECT, fetches
the JWS, walks ``previous_audit_id`` backwards record by record,
and renders the resulting chain alongside per-record signature
verification status.

Run modes:

    python -m tools.chain_inspector serve [--port 4482]
        Standalone web app on http://localhost:4482/.

    python -m tools.chain_inspector walk URI AUDIT_ID
        Pure-CLI walk; prints the chain as JSON.

The web UI is a tiny single-page HTML+JS app served from
``server.py``. The walking logic lives in ``walker.py`` so the same
code drives both the web UI and the CLI.
"""

from __future__ import annotations

__all__ = ["walk_chain", "ChainStep"]

from tools.chain_inspector.walker import ChainStep, walk_chain
