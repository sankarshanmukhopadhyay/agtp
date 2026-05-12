# AGTP URI forms (§11)

AGTP URIs come in six forms split across three addressing strategies:
identity-anchored, server-level, and domain-anchored. Forms 2a and 4
are deployment conventions that share parsing semantics with Forms 2
and 3 respectively; they're distinguished at the spec level for
documentation clarity.

## Identity-anchored

### Form 1 — Canonical Identity

```
agtp://[Agent-ID]
```

64-character lowercase hex representation of the Agent Genesis
document's SHA-256 hash. The URI expresses identity without explicit
hosting; resolution to a server happens through agent discovery
mechanisms (out of scope for this document).

Example: `agtp://d8dc6f0df55d66c7b30100db3cffbe383c5f814e6e58a08521fb7636c3bcc230`

### Form 1a — Identity with Explicit Host

```
agtp://[Agent-ID]@[host]
```

Agent-ID with explicit hosting location for direct addressing. The
`@host` component is a transport hint — the wire request still
carries the canonical `Agent-ID` header.

Example: `agtp://d8dc6f0d...@agents.acme.com`

## Server-level

### Form 2 — Server-level Discovery

```
agtp://[host]
```

Addresses a specific AGTP server. No agent identifier; the request
targets the server itself (typically `DISCOVER` to fetch the
manifest).

Example: `agtp://agents.acme.com`

### Form 2a — Organization-level

```
agtp://[domain]
```

Addresses an organization's AGTP presence at the bare domain. Form
2a and Form 2 are structurally identical (both `agtp://[host]`);
the distinction is operational. Use Form 2a when the AGTP server
is hosted at the organization's apex domain; use Form 2 when it's
hosted at a dedicated hostname like `agents.acme.com`.

Example: `agtp://acme.com`

> **Parser note:** The URI parser doesn't structurally differentiate
> Forms 2 and 2a — both produce `ParsedURI.form == "2"`. The "Form
> 2a" label is documentation; production URIs choose the form
> based on the operator's deployment convention.

## Domain-anchored agent

### Form 3 — Domain-Anchored Agent

```
agtp://[domain]/agents/[agent-name]
```

Addresses an agent by local name at a domain. The AGTP server at
the domain routes the request based on its `hosted_agents` manifest
entries (per `agtp-api §7`). The local name is matched
case-insensitively against `AgentDocument.name`.

Resolution happens **server-side**: the client sends the URI as-is;
the server detects the `/agents/[name]` path pattern, looks up the
local name in its registry, and dispatches to the resolved agent.
No pre-flight DISCOVER is required.

Example: `agtp://acme.com/agents/lauren`

### Form 4 — Subdomain-Anchored Agent

```
agtp://agtp.[domain]/agents/[agent-name]
```

Same semantics as Form 3 with a deployment convention using a
dedicated `agtp.` subdomain. Operators choose Form 3 or Form 4
based on their infrastructure preferences:

- **Form 3** when AGTP service runs at the organization's apex
  domain or a generic host (no dedicated subdomain).
- **Form 4** when the organization separates AGTP infrastructure
  under an `agtp.[domain]` subdomain.

Example: `agtp://agtp.acme.com/agents/lauren`

## Port handling

Canonical AGTP URIs **do not include port**. The default port is
4480 (TCP/TLS 1.3) and is implicit, mirroring the convention HTTP
URIs use for 80 / 443.

The parser accepts `:port` in any form as a non-canonical
convenience for development, testing, and ephemeral hosting:

```
agtp://127.0.0.1:12345                       # dev / test only
agtp://localhost:12345/agents/lauren         # ephemeral binding
```

`format_uri` **never emits port** — the canonical form is always
port-less. Callers that need to connect to a non-default port pass
the port to the transport layer separately, not through the URI.

## Server-side resolution flow

For Form 3 / 4 URIs, the server dispatcher:

1. Detects the `/agents/{name}` path pattern in the request line.
2. Looks up the local name (case-insensitively) against
   `registry.agents` by `AgentDocument.name`.
3. **On hit**: resolves to the canonical Agent-ID, rewrites the
   request's path to `/`, and proceeds with standard dispatch. The
   resolved Agent-ID is injected as the `Agent-ID` request header
   so downstream code sees the same shape as Form 1 / 1a requests.
4. **On miss**: returns 404 with `error.code = "agent-handle-not-found"`
   and the unrecognized handle in `error.handle`.
5. **On mismatch with explicit `Agent-ID` header**: if the request
   also carries an `Agent-ID` header that disagrees with the
   path-resolved agent, returns 400
   `error.code = "agent-identity-mismatch"`.

## Form selection at the parser

The parser tries forms in this order (first match wins):

1. **Forms 1 / 1a** — leading authority is 64 hex chars
   (case-sensitive lowercase).
2. **Forms 3 / 4** — host authority + path `/agents/{name}`. Form 3
   vs Form 4 is determined by whether the host starts with
   `agtp.` (`ParsedURI.form` returns `"4"` if so, else `"3"`).
3. **Forms 2 / 2a** — bare host authority. Both produce
   `ParsedURI.form == "2"`.

Sub-paths under `/agents/{name}/...` are **rejected** with an
explicit error pointing at the §11 Future Work entry. v00 supports
only exact `/agents/{name}` paths; richer agent-resource addressing
is reserved for future revisions.

## ParsedURI fields

```python
@dataclass
class ParsedURI:
    agent_id: Optional[str] = None        # Forms 1 / 1a
    agent_handle: Optional[str] = None    # Forms 3 / 4
    host: Optional[str] = None            # Forms 1a, 2, 2a, 3, 4
    port: Optional[int] = None            # non-canonical; tolerated
    query: Optional[str] = None
```

Properties:

- `form` — returns `"1"` / `"1a"` / `"2"` / `"3"` / `"4"`.
- `is_server_level` — True for Forms 2 / 2a.
- `is_domain_anchored` — True for Forms 3 / 4.
- `has_explicit_host` — True for any form with a host component.
- `effective_port` — `port` if set, else `4480`.

## Future work (§11 open items)

- **Sub-paths under `/agents/{name}/...`** — reserved for future
  revisions. Possibly better served as an explicit API surface
  (`QUERY /agents/{name}?resource=calendar`) rather than URI
  nesting; the design decision is open.
- **Identity-discovery resolution for Form 1** — Form 1 requires a
  separate mechanism to discover the serving host. The current
  implementation defers this to deployment-specific registries.
- **Form 5 reserved** — no current proposal; numbered slot left
  open for future addressing schemes (e.g., DID-based identifiers).

## Migration notes for clients

Pre-§11 implementations supported only Forms 1, 1a, and 2 — and the
parser accepted ports in any form. The §11 changes that matter for
clients:

1. **`ParsedURI` gained `agent_handle`** — handlers / dispatchers
   that destructure ParsedURI directly should add `agent_handle`
   handling, or use the `form` property for clean branching.
2. **`format_uri` no longer emits port** — callers that round-trip
   URIs through `parse_uri` / `format_uri` and depended on port
   preservation will lose the port component. Pass `port=` to the
   transport directly.
3. **Form 3 / 4 are new** — the spec doesn't require clients to
   produce them, but a client that constructs URIs from `(domain,
   name)` tuples can now generate clean URIs without needing the
   canonical Agent-ID up front.
4. **Sub-paths under `/agents/{name}/...` are rejected** — code
   that experimentally produced these URIs will get
   `AgentIDError` at parse time.
