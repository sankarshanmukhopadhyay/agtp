"""``python -m client`` -> client.main.main()."""

from __future__ import annotations

import sys

from client.main import main


if __name__ == "__main__":
    sys.exit(main())
