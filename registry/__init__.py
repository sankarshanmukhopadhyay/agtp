"""
``registry`` — the AGTP registry product.

A lightweight HTTP service that resolves bare ``agtp://{agent-id}``
URIs to a (host, port) pair so clients can connect without an
embedded host. Run via ``python -m registry`` or the
``agtp-registry`` console script after install.
"""

from __future__ import annotations
