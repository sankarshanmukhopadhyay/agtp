"""
``client.cli`` — terminal-line frontends for the AGTP client.

Three console scripts ship from this package:

  * ``agtp``         (``client.cli.main``)    invocation client
  * ``agtp-curl``    (``client.cli.curl``)    diagnostic curl-equivalent
  * ``agtp-migrate`` (``client.cli.migrate``) v1 -> v2 Agent Document tool

All three are thin layers over ``client.core_client`` for the actual
protocol work. Argparse setup, output formatting, and exit codes
live here; URI resolution, connection handling, and response parsing
live in core_client.
"""

from __future__ import annotations
