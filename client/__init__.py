"""
``client`` — the AGTP CLI client product.

Resolves ``agtp://`` URIs and invokes methods on agents and servers.
Run via ``python -m client`` or the ``agtp`` console script after
install. Companion tools live alongside:

  * ``client.cli``     ``agtp`` console script (CLI + --propose flow)
  * ``client.elemen``  desktop browser (``elemen``)
  * ``client.core_client`` shared resolver / fetcher

Verb / path validation lives in :mod:`core.methods` and
:mod:`core.path_grammar`.

The default registry URL is the constant exported here.
"""

from __future__ import annotations


# Bake-in default for clients that need to look up bare-ID URIs.
# Overridable per-call via the ``--registry`` flag.
DEFAULT_REGISTRY_URL = "https://registry.agtp.io"


__all__ = ["DEFAULT_REGISTRY_URL"]
