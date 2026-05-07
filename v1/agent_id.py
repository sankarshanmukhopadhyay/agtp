"""
Backward-compat shim. The implementation now lives in `agtp.ids`.

This shim is retained so existing scripts (and the elemen browser, which
loads modules out of v1/ via AGTP_LIB_PATH) keep working without change.
Slated for removal in v0.3.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the agtp package importable when v1/ is added to sys.path directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agtp.ids import *  # noqa: F401,F403
from agtp.ids import (  # noqa: F401
    AGENT_ID_BYTES,
    AGENT_ID_HEX_LENGTH,
    AGENT_ID_PATTERN,
    DEFAULT_AGTP_PORT,
    URI_PATTERN,
    AgentIDError,
    ParsedURI,
    format_uri,
    generate_agent_id,
    parse_uri,
    validate_agent_id,
)
