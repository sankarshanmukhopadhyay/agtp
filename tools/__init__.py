"""
Standalone tools that ship with the AGTP reference implementation.

Each tool is a CLI registered as a console script in
``pyproject.toml`` and importable as a Python module so it can be
embedded in larger pipelines.

Currently includes:

  * ``tools.openapi_import`` — the ``agtp-import-openapi`` command
    that converts an OpenAPI 3.x spec into a directory of AGTP
    endpoint TOML files (Phase 5).
"""

from __future__ import annotations
