"""``python -m client`` -> client.cli.main.main()."""

from __future__ import annotations

import sys

from client.cli.main import main


if __name__ == "__main__":
    sys.exit(main())
