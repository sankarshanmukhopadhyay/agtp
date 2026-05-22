"""
Reference AGTP registrar HTTP server.

The registrar runs on HTTP (not AGTP) — this is the "GoDaddy"
service, not an AGTP server. It uses Python's stdlib ``http.server``
so there's no third-party HTTP framework dependency. For
production-grade deployments operators fork this and put it behind
a real HTTPS frontend.

Endpoints:

  ``GET  /``              human-readable web form (HTML) for issuing
                          a Genesis manually
  ``GET  /pubkey``        registrar's Ed25519 public key (PEM,
                          text/plain). Verifiers fetch this to
                          validate signatures on issued Geneses.
  ``GET  /issued/{aid}``  fetch a previously-issued Genesis by
                          Canonical Agent-ID. Returns JSON, 404 when
                          unknown.
  ``GET  /issued``        JSON list of all issued agent_ids.
  ``POST /issue``         JSON API. Body::

                              {
                                "name":       "...",
                                "owner_id":   "...",
                                "principal_id": "...",      (optional)
                                "agent_public_key": "-----BEGIN PUBLIC KEY-----...",
                                "archetype":  "...",        (optional)
                                "governance_zone": "...",   (optional)
                                "trust_tier": 2,            (optional)
                                "verification_path": "..."  (optional)
                              }

                          Returns the signed Genesis as JSON.

There is **no authentication** on this reference implementation —
operators put the service behind an internal-network-only firewall
or front it with a separate auth proxy. Real registrars (Trust
Tier 1 issuers) authenticate via the DNS-challenge / log-anchoring
paths defined in ``draft-hood-independent-agtp §6.7.2``; that
machinery is out of scope for this reference.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from core.genesis import (
    GenesisFormatError,
    VALID_ARCHETYPES,
    VALID_TRUST_TIERS,
    VALID_VERIFICATION_PATHS,
)
from tools.registrar.store import RegistrarStore


# Inline HTML — no template engine. Keep this static; the form is
# rendered by every GET / so changes here ship without a restart.
_WEB_FORM = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>AGTP Registrar</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif;
           max-width: 640px; margin: 2em auto; line-height: 1.5;
           color: #222; }
    h1 { font-size: 1.4em; }
    label { display: block; margin-top: 0.8em; font-weight: 600; }
    input, select, textarea { width: 100%; box-sizing: border-box;
                              padding: 0.4em; font: inherit;
                              border: 1px solid #ccc; }
    textarea { font-family: ui-monospace, monospace; height: 6em; }
    button { margin-top: 1em; padding: 0.5em 1em; font: inherit;
             background: #2563eb; color: white; border: none;
             cursor: pointer; }
    .hint { color: #666; font-size: 0.85em; margin-top: 0.2em; }
    .note { background: #fff3cd; padding: 0.6em 0.8em;
            border-left: 4px solid #f0ad4e; margin: 1em 0; }
  </style>
</head>
<body>
  <h1>AGTP Registrar</h1>
  <p>Issue an Agent Genesis. The result is a signed JSON document
     binding the supplied public key to the agent's identity.</p>
  <p class="note">This reference registrar is unauthenticated. Run
     behind an internal-network firewall, never on the public
     internet.</p>
  <form method="POST" action="/issue" id="issue-form">
    <label>Agent name
      <input name="name" required>
    </label>
    <label>Owner ID
      <input name="owner_id" placeholder="example.inc" required>
    </label>
    <label>Principal ID
      <input name="principal_id" placeholder="optional; defaults to owner">
    </label>
    <label>Agent public key (PEM)
      <textarea name="agent_public_key" required
                placeholder="-----BEGIN PUBLIC KEY-----..."></textarea>
    </label>
    <label>Archetype
      <select name="archetype">
        <option value="">(none)</option>
        <option>assistant</option><option>analyst</option>
        <option>executor</option><option>orchestrator</option>
        <option>monitor</option>
      </select>
    </label>
    <label>Governance zone
      <input name="governance_zone" placeholder="zone:finance">
    </label>
    <label>Trust tier
      <select name="trust_tier">
        <option value="1">1 - Verified</option>
        <option value="2" selected>2 - Org-Asserted</option>
        <option value="3">3 - Experimental</option>
      </select>
    </label>
    <button type="submit">Issue Genesis</button>
  </form>
  <p class="hint">
    Registrar public key:
    <a href="/pubkey">/pubkey</a>.
    Issued Geneses: <a href="/issued">/issued</a>.
  </p>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    """HTTP handler. The server attaches the :class:`RegistrarStore`
    via the ``store`` class attribute before serving."""

    store: RegistrarStore  # set by serve()
    server_version = "agtp-registrar/1.0"

    # ----- GET -----

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler convention)
        if self.path == "/" or self.path == "":
            self._send(200, _WEB_FORM.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path == "/pubkey":
            self._send(
                200,
                self.store.issuer_public_key_pem.encode("utf-8"),
                "application/x-pem-file",
            )
            return
        if self.path == "/issued":
            body = json.dumps(
                {"issued": self.store.list_issued()}, indent=2,
            ).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if self.path.startswith("/issued/"):
            agent_id = self.path[len("/issued/") :]
            genesis = self.store.fetch(agent_id)
            if genesis is None:
                self._error(404, "no Genesis issued for that agent_id")
                return
            self._send(
                200,
                genesis.to_pretty_json().encode("utf-8"),
                "application/json",
            )
            return
        self._error(404, "unknown path")

    # ----- POST -----

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/issue":
            self._handle_issue()
            return
        if self.path == "/sign-manifest":
            self._handle_sign_manifest()
            return
        self._error(404, "unknown path")

    def _handle_issue(self) -> None:
        body = self._read_body()
        if body is None:
            return  # _read_body already wrote the error response

        params = self._parse_issue_body(body)
        if params is None:
            return  # _parse_issue_body wrote the error

        try:
            genesis = self.store.issue(**params)
        except (GenesisFormatError, ValueError) as exc:
            self._error(400, f"could not issue Genesis: {exc}")
            return

        self._send(
            201,
            genesis.to_pretty_json().encode("utf-8"),
            "application/json",
        )

    def _handle_sign_manifest(self) -> None:
        """POST /sign-manifest — sign an operator-supplied
        AgentDocument with the registrar's Ed25519 key.

        Body is the AgentDocument JSON (the shape the operator
        would otherwise persist to {name}.agent.json). Response is
        the same document with manifest_issuer / _public_key /
        _signature populated. Operator saves the response verbatim;
        the daemon verifies on load.
        """
        body = self._read_body()
        if body is None:
            return
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "application/json" not in content_type:
            self._error(
                415,
                f"sign-manifest requires application/json; got {content_type!r}",
            )
            return
        try:
            doc_dict = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(400, f"invalid JSON body: {exc}")
            return
        if not isinstance(doc_dict, dict):
            self._error(400, "body must be a JSON object (AgentDocument)")
            return
        try:
            signed = self.store.sign_manifest(doc_dict)
        except (ValueError, KeyError) as exc:
            self._error(400, f"could not sign manifest: {exc}")
            return
        out = json.dumps(signed, indent=2).encode("utf-8")
        self._send(200, out, "application/json")

    # ----- helpers -----

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._error(400, "invalid Content-Length")
            return None
        if length <= 0:
            self._error(400, "empty request body")
            return None
        return self.rfile.read(length)

    def _parse_issue_body(self, body: bytes) -> Dict[str, Any] | None:
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type:
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._error(400, f"invalid JSON body: {exc}")
                return None
        elif "application/x-www-form-urlencoded" in content_type:
            import urllib.parse
            data = dict(urllib.parse.parse_qsl(body.decode("utf-8")))
        else:
            self._error(
                415,
                f"unsupported Content-Type: {content_type!r}; "
                f"use application/json or application/x-www-form-urlencoded",
            )
            return None

        if not isinstance(data, dict):
            self._error(400, "body must be a JSON object or form")
            return None

        name = (data.get("name") or "").strip()
        owner = (data.get("owner_id") or data.get("owner") or "").strip()
        pubkey = (data.get("agent_public_key") or "").strip()
        if not name or not owner or not pubkey:
            self._error(
                400,
                "missing required field: name, owner_id, agent_public_key",
            )
            return None

        tier_raw = data.get("trust_tier", 2)
        try:
            tier = int(tier_raw)
        except (TypeError, ValueError):
            self._error(400, f"trust_tier must be integer; got {tier_raw!r}")
            return None
        if tier not in VALID_TRUST_TIERS:
            self._error(
                400, f"trust_tier must be one of {VALID_TRUST_TIERS}",
            )
            return None

        archetype = data.get("archetype") or None
        if archetype and archetype not in VALID_ARCHETYPES:
            self._error(
                400,
                f"archetype must be one of {sorted(VALID_ARCHETYPES)}",
            )
            return None

        verification_path = data.get("verification_path") or "self-signed"
        if verification_path not in VALID_VERIFICATION_PATHS:
            self._error(
                400,
                f"verification_path must be one of "
                f"{sorted(VALID_VERIFICATION_PATHS)}",
            )
            return None

        return {
            "name": name,
            "owner_id": owner,
            "principal_id": (data.get("principal_id") or "").strip() or owner,
            "agent_public_key_pem": pubkey,
            "archetype": archetype,
            "governance_zone": data.get("governance_zone") or None,
            "trust_tier": tier,
            "verification_path": verification_path,
        }

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode("utf-8")
        self._send(status, body, "application/json")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Quieter default than stdlib — operators redirect stderr if
        # they want full request logs.
        import sys
        sys.stderr.write(
            f"[registrar] {self.address_string()} - {format % args}\n",
        )


def serve(store: RegistrarStore, *, port: int = 4481, bind: str = "0.0.0.0") -> None:
    """Run the reference registrar HTTP server. Blocks until
    interrupted."""
    handler_cls = type(
        "BoundHandler",
        (_Handler,),
        {"store": store},
    )
    server = ThreadingHTTPServer((bind, port), handler_cls)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = ["serve"]
