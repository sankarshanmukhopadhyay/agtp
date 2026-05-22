"""
Chain inspector HTTP server.

Tiny stdlib ``http.server`` app. Two endpoints:

  ``GET  /``       single-page UI (HTML + inline JS).
  ``POST /walk``   JSON API that takes ``{agent_uri, audit_id,
                   insecure?, insecure_skip_verify?}`` and returns
                   the walked chain.

The walking logic itself lives in :mod:`tools.chain_inspector.walker`
so the CLI and the web app share the same code path.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tools.chain_inspector.walker import walk_chain


_WEB_FORM = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>AGTP Chain Inspector</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif;
           max-width: 960px; margin: 2em auto; line-height: 1.5;
           color: #222; padding: 0 1em; }
    h1 { font-size: 1.4em; }
    label { display: block; margin-top: 0.8em; font-weight: 600; }
    input, button { font: inherit; padding: 0.4em; box-sizing: border-box; }
    input[type=text] { width: 100%; border: 1px solid #ccc; }
    button { margin-top: 1em; padding: 0.5em 1em;
             background: #2563eb; color: white; border: none;
             cursor: pointer; }
    .hint { color: #666; font-size: 0.85em; margin-top: 0.2em; }
    .step { border: 1px solid #ddd; border-radius: 4px;
            padding: 0.8em 1em; margin-top: 0.6em;
            background: #fafafa; }
    .step h3 { margin: 0 0 0.4em 0; font-size: 0.95em;
               font-family: ui-monospace, monospace; word-break: break-all; }
    .step h3 .agent { color: #2563eb; }
    .badge { display: inline-block; padding: 0.1em 0.5em;
             border-radius: 3px; font-size: 0.75em;
             margin-right: 0.4em; vertical-align: middle; }
    .ok    { background: #d4edda; color: #155724; }
    .warn  { background: #fff3cd; color: #856404; }
    .err   { background: #f8d7da; color: #721c24; }
    .info  { background: #d1ecf1; color: #0c5460; }
    .kv { font-family: ui-monospace, monospace; font-size: 0.85em;
          white-space: pre-wrap; word-break: break-all;
          margin: 0.4em 0 0 0; }
    .kv b { color: #555; }
    .arrow { text-align: center; color: #999; margin: 0.4em 0; }
  </style>
</head>
<body>
  <h1>AGTP Chain Inspector</h1>
  <p>Paste an agent's URI and an audit_id. The inspector walks
     <code>previous_audit_id</code> backwards via the agent's
     <code>INSPECT</code> endpoint and renders the chain.</p>
  <form id="walk-form">
    <label>Agent URI
      <input type="text" name="agent_uri"
             placeholder="agtp://lauren.example.com or agtp://&lt;agent_id&gt;"
             required>
    </label>
    <label>Audit ID
      <input type="text" name="audit_id"
             placeholder="64-char hex (the Audit-ID response header)"
             required pattern="[0-9a-f]{64}">
    </label>
    <label>Known agents (optional, JSON map)
      <textarea name="known_agents"
                placeholder='{"&lt;agent_id&gt;": "agtp://...", "&lt;agent_id&gt;": "agtp://..."}'
                style="font-family: ui-monospace, monospace; height: 4em;"></textarea>
      <div class="hint">For cross-agent traversal: when a record's
        <code>extra.prior_actions</code> entry doesn't carry an inline
        <code>agent_uri</code>, the walker looks up the agent here.</div>
    </label>
    <label>
      <input type="checkbox" name="insecure"> Connect over plaintext
      (test daemons only)
    </label>
    <button type="submit">Walk Chain</button>
  </form>
  <div id="results"></div>

<script>
const form = document.getElementById('walk-form');
const results = document.getElementById('results');

form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  results.innerHTML = '<p>Walking...</p>';
  const fd = new FormData(form);
  let known = {};
  const knownText = (fd.get('known_agents') || '').trim();
  if (knownText) {
    try {
      known = JSON.parse(knownText);
      if (typeof known !== 'object' || Array.isArray(known)) {
        throw new Error('known_agents must be a JSON object');
      }
    } catch (e) {
      results.innerHTML = '<p class="err">known_agents JSON: ' +
                          escapeHtml(e.message) + '</p>';
      return;
    }
  }
  const body = JSON.stringify({
    agent_uri:     fd.get('agent_uri').trim(),
    audit_id:      fd.get('audit_id').trim().toLowerCase(),
    insecure:      fd.get('insecure') === 'on',
    known_agents:  known,
  });
  let data;
  try {
    const resp = await fetch('/walk', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body,
    });
    data = await resp.json();
    if (!resp.ok) {
      results.innerHTML = '<p class="err">' + (data.error || 'walk failed') + '</p>';
      return;
    }
  } catch (e) {
    results.innerHTML = '<p class="err">' + (e.message || e) + '</p>';
    return;
  }
  renderChain(data.chain || []);
});

function badge(text, cls) {
  return '<span class="badge ' + cls + '">' + text + '</span>';
}

function renderChain(steps) {
  if (!steps.length) {
    results.innerHTML = '<p>No steps returned.</p>';
    return;
  }
  // Group steps by agent_uri (or agent_id when uri is empty) so the
  // tree-shaped graph is easier to scan.
  let html = '<h2>Chain (' + steps.length + ' step' +
             (steps.length === 1 ? '' : 's') +
             ', BFS visit order)</h2>';
  steps.forEach((step) => {
    html += '<div class="step">';
    const label = step.agent_uri || step.agent_id || '(unknown agent)';
    html += '<h3><span class="agent">' + escapeHtml(label) + '</span> &nbsp;' +
            step.audit_id + '</h3>';
    html += '<div>';
    html += badge('step ' + step.step_id, 'info');
    if (step.parent_step_ids && step.parent_step_ids.length > 0) {
      html += badge('parents: ' + step.parent_step_ids.join(', '), 'info');
    }
    if (step.fetch_error) {
      html += badge('fetch failed', 'err');
    } else if (step.signed) {
      if (step.verified === true)      html += badge('signed + verified', 'ok');
      else if (step.verified === false) html += badge('SIGNATURE INVALID', 'err');
      else                              html += badge('signed (no key supplied)', 'info');
    } else {
      html += badge('unsigned (alg: none)', 'warn');
    }
    html += '</div>';
    if (step.fetch_error) {
      html += '<div class="kv">' + escapeHtml(step.fetch_error) + '</div>';
      html += '</div>';
      return;
    }
    const p = step.payload || {};
    html += '<div>';
    if (p.server_id)    html += badge('server: ' + p.server_id, 'info');
    if (p.status)       html += badge('status: ' + p.status, 'info');
    html += '</div>';
    html += '<div class="kv">';
    if (p.issued_at)    html += '<b>issued_at:</b>   ' + p.issued_at + '\\n';
    if (p.principal_id) html += '<b>principal_id:</b> ' + p.principal_id + '\\n';
    if (p.owner_id)     html += '<b>owner_id:</b>    ' + p.owner_id + '\\n';
    if (p.session_id)   html += '<b>session_id:</b>  ' + p.session_id + '\\n';
    if (p.task_id)      html += '<b>task_id:</b>     ' + p.task_id + '\\n';
    if (p.request_id)   html += '<b>request_id:</b>  ' + p.request_id + '\\n';
    if (p.response_id)  html += '<b>response_id:</b> ' + p.response_id + '\\n';
    if (p.previous_audit_id) {
      html += '<b>previous_audit_id:</b>  ' + p.previous_audit_id + '\\n';
    }
    if (step.prior_actions && step.prior_actions.length > 0) {
      html += '<b>prior_actions:</b>\\n';
      step.prior_actions.forEach((pa) => {
        const where = pa.agent_uri || '(unresolved)';
        html += '  → agent ' + pa.agent_id.slice(0, 12) + '… @ ' +
                escapeHtml(where) +
                ', audit ' + pa.audit_id.slice(0, 12) + '…\\n';
      });
    }
    html += '</div></div>';
  });
  results.innerHTML = html;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>'"]/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  }[c]));
}
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "agtp-chain-inspector/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", ""):
            self._send(200, _WEB_FORM.encode("utf-8"), "text/html; charset=utf-8")
            return
        self._error(404, "unknown path")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/walk":
            self._error(404, "unknown path")
            return
        body = self._read_body()
        if body is None:
            return
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(400, f"invalid JSON: {exc}")
            return
        if not isinstance(data, dict):
            self._error(400, "body must be a JSON object")
            return

        agent_uri = (data.get("agent_uri") or "").strip()
        audit_id = (data.get("audit_id") or "").strip().lower()
        if not agent_uri or not audit_id:
            self._error(400, "agent_uri and audit_id are required")
            return

        known_agents = data.get("known_agents") or {}
        if not isinstance(known_agents, dict):
            self._error(400, "known_agents must be a JSON object {agent_id: agent_uri}")
            return

        try:
            steps = walk_chain(
                agent_uri=agent_uri,
                start_audit_id=audit_id,
                known_agents={
                    str(k).lower(): str(v) for k, v in known_agents.items()
                },
                insecure=bool(data.get("insecure")),
                insecure_skip_verify=bool(data.get("insecure_skip_verify")),
            )
        except Exception as exc:  # noqa: BLE001
            self._error(500, f"walk failed: {exc}")
            return

        out = json.dumps({"chain": [s.to_dict() for s in steps]}).encode("utf-8")
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
        import sys
        sys.stderr.write(
            f"[chain-inspector] {self.address_string()} - {format % args}\n",
        )


def serve(*, port: int = 4482, bind: str = "0.0.0.0") -> None:
    server = ThreadingHTTPServer((bind, port), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = ["serve"]
