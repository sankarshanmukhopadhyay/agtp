"""
``server`` — the AGTP server product.

Hosts agents over the AGTP wire format on port 4480 and serves the
12-method registry. Run via ``python -m server`` or the
``agtp-server`` console script after install.

Subpackages:

  * ``server.amg``       AMG validator (server-side instance)
  * ``server.examples``  opt-in custom-method modules
"""

from __future__ import annotations
