# AGTP Server and Module Architecture

**Status:** Working draft
**Author:** Chris Hood
**Purpose:** Define the deployment architecture for AGTP, the responsibilities of the daemon, the role of modules, and the place of language and framework libraries.

## 1. The Model

AGTP follows the deployment shape of HTTP servers such as Apache httpd and nginx. One daemon owns the protocol. Modules extend the daemon. Libraries are what applications consume.

```
                     ┌──────────────────────────────────────┐
                     │              agtpd                   │
                     │   listens on 4480, owns AGTP         │
   AGTP wire ───────▶│   wire, identity, methods,           │
                     │   gates, sessions, discovery, certs  │
                     └────────────────┬─────────────────────┘
                                      │ gateway socket
                ┌─────────────────────┼─────────────────────┐
                │                     │                     │
         ┌──────▼──────┐      ┌───────▼──────┐     ┌────────▼────────┐
         │   mod_php   │      │   mod_python │     │    mod_go       │
         │  (PHP-FPM)  │      │  (handlers)  │     │   (Go process)  │
         └──────┬──────┘      └───────┬──────┘     └────────┬────────┘
                │                     │                     │
       ┌────────┼────────┐            │                     │
       │        │        │            │                     │
   agtp-php  agtp-php  agtp-php   agtp-python           agtp-go
   for       for       for       (user code)           (user code)
   Drupal    WordPress Symfony
```

The daemon is the protocol. Modules are language and runtime bridges. Libraries are what application code uses.

## 2. agtpd: The Daemon

agtpd owns everything inside the protocol boundary. An operator who installs agtpd gets a working AGTP server with no further dependencies.

### Core responsibilities

| # | Responsibility | Status today |
|---|----------------|--------------|
| 1 | TCP listener on port 4480 | Implemented (`server/main.py`) |
| 2 | TLS termination | Implemented |
| 3 | Wire format parsing and serialization | Implemented (`core/wire.py`) |
| 4 | Method catalog enforcement (the 425 verbs) | Implemented (`core/methods.py`) |
| 5 | Path grammar validation | Implemented (`core/path_grammar.py`) |
| 6 | JSON Schema validation per endpoint | Implemented (`server/schema_validation.py`) |
| 7 | Scope and authority gate | Implemented (`server/main.py` soft_deny_check) |
| 8 | Agent-ID resolution | Implemented |
| 9 | Agent Manifest serving | Implemented (`server/manifest.py`) |
| 10 | DISCOVER and discovery routing | Implemented |
| 11 | Configuration | Implemented (`agtp-server.toml`) |
| 12 | Logging | Basic; needs structured format |
| 13 | Session management | Specified, partial implementation |
| 14 | Agent Certificate verification | Specified, pending |
| 15 | Caching | Pending |
| 16 | Reverse proxy | Pending |
| 17 | AGTP-LOG receipt emission | Specified, pending |
| 18 | Ed25519 signing of receipts and responses | Pending |

Items 1 through 14 are protocol concerns. They live in the daemon because every AGTP server must do them identically. A module never reimplements any of these. By the time a request crosses the gateway socket into a module, it has been authenticated, validated against the catalog and path grammar, schema-checked, scope-gated, and correlated to a session.

Items 15 through 18 are deferred to subsequent versions but belong in the daemon when they land.

### What does not live in agtpd

The daemon is the protocol. It does not host applications, manage databases, or run business logic. The following are explicitly application concerns:

- Dynamic content handlers (Drupal, WordPress, custom apps)
- Databases of any kind
- Application logic
- Advanced governance and policy engines
- Monitoring and observability stacks
- LLM inference

A clean way to remember the line: if it would change between two different AGTP-hosted applications, it does not belong in agtpd.

## 3. Modules

A module is anything that plugs into agtpd over the gateway socket. Modules fall into two categories.

### Runtime modules

Runtime modules bridge agtpd to a language runtime. One per language, written once, used by every framework in that language.

- `mod_python` — bridges to a Python handler process pool. Replaces the in-process function call that exists today.
- `mod_php` — bridges to PHP-FPM workers.
- `mod_go` — bridges to a Go handler binary.
- `mod_node` — bridges to a Node.js process.
- `mod_rust` — bridges to a Rust handler binary.

Runtime modules are out-of-process by design. A runtime crash, memory leak, or interpreter bug in the handler never reaches agtpd. This matches the modern FastCGI + PHP-FPM pattern, which superseded the in-process mod_php pattern across the HTTP world over the 2010s.

### Operational modules

Operational modules extend daemon capability without crossing a language boundary. They do not bridge to a runtime; they add features to agtpd itself.

- `mod_proxy` — forward AGTP requests to another AGTP server. Enables federation, load balancing, edge termination.
- `mod_cache` — response caching for idempotent methods.
- `mod_log` — structured logging backends beyond the default.
- `mod_metrics` — Prometheus and OpenTelemetry exporters.
- `mod_audit` — AGTP-LOG receipt emission and SCITT transparency log integration.

Operational modules ship with agtpd or as official add-ons. They are configured the same way runtime modules are, in agtpd's main config file.

### What is not a module

Some categories that look like modules but are not:

- A framework integration (Drupal, Symfony, Laravel) is a library, not a module. Symfony runs on PHP, so it consumes `mod_php` indirectly through `agtp-php`.
- LLM inference is application logic, not infrastructure. An agent that calls an LLM does so inside its handler. `mod_llm` is not a module; an LLM-backed agent is a handler that happens to call out to an inference endpoint.
- MCP bridging is a library and an operational pattern, not a module. `agtp-mcp` is a library that runs as a handler and translates between AGTP and MCP.

## 4. Libraries

Libraries are what application code imports. They come in two layers.

### Language libraries (one per runtime)

- `agtp-python` — Python package for writing AGTP handlers. Mirrors today's `agtp/handlers.py` (the three dataclasses: `EndpointContext`, `EndpointResponse`, `EndpointError`).
- `agtp-php` — Composer package for PHP applications.
- `agtp-go` — Go module.
- `agtp-node` — npm package.
- `agtp-rust` — Cargo crate.

Each language library is the userspace half of a runtime module. `agtp-php` is what a PHP developer writes against; `mod_php` is what carries their handler responses back to agtpd. The two are designed together as a pair.

### Framework and application libraries (many per language)

These build on a language library and surface AGTP through a specific application or framework's idioms.

PHP ecosystem (built on `agtp-php`):

- `agtp-drupal` — Drupal module
- `agtp-wordpress` — WordPress plugin
- `agtp-symfony` — Symfony bundle
- `agtp-laravel` — Laravel package

Python ecosystem (built on `agtp-python`):

- `agtp-django` — Django integration
- `agtp-fastapi` — FastAPI integration
- `agtp-flask` — Flask integration

Cross-cutting (any language):

- `agtp-mcp` — MCP protocol bridge
- `agtp-a2a` — Google A2A bridge
- `agentic-drupal` — Agentic API reference connector for Drupal (semantic-verb wrapper over Drupal's REST surface; independent of AGTP transport)

## 5. The Gateway Socket

The gateway socket is the contract between agtpd and any module. It is the single most consequential interface in the architecture because everything below it depends on its stability.

### What the gateway carries

Each request that crosses the gateway has already been validated by agtpd. The module receives a parsed envelope, not raw wire bytes. The envelope shape comes directly from the existing `EndpointContext` dataclass and includes:

- `method` (verb from the AGTP catalog)
- `path` (validated against path grammar)
- `agent_id` (the calling agent, authenticated)
- `principal_id` (the human or entity the agent acts on behalf of)
- `authority_scope` (the scopes the calling agent has been granted)
- `session_id` and `task_id` (correlation)
- `headers` (request headers, trusted)
- `input` (request body, schema-validated)
- `request_id` (for tracing)

The module returns a response envelope: status code, headers, body, optional error.

### Transport

Unix domain sockets are the default. TCP loopback is the fallback for containerized deployments where the daemon and the handler pool run in sibling containers.

### Framing

Length-prefixed JSON for the first version (decided; see section 8). CBOR may replace it in a future revision once the protocol stabilizes. Length-prefixed framing avoids the half-close hazard documented in the wire format spec.

### Trusted headers

When mutual TLS lands, agtpd verifies the client's Agent Certificate during the TLS handshake. The verified Agent-ID, certificate fingerprint, and an `X-AGTP-Verified: yes` marker are then passed to the module as trusted headers. The module does not verify them again. The full certificate chain is available on demand but not transmitted by default.

### Version negotiation

The gateway protocol versions independently of the AGTP wire format. The module declares its supported gateway version on connection startup. agtpd accepts or rejects. This isolates gateway evolution from wire evolution.

### Registration

On startup, the operator's manifest declares the set of `(method, path)` pairs the deployment serves, plus their schemas and required scopes. agtpd loads the manifest and pushes the relevant subset to each module when it connects (decided; see section 8). Modules are not authoritative — PHP-FPM workers come and go, and a fresh worker must be told what it owns rather than asserting it. agtpd is the source of truth for routing.

## 6. Migration from Today's Codebase

The current `/server` directory contains both daemon-side and handler-side code in a single Python process. The migration is a refactor, not a rewrite.

### Daemon-side (stays in agtpd)

- `core/wire.py`
- `core/methods.py`
- `core/methods.json`
- `core/path_grammar.py`
- `core/identity.py`
- `server/main.py` (the connection handler, gates, dispatch)
- `server/schema_validation.py`
- `server/manifest.py`
- `server/audit.py`
- `server/handler_resolution.py` (replaced by gateway dispatch)

### Handler-side (moves to agtp-python)

- `agtp/handlers.py` — the three dataclasses
- `server/methods.py` REGISTRY pattern — becomes the Python library's registration helper
- All sample handler code

### The seam

The function call inside `dispatch()` that today invokes a handler in-process becomes a write to the gateway socket. A handler process on the other side reads the envelope, dispatches to the user's function, writes the response back. The first such handler process is a Python process running `agtp-python` and `mod_python`. This proves the gateway works without changing language.

## 7. Build Order

1. **Gateway socket specification.** A short working document defining transport, framing, envelope shape, trusted-header convention, version negotiation, and registration direction. This is the long-lived contract.
2. **Canonical schemas.** JSON Schema documents for `EndpointContext`, `EndpointResponse`, `EndpointError`, Agent Document, and Manifest. Lifted from the existing Python dataclasses, then frozen. Every language library loads these.
3. **mod_python.** Extract the handler-side code from `/server` into a separate Python process. Wire it to agtpd over the gateway socket. This is mostly a refactor of existing code, not new code.
4. **agtp-python.** The PyPI package developers install. Pairs with mod_python.
5. **mod_php and agtp-php.** The second runtime module and its paired library. Proves the gateway is language-neutral.
6. **agtp-drupal.** The first framework library on top of agtp-php. The Drupal community is the first beachhead.
7. **agentic-drupal.** Independent of AGTP transport. The semantic-verb reference connector over Drupal's REST surface. Useful on its own; coordinates with agtp-drupal when both are installed.
8. **Subsequent modules.** mod_go, mod_node, mod_rust, in order of demand.
9. **Operational modules.** mod_proxy, mod_cache, mod_audit, in order of operator need.

## 8. Open Questions and Closed Decisions

### Closed

- **Registration direction at startup.** *Decided:* operator manifest is authoritative. agtpd loads the manifest from its config and declares the relevant `(method, path)` set to each module when the module connects. PHP-FPM workers (and any pool-of-workers runtime) are ephemeral and cannot be the durable source of truth; a freshly forked worker has to be told what it owns.

- **Gateway framing format.** *Decided:* length-prefixed JSON for gateway v1. CBOR is a candidate for v2 once the protocol stabilizes and the per-request overhead matters. Length-prefixed framing avoids the half-close hazard called out in the wire format spec.

### Open

- **Streaming responses.** The wire format is strictly synchronous today. When streaming lands at the wire layer, the gateway socket needs to support it too. Deferred to a later gateway version.

- **Concurrency model per module.** Each runtime has different conventions (PHP-FPM workers, Python asyncio, Go goroutines). The gateway protocol stays single-request-per-connection; concurrency is a per-module concern handled by spawning multiple gateway connections.

- **Connection reuse for outbound calls.** When a handler originates AGTP requests to other agents, does the daemon pool those connections, or does the language library? Leaning toward daemon-side so every language benefits.

- **Key custody.** Ed25519 private keys for response signing live in agtpd, never in modules. Modules request signing through the gateway. Needs formalization in the gateway spec.

## 9. What This Document Does Not Cover

- The AGTP wire format itself (defined in `draft-hood-independent-agtp-07`)
- The AGTP method catalog (defined in `core/methods.json` and the AGTP-API draft)
- Agent identity and trust tiers (defined in the core draft and AGTP-Trust)
- Discovery and the Agent Name Service (defined in AGTP-Discovery)
- AGTP-LOG and SCITT integration (defined in AGTP-LOG)
- Merchant identity (defined in AGTP-Merchant-Identity)

This document is strictly about the deployment shape of the server, the modules that plug into it, and the libraries that consume it. The protocol drafts remain authoritative for everything they cover.
