"""
Backward-compat shim. The server implementation now lives in `agtp.server`.
Run the modern entrypoint with:

    python -m agtp.server --insecure --port 4480 --agents-dir agents/

Slated for removal in v0.3.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agtp.server import *  # noqa: F401,F403
from agtp.server import (  # noqa: F401
    DEFAULT_PORT,
    AgentRegistry,
    handle_connection,
    main,
    run,
)


if __name__ == "__main__":
    sys.exit(main())
