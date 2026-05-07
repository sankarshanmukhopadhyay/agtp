"""
Backward-compat shim. The implementation now lives in `agtp.registry`.
Run the modern entrypoint with:

    python -m agtp.registry --port 8080

Slated for removal in v0.3.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agtp.registry import *  # noqa: F401,F403
from agtp.registry import (  # noqa: F401
    REGISTRY_FILE_DEFAULT,
    RegistryHandler,
    RegistryStore,
    main,
    run,
)


if __name__ == "__main__":
    sys.exit(main())
