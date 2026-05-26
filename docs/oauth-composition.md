# OAuth / OIDC composition with AGTP

AGTP identity (Agent-ID, Genesis, AgentDocument) and OAuth /
OIDC authorization (bearer tokens, JWKS, IdPs) are **orthogonal
axes**. They compose three ways, called Patterns 1, 2, and 3 in
the implementation and tests.

| Pattern | Wire shape | What it answers |
|---|---|---|
| **1 — AGTP only** | `Agent-ID: ...` | "Which agent is calling?" Cryptographically bound to the Genesis. No bearer token. |
| **2 — AGTP carrying OAuth** | `Agent-ID: ...` + `Authorization: Bearer <token>` | Pattern 1 + "On whose behalf is this agent acting *right now*?" The agent identifies itself; the token identifies the principal. |
| **3 — OIDC-federated Genesis-issuer trust** | (no wire change) | "Do I trust the registrar that issued this agent's Genesis?" The registrar's signing key is attested via the operator's enterprise IdP. |

The patterns stack — a single deployment can run all three at
once. Pattern 1 is the unconditional baseline; Patterns 2 and 3
are opt-in.

---

## Invariants

These hold across every pattern. Implementations MUST preserve
them; tests check them.

1. **AGTP identity and OAuth authorization are orthogonal.** The
   Agent-ID identifies the *agent* (wire layer). The OAuth bearer
   token identifies the *principal the agent is acting for at
   this moment* (application layer). Neither is derivable from
   the other.

2. **Wire-format stability.** Pattern 1 deployments emit no
   OAuth-related headers, no OAuth-related document fields. A
   server with no OAuth config behaves identically to one
   compiled before the OAuth work landed. No header or document
   field added by this work is mandatory.

3. **Token opacity.** The daemon validates the token via a
   pluggable validator and lifts one configured claim onto
   `request.acting_principal_id`. It does not interpret other
   claims and it does not store, log, or propagate the token
   itself. Handlers see `acting_principal_id`; they do not see
   the raw token.

4. **The token MUST NOT appear in the Attribution-Record.** Only
   the validated, lifted claim. Tokens are credential material;
   attribution records are append-only audit logs.

5. **Validation failures are 401.** Not 403, not 461. AGTP
   surfaces a structured `reason` from a fixed vocabulary
   (`oauth-malformed`, `oauth-invalid-signature`, `oauth-expired`,
   `oauth-not-yet-valid`, `oauth-unknown-issuer`, `oauth-invalid`)
   in the response body so clients can branch without parsing
   prose.

---

## Pattern 1 — AGTP only

The unconditional baseline. Every AGTP deployment supports this.

**Request:**

```
AGTP/1.0 QUERY /
Agent-ID: a1b2c3d4...64hex
Content-Length: 23

{"q": "what time?"}
```

**Server config:** none — `[policies.oauth]` is absent or has
`enabled = false`.

**Behavior:** the dispatcher runs the standard Authority-Scope
and method-permitted checks. No Authorization header is required;
no token is interpreted. `request.acting_principal_id` is `""`.

---

## Pattern 2 — AGTP carrying OAuth as application-layer auth

The agent identifies itself via the AGTP wire layer; the
principal the agent is acting for *right now* is identified by an
OAuth bearer token in the standard `Authorization` header.

**Request:**

```
AGTP/1.0 PURCHASE /catalog/coffee/large
Agent-ID: a1b2c3d4...64hex
Authorization: Bearer eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJjaHJpc0Bub21vdGljLmFpIiwgImV4cCI6IDk5OTk5OTk5OTl9.AbcdEfghIjkl
Content-Length: 42

{"order_id": "ord_42", "qty": 1}
```

The bearer-token shape is RFC 7235 §2.1; the token itself is
opaque to AGTP. The example shows a JWT, but any opaque string a
configured validator can handle works.

### Server config — `[policies.oauth]`

```toml
[policies.oauth]
enabled = true

# Methods that REQUIRE a valid token. Missing tokens on other
# methods are tolerated; if a token IS presented on any method,
# it MUST validate.
required_on_methods = ["PURCHASE", "WRITE"]

# Which validator to use. Built-in: "noop" (accepts any non-empty
# token), "jwt" (Ed25519/RSA signature + standard claims). Custom
# validators register via server.oauth_context.register_validator.
validator = "jwt"

# Claim to lift onto request.acting_principal_id on success.
principal_id_claim = "sub"

# Pass-through to the validator's constructor.
[policies.oauth.validator_config]
public_key = "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAk...\n-----END PUBLIC KEY-----"
allowed_algs = ["EdDSA"]
expected_issuer = "https://idp.example"
expected_audience = "agtp://lauren"
leeway_seconds = 30
```

### Per-agent overrides

The same shape lives on `AgentDocument.policies.oauth`. When
present, per-agent values override the server-wide block on every
field. This lets one server host a mix — one agent requires
tokens on PURCHASE, another never requires them at all.

```json
{
  "agent_id": "...",
  "name": "lauren",
  "...": "...",
  "policies": {
    "oauth": {
      "enabled": true,
      "validator": "noop",
      "required_on_methods": ["PURCHASE"]
    }
  }
}
```

### CLI

`agtp-agent register` and `agtp-agent install` accept OAuth flags
that preconfigure the `policies.oauth` block:

```
agtp-agent register \
  --name lauren --owner nomotic.inc \
  --agents-dir /etc/agtp/agents \
  --methods PURCHASE,QUOTE \
  --oauth-validator jwt \
  --oauth-required-on PURCHASE \
  --oauth-principal-id-claim sub \
  --oauth-config '{"public_key": "...", "allowed_algs": ["EdDSA"]}'
```

Omit all `--oauth-*` flags and you get a Pattern 1 document
identical to the pre-OAuth flow — the generated AgentDocument has
no `policies` key on the wire.

### Failure surface

| Reason | Meaning |
|---|---|
| `oauth-malformed` | Authorization header is absent on a required-method invocation, present but not Bearer-scheme, or present but empty. |
| `oauth-invalid-signature` | JWT signature does not verify against the configured public key. |
| `oauth-expired` | `exp` is in the past (after leeway). |
| `oauth-not-yet-valid` | `nbf` is in the future (after leeway). |
| `oauth-unknown-issuer` | `iss` does not match `expected_issuer`. |
| `oauth-invalid` | Catch-all for validator-side failures (e.g. audience mismatch). |

All surface as HTTP 401 with a structured body:

```json
{
  "reason": "oauth-expired",
  "explanation": "token expired (exp=2026-04-22T03:14:07Z, now=2026-05-25T...)",
  "method": "PURCHASE"
}
```

### Attribution

On successful validation, the daemon stamps
`acting_principal_id: "<lifted claim value>"` into the
Attribution-Record's `attribution_extra` block. The token itself
never appears in the record.

---

## Pattern 3 — OIDC-federated Genesis-issuer trust

The Tier 1 trust path needs to answer "is this Genesis-issuer key
trusted?" without baking a hardcoded registrar list into every
deployment. AGTP supports two answers:

- **Local trust anchor** — operator-pinned `(name, key)` pairs in
  a JSON file. Simplest path; works for fixed sets of trusted
  registrars.
- **OIDC-federated trust anchor** — the registrar publishes its
  signing keys in a JWKS that an OIDC issuer hosts. The local
  config carries only the OIDC discovery URL and the expected
  issuer; runtime resolution fetches the discovery document and
  JWKS, then checks whether the Genesis-issuer key matches any
  JWK the IdP published.

### Wire shape

**There is none.** Pattern 3 is entirely operator-side: the
registrar's key is verified against the operator's trust anchors
during AgentDocument load / Tier 1 promotion. The agent's
on-wire shape is identical to Pattern 1 (or Pattern 2, if OAuth
composition is also on).

### Trust-anchors file

The daemon's agents directory loads `trust-anchors.json` at boot:

```json
{
  "anchors": [
    {"type": "key",
     "name": "primary-registrar",
     "value": "FXJ-X2hL3...32-byte-b64url..."},

    {"type": "oidc",
     "name": "enterprise-idp",
     "discovery_url": "https://idp.example/.well-known/openid-configuration",
     "trusted_issuer": "https://idp.example"}
  ]
}
```

### Resolution algorithm

`core.issuer_resolution.resolve_issuer_trust(key, trust_anchors=...)`:

1. **Local anchors check first** (cheap; no network). Any `type:
   "key"` entry whose `value` byte-equals the target key returns
   `("local", entry)`.
2. **OIDC anchors check next**, in declaration order. For each
   `type: "oidc"` anchor:
   - Fetch the discovery document (cached, 1-hour default TTL).
   - When `trusted_issuer` is set, the discovery doc's `issuer`
     field MUST match — defends against IdP-substitution.
   - Fetch the JWKS named by `jwks_uri` (cached).
   - For each JWK with `kty: "OKP"` and `crv: "Ed25519"`, convert
     to AGTP-canonical form (b64url of raw 32 bytes, unpadded)
     and compare.
   - On match, return `("oidc", {anchor, jwk, discovery})`.
3. **No match** → `("unknown", None)`.

**Network failures and malformed responses return
`("unknown", None)` and never raise.** Falling back cleanly is
better than crashing every request that referenced an unreachable
IdP.

### CLI

```
agtp-agent register \
  --name lauren --owner nomotic.inc \
  --agents-dir /etc/agtp/agents \
  --trust-anchor /path/to/my-anchors.json
```

`--trust-anchor` copies the file into the agents dir as
`trust-anchors.json` and fails fast on missing / malformed JSON.

### Caching

`_DISCOVERY_CACHE` and `_JWKS_CACHE` are process-global with a
1-hour TTL by default. Operators with strict key-rotation
requirements should configure the TTL down (the resolver accepts
a `ttl_seconds=` kwarg) or restart the daemon at rotation time.

---

## Stacking the patterns

These compose. An enterprise deployment commonly runs all three:

```
agtp-agent register \
  --name lauren --owner nomotic.inc \
  --agents-dir /etc/agtp/agents \
  --methods PURCHASE \
  --oauth-validator jwt \
  --oauth-required-on PURCHASE \
  --oauth-config '{"public_key": "...", "allowed_algs": ["EdDSA"]}' \
  --trust-anchor /etc/agtp/idp-anchors.json
```

Per request: the AGTP wire layer identifies the agent (Pattern 1
+ Pattern 3 trust check on the agent's Genesis issuer); the
Authorization header carries the principal token (Pattern 2). The
three answers ride on three different mechanisms; they fail
independently with three different surfaces.

---

## What's out of scope (today)

- Full OIDC dynamic client registration. The resolver fetches the
  discovery doc and JWKS, nothing else.
- Refresh-token flow. AGTP's role is per-request token validation,
  not token lifecycle.
- RFC 7662 introspection. Validators currently work on JWS-style
  signatures + claims; introspection would be a different
  validator implementation (welcome contribution).
- SPIFFE / SPIRE composition. SPIFFE identities map naturally to
  the AGTP `Agent-ID` slot, but the integration is not yet built.

---

## See also

- `core/issuer_resolution.py` — Pattern 3 resolver implementation.
- `server/oauth_context.py` — Pattern 2 validator framework.
- `server/config.py` `OAuthConfig` — TOML schema for
  `[policies.oauth]`.
- `tests/test_oauth_composition.py`, `tests/test_issuer_resolution.py`,
  `tests/test_oauth_cli.py` — the executable specification.
