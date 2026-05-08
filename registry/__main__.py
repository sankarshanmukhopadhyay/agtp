"""``python -m registry`` -> registry.main.main()."""

from __future__ import annotations

import sys

from registry.main import main


if __name__ == "__main__":
    sys.exit(main())
