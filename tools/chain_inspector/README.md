# tools.chain_inspector

Walk and render AGTP Attribution-Record chains.

This is the "follow the receipt" tool. Given an agent's URI and any
Audit-ID emitted by that agent (look in the `Audit-ID` response
header), the inspector calls the agent's `INSPECT` endpoint, fetches
the signed JWS, follows `previous_audit_id` backwards, and renders
the chain with per-step signature verification status.

The chain inspector itself is **not part of `agtpd`**. It runs as a
separate web app (default port 4482), parallel to the registrar
(HTTPS, typically on 443 or behind a reverse proxy). The protocol
the inspector speaks (AGTP INSPECT on port 4480) is what matters;
this tool is the reference UI.

## Running it

### As a web app

```bash
python -m tools.chain_inspector serve --port 4482
```

Opens a single-page UI at `http://localhost:4482/`:

- Paste an `agtp://` URI for the agent's daemon.
- Paste an `Audit-ID` (the 64-char hex from any response header
  that agent has produced).
- Optionally check "Connect over plaintext" when the target daemon
  is a dev fixture without TLS.
- Hit **Walk Chain**.

The page renders the chain newest-first, with each record showing
its header, payload identifiers (server_id, agent_id, principal_id,
owner_id, session_id, task_id, request_id, response_id, issued_at,
status), and a badge indicating signature status
(`signed + verified` / `SIGNATURE INVALID` / `signed (no key
supplied)` / `unsigned (alg: none)`).

### From the CLI

```bash
python -m tools.chain_inspector walk \
    agtp://lauren.example.com \
    e42bac416ea7c9249f182a4d93e12fd749bcb0e5d6254b21fc98a898a5f93617 \
    --insecure
```

Prints the chain as JSON. Pipe through `jq` for terminal rendering.

## How it works

Per step the walker:

1. Sends `INSPECT {"target": "audit", "audit_id": ...}` over AGTP
   to the agent's daemon (port 4480 by default).
2. Reads the `jws` field from the response (Phase 6 INSPECT shape).
3. Parses the JWS Compact Serialization (RFC 7515 §3.1) into
   `(header, payload, signature)`.
4. If a public key was supplied, verifies the signature.
5. Reads `previous_audit_id` from the payload.
6. Repeats with that id.

The walker stops when:

- `previous_audit_id` is empty (agent's first record), OR
- a fetch returns 404 (record rotated out / unknown id), OR
- `max_steps` is reached (default 256, defensive cap against
  attacker-supplied chains), OR
- a cycle is detected (same audit_id seen twice — possible only via
  attacker-controlled INSPECT responses, since real chains can't
  cycle by construction).

## Cross-agent chains

The walker follows `extra.prior_actions[]` references across
agents. Each entry carries `agent_id`, `audit_id`, and an optional
`agent_uri`. When `agent_uri` is present (self-describing), the
walker uses it directly. When absent, it consults the
`--known-agents` map (CLI) / `known_agents` field (POST /walk
body) — a JSON object mapping `agent_id` → `agtp://...` URI.

Walk order is breadth-first. Each step records the indices of
upstream steps that point to it (`parent_step_ids`), so renderers
can rebuild the tree shape. Diamond cases (the same audit_id
reachable via two different paths) appear once with two parents.

When a cross-agent reference can't be resolved (no inline URI,
not in the known-agents map), the step records a `fetch_error`
and the branch stops there — the rest of the walk continues.

```bash
# CLI: --known-agents takes a JSON file mapping agent_id → URI.
python -m tools.chain_inspector walk \
    agtp://lauren.example \
    e42bac... \
    --known-agents ./agents-known.json
```

Where `agents-known.json` looks like:

```json
{
  "f82d6e7f3e701beaab480d69aa620ba13e0113b722534f866cc3eb16bb3a1017":
    "agtp://lauren.example.com",
  "a99c8b1f...": "agtp://acme.tld"
}
```

## Authentication

**None in v1.** The INSPECT records are designed to be publicly
readable so chain inspectors and regulators can walk arbitrary
chains without credentials. Operators who want to lock this down
put the daemon behind a Scope-Enforcement Point (`mod_agent_cert`)
that requires a specific scope before INSPECT is admitted, or run
the daemon on an internal network.

## Configuration

None. The web UI and the CLI share the same walker logic
(`tools/chain_inspector/walker.py`). Pass `--insecure` /
`--insecure-skip-verify` flags when targeting dev fixtures.

## Storage

The inspector has no persistent state. Every walk is a fresh
read-through. The records themselves live on the agent's daemon
under `[audit].records_root` (default `~/.agtp/audit/records/`).
