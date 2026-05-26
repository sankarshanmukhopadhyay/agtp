# tools.registrar

Reference AGTP registrar — the "GoDaddy" of Agent Genesis issuance.

`tools.registrar` is **not part of `agtpd`**. It's a separate
HTTPS service that issues signed Agent Genesis documents on
demand. Agents present their Genesis through the daemon's
`DISCOVER /genesis` endpoint and through the `subject-agent-id`
X.509 extension on their Agent Cert. The registrar's role is
parallel to a domain registrar issuing a certificate; once issued,
the Genesis is the source of truth and the registrar can go
offline without breaking active agents.

**Port note.** The registrar runs on **HTTPS, not AGTP**. Port
4480 is IANA-registered for AGTP and is reserved for the daemon's
wire protocol — running anything else there violates the
registration. The registrar is operator tooling that happens to
issue identity documents AGTP consumes; it lives on 443 (or
behind a reverse proxy doing TLS termination on a high-numbered
plaintext port).

## Running it

The realistic production posture is the registrar behind a
real TLS-terminating reverse proxy (nginx, Caddy, Apache,
Cloudflare). The daemon binds plaintext on a high-numbered port
and the proxy handles certificates:

```bash
# Behind nginx — daemon binds plaintext on a non-privileged port
python -m tools.registrar serve --port 8443 --data-dir ~/.agtp/registrar
```

For direct HTTPS without a fronting proxy:

```bash
sudo python -m tools.registrar serve \
    --port 443 \
    --tls-cert /etc/letsencrypt/live/registrar.example.com/fullchain.pem \
    --tls-key  /etc/letsencrypt/live/registrar.example.com/privkey.pem \
    --data-dir /var/lib/agtp/registrar
```

(`sudo` because 443 is privileged. Most operators front the
service with a reverse proxy and skip the sudo.)

The first run generates an Ed25519 issuer keypair at
`{data-dir}/registrar.key` / `.pub`. Every issued Genesis
references this key as its `issuer_public_key`. **Don't rotate the
key without reissuing all outstanding Geneses** — old Geneses will
stop verifying.

## HTTP endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/`               | Human-readable web form |
| GET  | `/pubkey`         | Registrar's Ed25519 public key (PEM) |
| GET  | `/issued`         | JSON list of all issued agent_ids |
| GET  | `/issued/{aid}`   | Fetch a specific Genesis (JSON) |
| POST | `/issue`          | Mint a new Genesis (JSON or form body) |

### `POST /issue` body

```json
{
  "name":              "lauren",
  "owner_id":          "nomotic.inc",
  "principal_id":      "chris@nomotic.ai",       // optional; defaults to owner_id
  "agent_public_key":  "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA...\n-----END PUBLIC KEY-----",
  "archetype":         "analyst",                // optional
  "governance_zone":   "zone:rnd",               // optional
  "trust_tier":        2,                        // 1, 2 (default), or 3
  "verification_path": "self-signed"             // see core.genesis VALID_VERIFICATION_PATHS
}
```

Returns the signed Genesis document with HTTP 201. Failures
(`400 Bad Request`, `415 Unsupported Media Type`) return
`{"error": "..."}`.

## Authentication

**None in v1.** This is a reference implementation. Operators
either:

- Run it on an internal-only network and rely on perimeter
  controls.
- Front it with a separate auth proxy (oauth2-proxy, nginx
  client-cert, etc.).

Trust Tier 1 registrars in production complete DNS-anchored or
log-anchored verification per `draft-hood-independent-agtp §6.7.2`
before issuing. That machinery is out of scope here.

## CLI mode (no server)

For dev workflows or scripted issuance:

```bash
# Use the same registrar key without standing up a server.
python -m tools.registrar issue \
    --name lauren --owner nomotic.inc \
    --public-key lauren.pub \
    --archetype analyst --tier 2
```

Other subcommands:

```bash
python -m tools.registrar pubkey       # print the issuer's PEM public key
python -m tools.registrar list         # list issued agent_ids
```

All share the `--data-dir` option (default
`~/.agtp/registrar/`).

## Storage layout

```
{data-dir}/
    registrar.key          # Ed25519 private key (PEM, 0600)
    registrar.pub          # Ed25519 public key (PEM, 0644)
    issued/
        {agent_id}.json    # one signed Genesis per agent
    issued.jsonl           # append-only audit log of issuances
```

Everything is JSON or PEM on disk; no database. Operators with
multiple registrars on one host MUST set `--data-dir` explicitly
to prevent collisions.

## End-to-end flow

The realistic operator path uses the unified
[`agtp-agent`](../agtp_agent.py) CLI rather than calling the
three lower-level tools by hand. One command produces every file
the daemon's `agents/` directory needs:

```bash
python -m tools.agtp_agent register \
    --name lauren \
    --owner nomotic.inc \
    --principal chris@nomotic.ai \
    --registrar https://registrar.example.com/issue \
    --agents-dir /var/agtp/agents \
    --methods "DISCOVER,DESCRIBE,QUERY,BOOK" \
    --scopes "bookings:write" \
    --skills "scheduling" \
    --description "Reception scheduling agent"
```

That command:

1. Mints the agent's Ed25519 keypair locally.
2. POSTs the public key to the registrar (HTTPS), receives the
   signed Genesis back.
3. Generates the AgentDocument from the Genesis (cryptographic
   identity) + flags (mutable capability declarations).
4. Drops `lauren.genesis.json`, `lauren.agent.json`, `lauren.key`,
   `lauren.pub` into `/var/agtp/agents/`.
5. Daemon picks them up on next boot (or hot-reload, if wired).

Pass `--with-cert` to also generate an X.509 Agent Cert bound to
the Genesis for mTLS deployments.

Then at request time:

- Inbound request arrives over mTLS with the cert from step (5).
  `CertVerifier` extracts `subject-agent-id = sha256(Genesis)`.
- Daemon serves Genesis at `DISCOVER /genesis` on request.
- Inspector / governance tool fetches `DISCOVER /genesis` and
  calls `verify_cert_genesis_binding(genesis=fetched,
  subject_agent_id=cert.subject_agent_id)` to check
  `sha256(Genesis) == subject-agent-id` and the Genesis
  signature against the registrar's `issuer_public_key`. Trust-
  anchor decisions (is this registrar trusted?) layer on top.

The lower-level CLIs (`tools.agtp_genesis`, `tools.registrar
issue`, `tools.generate_agent_cert`) stay as escape hatches for
operators who want one piece at a time — useful when the agent
key lives on a HSM and the keypair-minting step must happen
elsewhere.
