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

```
1.  Agent operator runs:
        python -m tools.agtp_genesis create --name lauren \
            --owner nomotic.inc --out lauren.genesis.json

    or POSTs to the registrar's /issue endpoint, then saves the
    returned Genesis as lauren.genesis.json.

2.  Agent operator generates an Agent Cert bound to that Genesis:
        python -m tools.generate_agent_cert agents/lauren \
            --genesis lauren.genesis.json \
            --principal-id chris@nomotic.ai \
            --authority-scope bookings:write

3.  Agent operator drops lauren.agent.json and lauren.genesis.json
    next to each other in agtpd's agents/ directory. Daemon picks
    them up at boot.

4.  Inbound request arrives over mTLS with the cert from step 2.
    CertVerifier extracts subject-agent-id = sha256(Genesis).
    Daemon serves Genesis at DISCOVER /genesis on request.

5.  Inspector / governance tool fetches DISCOVER /genesis, verifies
        verify_cert_genesis_binding(genesis=fetched,
                                    subject_agent_id=cert.subject_agent_id)
    which checks (a) sha256(Genesis) == subject-agent-id, and
    (b) Genesis signature verifies against issuer_public_key.
    Trust-anchor decisions (is this registrar trusted?) are
    layered on top.
```
