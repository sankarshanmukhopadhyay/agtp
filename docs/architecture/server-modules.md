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
| 14 | Agent Certificate verification | Implemented (`server/mtls.py`, Phase B); standard X.509 + Ed25519 with Agent-ID derived from public-key hash. Full Agent-Cert custom extensions (subject-agent-id, principal-id, authority-scope-commitment, etc.) deferred to a future revision. |
| 15 | Caching | Implemented (`mod_cache`, M9) |
| 16 | Reverse proxy | Implemented (`mod_proxy`, M9) |
| 17 | AGTP-LOG receipt emission | Implemented as Ed25519-signed JSONL (`mod_audit`); COSE/SCITT wrapper deferred |
| 18 | Ed25519 signing of receipts and responses | Implemented (`server/signing.py`); Attribution-Record now signed when `[signing].enabled` |

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

1. **Gateway socket specification.** ✅ Landed as
   [`gateway-protocol-v1.md`](gateway-protocol-v1.md).
2. **Canonical schemas.** ✅ Landed under
   [`../../core/schemas/`](../../core/schemas/) with drift-detection
   tests in `tests/schemas/`.
3. **mod_python.** ✅ Landed as the
   [`mod_python/`](../../runtimes/mod_python/) package. Connects to `agtpd`
   over the gateway socket and serves `@endpoint`-decorated Python
   handlers. End-to-end coverage in `tests/test_gateway_e2e.py` and
   `tests/test_gateway_resume.py`.
4. **agtp-python.** ✅ The handler-author surface lives at
   [`../../agtp/`](../../agtp/) — `EndpointContext`,
   `EndpointResponse`, `EndpointError`, `@endpoint`,
   `HandlerRegistry`, `agtp.testing`. Versioned via
   [`../../agtp/CHANGELOG.md`](../../agtp/CHANGELOG.md).
5. **mod_php and agtp-php.** ✅ Extracted to the external
   [`agtp-php`](https://github.com/nomoticai/agtp-php) repo
   (`agtp/agtp-php` + `agtp/mod-php` on Packagist). The
   handler-author API (`Agtp\EndpointContext`,
   `Agtp\EndpointResponse`, `Agtp\EndpointError`,
   `#[AgtpEndpoint]`, `Agtp\HandlerRegistry`, `Agtp\Testing`)
   mirrors the Python library. The runtime module (`mod_php`) ports
   `mod_python.client.GatewayClient` value-for-value; end-to-end
   coverage in `tests/test_gateway_e2e_php.py` (skipped when PHP is
   not on PATH or `$AGTP_MOD_PHP_DIR`/`../agtp-php/mod_php/` is
   absent). PHP 8.1+ minimum (matches Drupal 10's floor).
6. **agtp-drupal.** ✅ Extracted to the external
   [`agtp-drupal`](https://github.com/nomoticai/agtp-drupal) repo
   (`agtp/agtp-drupal` on Packagist). Drupal 10.2+ / 11 module
   that adopts handler discovery into the Drupal service
   container: site builders tag handler services with
   `agtp.endpoint`, and `AgtpHandlerCollector` populates the
   agtp-php registry at boot. Runtime entry point is the drush
   command `agtp:serve --gateway-socket=...`. Handlers stay
   testable as plain functions via `\Agtp\Testing`.
   Pairing PHP framework integrations also ship as standalone
   repos so that each framework's user base only pulls what they
   need:
   - **WordPress**: [`agtp-wordpress`](https://github.com/nomoticai/agtp-wordpress)
     (`agtp/agtp-wordpress` on Packagist). Plugin with
     `agtp_register_handlers` filter and `agtp_init` action for
     discovery; `wp agtp serve` WP-CLI command.
   - **Symfony**: [`agtp-symfony`](https://github.com/nomoticai/agtp-symfony)
     (`agtp/agtp-symfony` on Packagist). Bundle with
     `agtp.endpoint` tagged services collected via a compiler
     pass; `bin/console agtp:serve` Console command.
   - **Laravel**: [`agtp-laravel`](https://github.com/nomoticai/agtp-laravel)
     (`agtp/agtp-laravel` on Packagist). Auto-discovered service
     provider; container `tag()` for discovery; `php artisan
     agtp:serve` Artisan command.
7. **agentic-drupal.** Independent of AGTP transport. The
   semantic-verb reference connector over Drupal's REST surface.
   Useful on its own; coordinates with agtp-drupal when both are
   installed.
8. **Subsequent modules.** ✅ mod_go, mod_node, mod_rust all landed.
   - **Go**: [`../../agtp-go/`](../../sdk/agtp-go/) +
     [`../../mod_go/`](../../runtimes/mod_go/). Sync `net.Dial` transport;
     `agtp.HandlerFunc` returns `(HandlerResult, error)` with a
     sum-type interface for response/error. Tested by
     `tests/test_gateway_e2e_go.py` against a real `go run` subprocess.
   - **Node.js / TypeScript**: [`../../agtp-node/`](../../sdk/agtp-node/) +
     [`../../mod_node/`](../../runtimes/mod_node/). Async-first
     (`HandlerFn` returns `HandlerResult | Promise<HandlerResult>`);
     `node:net` transport with `pause()`/`readable` event-driven reads.
     Tested by `tests/test_gateway_e2e_node.py` against a real Node
     subprocess.
   - **Rust**: [`../../agtp-rust/`](../../sdk/agtp-rust/) +
     [`../../mod_rust/`](../../runtimes/mod_rust/). Sync `std::net::TcpStream`
     transport; `HandlerFn = fn(&EndpointContext) -> Result<HandlerOutcome, String>`.
     Tested by `tests/test_gateway_e2e_rust.py` against a `cargo build`-ed
     binary.
9. **Operational modules.** ✅ All three landed in M9.
   - **mod_cache** ([`../../mod_cache/`](../../operational/mod_cache/)) — response
     caching for endpoints whose semantic block declares
     `impact == "informational"` (or `reversible + is_idempotent`).
     In-memory LRU + TTL backend; multi-process / Redis backend
     deferred. Integrates via the new `DispatchHook` surface in
     [`../../server/hooks.py`](../../server/hooks.py).
   - **mod_audit** ([`../../mod_audit/`](../../operational/mod_audit/)) — append-only
     JSONL log of every dispatch (timestamp, method, path, agent,
     outcome). Same field set the AGTP-LOG draft's signed receipts
     will carry; signing waits for Ed25519 in the daemon.
   - **mod_proxy** ([`../../mod_proxy/`](../../operational/mod_proxy/)) — new
     `proxy` handler-binding type that forwards AGTP requests to
     an upstream `agtpd`. Parallels the existing HTTP
     `external_service` binding. Preserves `Agent-ID`,
     `Principal-ID`, `Session-ID`, `Task-ID`, `Authority-Scope`.

   The hook surface itself is documented inline in
   [`../../server/hooks.py`](../../server/hooks.py). Future operational
   modules (mod_metrics, mod_log_structured, mod_signing) follow the
   same `install(server_state)` boot convention and consume the
   same `HookRegistry`.

## 8. Open Questions and Closed Decisions

### Closed

- **Registration direction at startup.** *Decided:* operator manifest is authoritative. agtpd loads the manifest from its config and declares the relevant `(method, path)` set to each module when the module connects. PHP-FPM workers (and any pool-of-workers runtime) are ephemeral and cannot be the durable source of truth; a freshly forked worker has to be told what it owns.

- **Gateway framing format.** *Decided:* length-prefixed JSON for gateway v1. CBOR is a candidate for v2 once the protocol stabilizes and the per-request overhead matters. Length-prefixed framing avoids the half-close hazard called out in the wire format spec.

### Open

- **Streaming responses.** The wire format is strictly synchronous today. When streaming lands at the wire layer, the gateway socket needs to support it too. Deferred to a later gateway version.

- **Concurrency model per module.** Each runtime has different conventions (PHP-FPM workers, Python asyncio, Go goroutines). The gateway protocol stays single-request-per-connection; concurrency is a per-module concern handled by spawning multiple gateway connections.

- **Connection reuse for outbound calls.** When a handler originates AGTP requests to other agents, does the daemon pool those connections, or does the language library? Leaning toward daemon-side so every language benefits.

- **Key custody.** ✅ *Closed.* Ed25519 private keys live in agtpd
  via `server.signing.SigningService` (loaded at boot from a
  PEM file path declared in `[signing]`). Runtime modules MUST NOT
  hold private keys; the `sign_request` gateway capability (Phase C)
  lets modules request signatures over opaque bytes without ever
  touching the key. `mod_audit` (in-daemon) and `mod_python`
  (via `ctx.daemon.sign()`) both consume signing through this
  surface; other runtime modules pick up the capability as they're
  updated.

## 9. What This Document Does Not Cover

- The AGTP wire format itself (defined in `draft-hood-independent-agtp-07`)
- The AGTP method catalog (defined in `core/methods.json` and the AGTP-API draft)
- Agent identity and trust tiers (defined in the core draft and AGTP-Trust)
- Discovery and the Agent Name Service (defined in AGTP-Discovery)
- AGTP-LOG and SCITT integration (defined in AGTP-LOG)
- Merchant identity (defined in AGTP-Merchant-Identity)

This document is strictly about the deployment shape of the server, the modules that plug into it, and the libraries that consume it. The protocol drafts remain authoritative for everything they cover.
