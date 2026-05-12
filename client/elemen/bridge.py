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
    headers = dict(result.headers or {})

    # Surface the three header-based dispatch signals as first-class
    # fields so the JS classifier doesn't have to do case-insensitive
    # key probing on every render. Empty strings when absent — the
    # classifier then falls back to URI form and body shape.
    def _h(name: str) -> str:
        lower = name.lower()
        for k, v in headers.items():
            if k.lower() == lower:
                return v
        return ""

    out = {
        "ok": True,
        "kind": result.kind,
        "host": result.host,
        "port": result.port,
        "status_code": result.status_code,
        "status_text": result.status_text,
        "headers": headers,
        "body": body_text,
        "content_type": headers.get("Content-Type", ""),
        "document_type": _h("X-AGTP-Document-Type"),
        "application": _h("X-AGTP-Application"),
        "application_version": _h("X-AGTP-Application-Version"),
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


def _open_save_dialog(
    default_filename: str,
    *,
    file_types: tuple = ("All files (*.*)",),
) -> str:
    """
    Open pywebview's native save dialog. Returns "" when pywebview is
    unavailable (e.g., in unit tests) or the user cancels.

    Tests monkeypatch this function to return a fixed path.
    """
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        return ""
    try:
        windows = list(getattr(webview, "windows", []) or [])
    except Exception:
        return ""
    if not windows:
        return ""
    try:
        result = windows[0].create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=default_filename,
            file_types=file_types,
        )
    except Exception:
        return ""
    if not result:
        return ""
    if isinstance(result, (list, tuple)):
        return result[0] if result else ""
    return str(result)


def _open_open_dialog(
    *,
    file_types: tuple = ("All files (*.*)",),
) -> str:
    """Native open-file dialog; counterpart of ``_open_save_dialog``."""
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        return ""
    try:
        windows = list(getattr(webview, "windows", []) or [])
    except Exception:
        return ""
    if not windows:
        return ""
    try:
        result = windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=file_types,
        )
    except Exception:
        return ""
    if not result:
        return ""
    if isinstance(result, (list, tuple)):
        return result[0] if result else ""
    return str(result)


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

    # ---- Compose drawer ----

    def validate_compose(self, draft: Dict[str, Any]) -> Dict[str, Any]:
        """
        Catalog-based partial validation for the Compose drawer.

        Validation reduces to two cheap checks against
        :mod:`core.methods` (the verb name) and
        :mod:`core.path_grammar` (the optional path). Returns the
        shape the drawer's JS consumes (errors / warnings /
        completion).

        Per-field result keys:

          * ``name``  — the verb. Refuses unknown / legacy verbs and
            offers close-match suggestions.
          * ``path``  — the URI path. Optional; when present, must
            satisfy ``core.path_grammar.validate_path``.
        """
        from core.methods import (
            find_close_matches, is_approved_verb, is_legacy_verb,
        )
        from core.path_grammar import PathGrammarError, validate_path
        if not isinstance(draft, dict):
            draft = {}
        errors: Dict[str, str] = {}
        warnings: Dict[str, str] = {}
        completion: Dict[str, str] = {}
        suggestions_out: Dict[str, list] = {}

        name = (draft.get("name") or "").strip().upper()
        if name:
            if not is_approved_verb(name):
                if is_legacy_verb(name):
                    suggestions = find_close_matches(name)
                    errors["name"] = (
                        f"{name!r} is a legacy HTTP method; servers admit "
                        f"it only by Legacy: opt-in. Try "
                        f"{', '.join(suggestions) or '(no close matches)'}."
                    )
                    if suggestions:
                        suggestions_out["name"] = list(suggestions)
                else:
                    suggestions = find_close_matches(name)
                    if suggestions:
                        errors["name"] = (
                            f"{name!r} is not a recognized AGTP verb. "
                            f"Did you mean {', '.join(suggestions)}?"
                        )
                        suggestions_out["name"] = list(suggestions)
                    else:
                        errors["name"] = (
                            f"{name!r} is not a recognized AGTP verb."
                        )
            completion["name"] = "complete" if not errors.get("name") else "error"
        else:
            completion["name"] = "untouched"

        path = (draft.get("path") or "").strip()
        if path:
            try:
                validate_path(path)
            except PathGrammarError as exc:
                errors["path"] = exc.message
            completion["path"] = "complete" if not errors.get("path") else "error"
        else:
            completion["path"] = "untouched"

        return {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "completion": completion,
            "suggestions": suggestions_out,
        }

    def get_verb_catalog(self) -> list:
        """
        Surface the AGTP verb catalog for the drawer's autocomplete.

        Each entry is one approved verb with its categories and a
        one-line description. The drawer groups them by primary
        category for the dropdown.

        Shape (one-per-verb)::

            {"name": "RECONCILE",
             "categories": ["transaction", "analysis"],
             "description": "Reconciles ledger entries...",
             "deprecated": false,
             "successor": null,
             "removed_in": null}

        Phase-6 fields ``deprecated`` / ``successor`` / ``removed_in``
        let the drawer render a visible marker on deprecated verbs
        (italics + tooltip) so users see the migration prompt at
        author time, not first-traffic time.
        """
        from core.methods import _METHODS_DOC
        out: list = []
        for name, data in _METHODS_DOC["methods"].items():
            entry: dict = {
                "name": name,
                "categories": list(data.get("categories", [])),
                "description": str(data.get("description", "")),
            }
            if "deprecated_in" in data:
                entry["deprecated"] = True
                entry["deprecated_in"] = str(data["deprecated_in"])
                if data.get("successor"):
                    entry["successor"] = str(data["successor"]).upper()
                if data.get("removed_in"):
                    entry["removed_in"] = str(data["removed_in"])
            else:
                entry["deprecated"] = False
            out.append(entry)
        return out

    def get_catalog_version(self) -> dict:
        """Phase-6: surface the loaded catalog's version + the list
        of versions this server validates against. The drawer
        compares this to its own (cached) version on first DESCRIBE
        so users see major-version mismatches as an advisory toast.
        """
        from core.methods import (
            catalog_version, catalog_versions_supported,
        )
        return {
            "version": catalog_version(),
            "supported": list(catalog_versions_supported()),
        }

    def save_method_yaml(
        self,
        spec: Dict[str, Any],
        suggested_filename: str = "",
    ) -> str:
        """
        Open a native save-file dialog and write the spec as YAML.
        Returns the saved path or "" when the user cancels (or pyyaml
        is not installed; the caller surfaces a fallback message).
        """
        if not isinstance(spec, dict):
            return ""
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            return ""
        text = yaml.safe_dump(spec, sort_keys=False)
        suggested = (suggested_filename or "method.yaml").strip()
        path = _open_save_dialog(suggested, file_types=("YAML files (*.yaml;*.yml)", "All files (*.*)"))
        if not path:
            return ""
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError:
            return ""
        return path

    def export_library(self, library_data: Dict[str, Any]) -> str:
        """
        Export the method library as a JSON file via the native save
        dialog. Returns the saved path or "" on cancellation.
        """
        if not isinstance(library_data, dict):
            library_data = {}
        path = _open_save_dialog(
            "elemen-method-library.json",
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        if not path:
            return ""
        try:
            Path(path).write_text(
                json.dumps(library_data, indent=2), encoding="utf-8"
            )
        except OSError:
            return ""
        return path

    def import_library(self) -> Dict[str, Any]:
        """
        Open a file picker and return the parsed library JSON.
        Returns an empty dict on cancel or parse error.
        """
        path = _open_open_dialog(
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        if not path:
            return {}
        try:
            text = Path(path).read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

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
