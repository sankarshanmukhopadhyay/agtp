"""
Backward-compat shim. The implementation now lives in `agtp.wire`.
Slated for removal in v0.3.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agtp.wire import *  # noqa: F401,F403
from agtp.wire import (  # noqa: F401
    AGTP_VERSION,
    AGTPRequest,
    AGTPResponse,
    WireFormatError,
    parse_request,
    parse_response,
)
