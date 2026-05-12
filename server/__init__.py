"""
``server`` — the AGTP server product.

Hosts agents over the AGTP wire format on port 4480 and serves the
12-method registry. Run via ``python -m server`` or the
``agtp-server`` console script after install.

Subpackages:

  * ``server.synthesis`` recipe-driven composition runtime
  * ``server.examples``  opt-in custom-method modules

Verb / path validation lives in :mod:`core.methods` and
:mod:`core.path_grammar`. Per-server method policy
(``allow`` / ``disallow`` / ``legacy`` / ``redirects``) lives
under ``[policies.methods]`` in ``agtp-server.toml``; see
:class:`server.config.MethodsPolicy`.
"""

from __future__ import annotations
