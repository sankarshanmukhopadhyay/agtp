"""
``core`` — AGTP wire protocol primitives.

Modules here describe the protocol itself: the wire framing
(``wire``), URI / agent-ID parsing (``ids``), the Agent Document
schema (``identity``), the Server Manifest dataclasses
(``manifest``), the canonical status code helpers (``status``), the
matching handshake (``handshake``), and the HTML identity-card
renderer (``render``).

The ``core`` package is intentionally generation-free: it knows what
the wire shapes are, but does not produce any of them on its own.
Generators live in product packages (``server.manifest.generate``,
etc.) and call back into ``core`` for the dataclass forms.
"""

from __future__ import annotations
