"""
Elemen's pywebview bridge.

This module exposes an ``Api`` class to the JS side of the GUI via
``window.pywebview.api``. The class is a thin adapter from the
``client.core_client.FetchResult`` dataclass to the JS-friendly dict
shapes the UI expects.

The public method names (``fetch``, ``fetch_manifest``, ``discover``,
``invoke``, ``fetch_mcp_catalog``, ``history_*``) are part of the
contract with ``client/elemen/ui/app.js``. Keep them stable.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# Make the top-level ``core`` / ``client`` packages importable when
# elemen is launched directly (``python -m client.elemen.app``) from
# anywhere on the filesystem.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from client import core_client  # noqa: E402
from client.core_client import (  # noqa: E402
    DEFAULT_REGISTRY_URL,
    FORMAT_TO_ACCEPT,
    FetchResult,
)


# ---------------------------------------------------------------------------
# FetchResult -> JS dict serialization.
# ---------------------------------------------------------------------------


def _result_to_js(result: FetchResult, *, fmt: str = "json") -> Dict[str, Any]:
    """
    Convert a FetchResult into the dict shape the elemen JS expects.

    Success shape::
        {ok: True, kind, agent_id, host, port, status_code, status_text,
         headers, body, content_type, format, manifest?}

    Failure shape::
        {ok: False, error, stage, agent_id?, host?, port?}
    """
    if not result.ok:
        out: Dict[str, Any] = {
            "ok": False,
            "error": result.error,
            "stage": result.stage,
        }
        if result.agent_id:
            out["agent_id"] = result.agent_id
        if result.host is not None:
            out["host"] = result.host
            out["port"] = result.port
        return out

    body_text = result.body_text
    out = {
        "ok": True,
        "kind": result.kind,
        "host": result.host,
        "port": result.port,
        "status_code": result.status_code,
        "status_text": result.status_text,
        "headers": dict(result.headers or {}),
        "body": body_text,
        "content_type": (result.headers or {}).get("Content-Type", ""),
        "format": fmt,
    }
    if result.agent_id:
        out["agent_id"] = result.agent_id
    if result.kind == "manifest" and isinstance(result.parsed, dict):
        out["manifest"] = result.parsed
    return out


def _mcp_result_to_js(result: FetchResult) -> Dict[str, Any]:
    """
    MCP catalog responses use a slightly different envelope (the JS
    side reads ``tools`` / ``url`` / ``raw`` rather than agent-flavored
    fields).
    """
    if not result.ok:
        return {
            "ok": False,
            "error": result.error,
            "stage": result.stage,
            "url": result.resolved_endpoint,
        }
    tools: list = []
    raw = None
    parsed = result.parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("tools"), list):
        tools = parsed["tools"]
    elif isinstance(parsed, list):
        tools = parsed
    else:
        raw = parsed if parsed is not None else result.body_text
    return {
        "ok": True,
        "kind": "mcp_catalog",
        "url": result.resolved_endpoint,
        "status_code": result.status_code,
        "body": result.body_text,
        "tools": tools,
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# pywebview-exposed API.
# ---------------------------------------------------------------------------


HISTORY_LIMIT = 200


def _data_dir() -> Path:
    """Cross-platform per-user data directory for elemen."""
    override = os.environ.get("ELEMEN_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "win32" and os.environ.get("APPDATA"):
        path = Path(os.environ["APPDATA"]) / "elemen"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "elemen"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        path = (Path(xdg) if xdg else Path.home() / ".config") / "elemen"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _history_file() -> Path:
    return _data_dir() / "history.json"


def _read_history() -> list:
    p = _history_file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_history(entries: list) -> None:
    try:
        _history_file().write_text(
            json.dumps(entries, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


class Api:
    """Methods exposed to the JS frontend via ``window.pywebview.api``."""

    def __init__(self, initial_uri: str = "") -> None:
        self._initial_uri = initial_uri

    # ---- bootstrap ----

    def get_initial_uri(self) -> str:
        return self._initial_uri

    def get_default_registry(self) -> str:
        return DEFAULT_REGISTRY_URL

    # ---- AGTP fetches ----

    def fetch(
        self,
        uri: str,
        fmt: str = "json",
        registry: str = "",
        insecure: bool = False,
        insecure_skip_verify: bool = False,
    ) -> Dict[str, Any]:
        """
        Resolve an ``agtp://`` URI. Auto-routes Form 1/1a -> agent
        document, Form 2 -> Server Manifest.
        """
        result = core_client.fetch(
            (uri or "").strip(),
            fmt=fmt,
            registry_url=(registry or "").strip() or DEFAULT_REGISTRY_URL,
            insecure=insecure,
            insecure_skip_verify=insecure_skip_verify,
        )
        return _result_to_js(result, fmt=fmt)

    def fetch_manifest(
        self,
        host: str,
        port: int,
        insecure: bool = False,
        insecure_skip_verify: bool = False,
    ) -> Dict[str, Any]:
        """Direct manifest fetch when the host:port is already known."""
        result = core_client.fetch_manifest(
            host,
            int(port),
            insecure=insecure,
            insecure_skip_verify=insecure_skip_verify,
        )
        return _result_to_js(result, fmt="json")

    def fetch_mcp_catalog(
        self,
        catalog_url: str,
        insecure_skip_verify: bool = False,
    ) -> Dict[str, Any]:
        """Fetch an MCP tool catalog over HTTPS for the protocol tab."""
        result = core_client.fetch_mcp_catalog(
            catalog_url,
            insecure_skip_verify=insecure_skip_verify,
        )
        return _mcp_result_to_js(result)

    def discover(
        self,
        uri: str,
        registry: str = "",
        insecure: bool = False,
        insecure_skip_verify: bool = False,
    ) -> Dict[str, Any]:
        """
        Send DISCOVER target=methods, returning the bucketed shape the
        UI uses for the per-agent matching handshake.
        """
        result = core_client.invoke_method(
            (uri or "").strip(),
            "DISCOVER",
            body={"target": "methods"},
            registry_url=(registry or "").strip() or DEFAULT_REGISTRY_URL,
            insecure=insecure,
            insecure_skip_verify=insecure_skip_verify,
        )
        if not result.ok or result.status_code != 200:
            base = _result_to_js(result, fmt="json")
            base.setdefault("ok", False)
            base["error"] = result.error or (
                f"DISCOVER returned {result.status_code} {result.status_text}"
            )
            base.setdefault("stage", result.stage or "discover")
            base.setdefault("raw", result.body_text)
            return base
        payload = result.parsed if isinstance(result.parsed, dict) else {}
        return {
            "ok": True,
            "agent_id": result.agent_id,
            "host": result.host,
            "port": result.port,
            "status_code": result.status_code,
            "embedded": payload.get("embedded", []),
            "custom": payload.get("custom", []),
            "summary": payload.get("summary", {}),
            "raw": result.body_text,
        }

    def invoke(
        self,
        uri: str,
        method_name: str,
        body_dict: Optional[Dict[str, Any]] = None,
        registry: str = "",
        insecure: bool = False,
        insecure_skip_verify: bool = False,
        synthesis_id: str = "",
    ) -> Dict[str, Any]:
        """
        Invoke a method on the URI's agent. ``body_dict`` may be None.
        When ``synthesis_id`` is non-empty, the request carries a
        ``Synthesis-Id`` header so the server rewrites it onto the
        underlying method.
        """
        body = body_dict if isinstance(body_dict, dict) else None
        result = core_client.invoke_method(
            (uri or "").strip(),
            method_name,
            body=body,
            registry_url=(registry or "").strip() or DEFAULT_REGISTRY_URL,
            insecure=insecure,
            insecure_skip_verify=insecure_skip_verify,
            synthesis_id=(synthesis_id or "").strip() or None,
        )
        envelope = _result_to_js(result, fmt="json")
        if result.ok:
            envelope["method"] = method_name.upper()
        return envelope

    # ---- per-user URL/fetch history ----

    def history_load(self) -> list:
        return _read_history()

    def history_add(self, entry: Dict[str, Any]) -> list:
        if not isinstance(entry, dict):
            return _read_history()
        entry = dict(entry)
        entry["ts"] = time.time()

        existing = _read_history()
        # Dedupe on (uri, format) so the most recent wins.
        key = (entry.get("uri"), entry.get("format"))
        existing = [
            e for e in existing if (e.get("uri"), e.get("format")) != key
        ]
        existing.insert(0, entry)
        del existing[HISTORY_LIMIT:]
        _write_history(existing)
        return existing

    def history_clear(self) -> list:
        _write_history([])
        return []


__all__ = ["Api"]
