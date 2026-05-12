"""
Duration parsing and expiration computation for §7 persistent synthesis.

The server's ``[policies.synthesis]`` config block carries three
operator-facing durations:

  * ``session_duration``               — default for non-persistent
                                          syntheses.
  * ``persistent_default_duration``    — default when the agent
                                          requests ``persistent: true``
                                          without naming a duration.
  * ``persistent_max_duration``        — upper bound regardless of
                                          what the agent requests.

PROPOSE callers MAY include ``requested_duration`` in the body. The
runtime resolves the actual granted duration as:

  * Non-persistent  → ``session_duration``.
  * Persistent with no request → ``persistent_default_duration``.
  * Persistent with request ≤ max → requested duration (granted).
  * Persistent with request > max → ``persistent_max_duration``
                                     (granted, but the response notes
                                     that the request was capped).

All durations use the compact ``<int><unit>`` notation: ``s`` /
``m`` / ``h`` / ``d`` (seconds / minutes / hours / days). Compound
durations (``1d12h``) and ISO 8601 are out of scope for v00.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)

_UNIT_TO_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 60 * 60 * 24,
}


def parse_duration(text: str) -> float:
    """
    Parse a duration string like ``"24h"`` into seconds.

    Accepted units: ``s`` (seconds), ``m`` (minutes), ``h`` (hours),
    ``d`` (days). The value must be a non-negative integer.

    Raises :class:`ValueError` on malformed input — the caller maps
    that to a 400 Bad Request with ``issue="missing-required-field"``
    (the request was syntactically a PROPOSE but the duration was
    unreadable).
    """
    if not isinstance(text, str) or not text:
        raise ValueError(
            f"duration must be a non-empty string (got {text!r})"
        )
    m = _DURATION_RE.match(text)
    if m is None:
        raise ValueError(
            f"duration {text!r} does not match <int><unit> "
            f"(units: s, m, h, d)"
        )
    value = int(m.group(1))
    unit = m.group(2).lower()
    if value < 0:
        raise ValueError(f"duration {text!r} must be non-negative")
    return float(value * _UNIT_TO_SECONDS[unit])


def format_duration_seconds(seconds: float) -> str:
    """
    Render ``seconds`` as the largest exact unit possible (``86400`` →
    ``"1d"``, ``3600`` → ``"1h"``). Fractional results fall through to
    plain seconds (``"90s"``).

    Used in the 263 response's ``granted_duration`` field so the
    agent sees the same notation it sent.
    """
    if seconds < 0:
        raise ValueError(f"seconds must be non-negative (got {seconds!r})")
    sec_int = int(seconds)
    if sec_int == 0:
        return "0s"
    for unit, scale in (("d", _UNIT_TO_SECONDS["d"]),
                        ("h", _UNIT_TO_SECONDS["h"]),
                        ("m", _UNIT_TO_SECONDS["m"])):
        if sec_int % scale == 0:
            return f"{sec_int // scale}{unit}"
    return f"{sec_int}s"


def compute_expiration(
    *,
    config: Any,
    persistent: bool,
    requested_seconds: Optional[float],
    now: Optional[datetime] = None,
) -> Tuple[Optional[datetime], Optional[str]]:
    """
    Resolve the granted duration and absolute expiration timestamp
    for a freshly-accepted synthesis.

    ``config`` is a :class:`server.config.ServerConfig` instance (or
    ``None`` for the runtime-default path used by tests). When ``None``
    the function returns ``(None, None)`` — the synthesis is treated
    as session-scoped without a hard expiration. This is the v1
    behavior: ``expires_at`` is advisory and the synthesis lives as
    long as the runtime keeps it.

    Returns ``(expires_at_utc_or_None, granted_duration_str_or_None)``.
    """
    if config is None:
        return (None, None)
    synth_cfg = getattr(config, "synthesis", None)
    if synth_cfg is None:
        return (None, None)

    if not persistent:
        session_str = getattr(synth_cfg, "session_duration", "") or ""
        if not session_str:
            return (None, None)
        seconds = parse_duration(session_str)
        granted_str = format_duration_seconds(seconds)
    else:
        max_str = getattr(synth_cfg, "persistent_max_duration", "") or ""
        max_seconds = parse_duration(max_str) if max_str else None
        if requested_seconds is None:
            default_str = (
                getattr(synth_cfg, "persistent_default_duration", "") or ""
            )
            if not default_str:
                return (None, None)
            seconds = parse_duration(default_str)
        else:
            seconds = requested_seconds
        # Cap at max if provided and exceeded.
        if max_seconds is not None and seconds > max_seconds:
            seconds = max_seconds
        granted_str = format_duration_seconds(seconds)

    if now is None:
        now = datetime.now(tz=timezone.utc)
    expires_at = now + timedelta(seconds=seconds)
    return (expires_at, granted_str)


__all__ = [
    "compute_expiration",
    "format_duration_seconds",
    "parse_duration",
]
