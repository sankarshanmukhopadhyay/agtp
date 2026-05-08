"""``python -m server`` -> server.main.main()."""

from __future__ import annotations

import sys

from server.main import main


if __name__ == "__main__":
    sys.exit(main())
