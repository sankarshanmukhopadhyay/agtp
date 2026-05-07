"""
Backward-compat shim. The implementation now lives in `agtp.identity`.
Slated for removal in v0.3.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agtp.identity import *  # noqa: F401,F403
from agtp.identity import (  # noqa: F401
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_YAML,
    FIELD_ORDER,
    AgentDocument,
    from_dict,
    utc_now_iso,
)
