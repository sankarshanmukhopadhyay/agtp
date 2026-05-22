"""
tools.registrar — reference AGTP registrar.

The "GoDaddy" half of the AGTP identity story. The registrar:

  * Mints fresh Agent Genesis documents on demand
  * Signs them with its long-term Ed25519 issuer key
  * Publishes its public key at a well-known URL so verifiers can
    validate issued Geneses
  * Stores every issued Genesis on disk for later retrieval

This module is a **reference implementation** — single-file stdlib
HTTP server, JSONL storage, no dependencies beyond ``cryptography``.
Production deployments swap it for a hardened service; the protocol
(the Genesis document format and the issuance HTTP API) is what
matters.

Run modes:

  * ``python -m tools.registrar serve`` — HTTP server on port 4481.
  * ``python -m tools.registrar issue --name X --owner Y`` — offline
    CLI that uses the same issuer key without standing up a server.
  * ``python -m tools.registrar pubkey`` — print the registrar's
    public key to stdout.

The registrar's data directory defaults to
``~/.agtp/registrar/`` (Linux/macOS) or
``%APPDATA%\\agtp\\registrar\\`` (Windows). Override with the
``--data-dir`` flag.
"""

from __future__ import annotations

__all__ = ["RegistrarStore"]

from tools.registrar.store import RegistrarStore
