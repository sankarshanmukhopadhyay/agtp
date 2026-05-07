"""
elemen — a desktop browser for the AGTP protocol.

Launches a native window (via pywebview) hosting an HTML UI. The UI
calls into a Python API to perform AGTP fetches and persist history.

Usage:
    python app.py
    python app.py agtp://72dd28d1...    # opens with URI prefilled
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import webview

import client


HERE = Path(__file__).resolve().parent
UI_INDEX = HERE / "ui" / "index.html"

HISTORY_LIMIT = 200


def _data_dir() -> Path:
    """
    Cross-platform per-user data directory for elemen.

    Resolution order:
      ELEMEN_DATA_DIR env var (override) >
      OS-conventional dir (APPDATA / ~/Library/Application Support / XDG_CONFIG_HOME) >
      ~/.elemen as fallback.
    """
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


def _read_history() -> list[dict]:
    p = _history_file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_history(entries: list[dict]) -> None:
    try:
        _history_file().write_text(
            json.dumps(entries, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


class Api:
    """Methods exposed to the JS frontend via window.pywebview.api."""

    def __init__(self, initial_uri: str = ""):
        self._initial_uri = initial_uri

    def get_initial_uri(self) -> str:
        return self._initial_uri

    def get_default_registry(self) -> str:
        return client.DEFAULT_REGISTRY_URL

    def fetch(
        self,
        uri: str,
        fmt: str = "json",
        registry: str = "",
        insecure: bool = False,
        insecure_skip_verify: bool = False,
    ) -> dict:
        registry_url = registry.strip() or client.DEFAULT_REGISTRY_URL
        return client.fetch(
            uri.strip(),
            fmt=fmt,
            registry=registry_url,
            insecure=insecure,
            insecure_skip_verify=insecure_skip_verify,
        )

    def discover(
        self,
        uri: str,
        registry: str = "",
        insecure: bool = False,
        insecure_skip_verify: bool = False,
    ) -> dict:
        """Send DISCOVER /methods, return bucketed shape for the UI."""
        registry_url = registry.strip() or client.DEFAULT_REGISTRY_URL
        return client.discover_methods(
            uri.strip(),
            registry=registry_url,
            insecure=insecure,
            insecure_skip_verify=insecure_skip_verify,
        )

    def invoke(
        self,
        uri: str,
        method_name: str,
        body_dict: Optional[dict] = None,
        registry: str = "",
        insecure: bool = False,
        insecure_skip_verify: bool = False,
    ) -> dict:
        """Invoke a method on the URI's agent. body_dict may be None."""
        registry_url = registry.strip() or client.DEFAULT_REGISTRY_URL
        return client.invoke_method(
            uri.strip(),
            method_name,
            body_dict if isinstance(body_dict, dict) else None,
            registry=registry_url,
            insecure=insecure,
            insecure_skip_verify=insecure_skip_verify,
        )

    # ---- history ----
    def history_load(self) -> list[dict]:
        return _read_history()

    def history_add(self, entry: dict) -> list[dict]:
        if not isinstance(entry, dict):
            return _read_history()
        entry = dict(entry)
        entry["ts"] = time.time()

        existing = _read_history()
        # Dedupe on (uri, format) — most recent wins.
        key = (entry.get("uri"), entry.get("format"))
        existing = [
            e for e in existing if (e.get("uri"), e.get("format")) != key
        ]
        existing.insert(0, entry)
        del existing[HISTORY_LIMIT:]
        _write_history(existing)
        return existing

    def history_clear(self) -> list[dict]:
        _write_history([])
        return []


def main() -> int:
    initial_uri = ""
    if len(sys.argv) > 1:
        initial_uri = sys.argv[1].strip()

    api = Api(initial_uri=initial_uri)

    webview.create_window(
        title="elemen — AGTP Browser",
        url=str(UI_INDEX),
        js_api=api,
        width=1200,
        height=820,
        min_size=(820, 520),
    )
    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
