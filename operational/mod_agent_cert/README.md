# mod_agent_cert

Operational module: Scope-Enforcement-Point gating from AGTP Agent
Certificate extensions (`draft-hood-agtp-agent-cert-00`).

## What it does

`mod_agent_cert` registers a `before_dispatch` hook against the
daemon's `HookRegistry`. On every request, before the handler runs,
the hook reads the parsed Agent-Cert extensions on
`EndpointContext.agent_cert_extensions` and enforces two checks:

1. **Authority-Scope must be a subset of `authority-scope-commitment`.**
   Every token in the inbound `Authority-Scope` request header MUST be
   a member of the scope token set the cert commits to. Tokens outside
   the commitment return **455 Scope Violation** without the body
   parser ever running.

2. **`AGTP-Zone-ID` must match the cert's `governance-zone`** (when
   both are present). Mismatches return **457 Zone Violation**.

When the connection has no verified Agent Cert (mTLS disabled or
transport-only cert without extensions), the hook is a no-op. The
daemon's existing soft-deny gate continues to enforce scope against
the agent's declared scopes; this module is purely additive.

## When to load it

Load `mod_agent_cert` when:

- The daemon is running with mTLS enabled (`[mtls].mode = "optional"`
  or `"required"`).
- Agents in the deployment carry full Agent Certs (subject-agent-id,
  authority-scope-commitment, optional governance-zone, etc.).
- You want **zero-trust enforcement at the wire layer** — refusing
  out-of-scope requests before any application-layer parsing.

For deployments using only transport-only certs (Phase 2 shape), the
module is a no-op and there's no harm in loading it, but no benefit
either.

## Configuration

None in v1. The hook activates whenever a verified Agent Cert
carrying the relevant extensions is present.

Operators who want to disable enforcement for a specific period
unload the module by omitting it from `--load-module`.

## Cost

O(scopes) per request, where `scopes` is the number of tokens in the
inbound `Authority-Scope` header — typically 1-3. The cert's scope
commitment is parsed once at TLS handshake time; per-request work is
a set membership lookup.

## Loading

```bash
agtpd --load-module mod_cache --load-module mod_agent_cert
```

Order doesn't matter — hooks run in registration order, and
`mod_agent_cert` is short-circuit-only, so a cache hit before
`mod_agent_cert` runs is rare and safe (the cert was already
verified at TLS handshake; cached responses for a given Agent-ID are
the same agent's authorized output).

## Status codes

| Code | When |
|---|---|
| 455 Scope Violation | One or more Authority-Scope tokens not in cert commitment. Body carries `error.outside_commitment`, `error.claimed`, `error.committed`, `error.agent_id`. |
| 457 Zone Violation | `AGTP-Zone-ID` header disagrees with cert's `governance-zone`. Body carries `error.cert_zone`, `error.request_zone`. |
