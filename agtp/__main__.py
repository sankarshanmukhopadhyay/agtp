"""
Entrypoint for `python -m agtp`. Dispatches to the CLI client.
"""

from __future__ import annotations

import sys

from agtp.client import main


if __name__ == "__main__":
    sys.exit(main())
