"""
Backward-compat shim. The CLI implementation now lives in `agtp.client`.
Run the modern entrypoint with:

    python -m agtp agtp://{id}
    python -m agtp agtp://{id} QUERY --param intent="hello"

Slated for removal in v0.3.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agtp.client import *  # noqa: F401,F403
from agtp.client import (  # noqa: F401
    DEFAULT_METHOD,
    ResolutionError,
    build_body,
    build_parser,
    lookup_registry,
    main,
    resolve_target,
    run,
    send_method,
)


if __name__ == "__main__":
    sys.exit(main())
