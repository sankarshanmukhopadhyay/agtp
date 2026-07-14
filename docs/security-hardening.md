# Security Hardening and Assurance Posture

This document records the controls added from the AGTP governance/security evaluation and the residual operational obligations.

## Enforced controls

### Lifecycle authorization gate

The dispatcher checks `AgentDocument.status` before synthesis, catalog validation, path validation, policy evaluation, or handler resolution. `retired` and `deprecated` identities receive `410 Gone`; `suspended` identities receive `503 Unavailable`. `DISCOVER` and `DESCRIBE` remain available so callers can retrieve status and remediation information.

**Evidence produced:** structured `agent-revoked` or `agent-suspended` errors, including `agent_id` and `agent_status`.

### Machine-readable deployment posture

Server manifests now expose:

```json
{
  "security": {"identity_binding": "none|optional|required"},
  "assurance": {
    "identity_binding": "none|optional|required",
    "attribution_records": true,
    "anonymous_discovery": false,
    "posture": "demo|mixed|hardened"
  }
}
```

`posture=hardened` requires certificate-bound identity and enabled attribution records. Clients SHOULD refuse consequential operations when peer posture does not satisfy local policy.

### Audit identity provenance

Attribution records distinguish `certificate-bound` identity from `unverified` header identity through `extra.identity_assurance`. This field is evidence about the authentication basis, not a trust decision by itself.

## Residual risks and operator obligations

- Plaintext or `mtls.mode=disabled` deployments remain Agent-ID spoofable and are suitable only for local development.
- JWS audit mode does not independently prove filesystem completeness. Use SCITT anchoring for dispute-grade evidence.
- RCNS process-local rate and idempotency state is not a shared distributed control. Multi-instance deployments need an external state backend.
- Intent Assertions still require merchant-side signature, audience, expiry, and replay enforcement before financial fulfillment.
- OAuth `noop` validation remains test-only and must not be enabled in production.

## Conformance expectations

A conforming hardened deployment can demonstrate:

1. Revoked identities receive 410 and suspended identities receive 503 before operational dispatch.
2. Manifest posture matches live mTLS, audit, and anonymous-discovery configuration.
3. Attribution records disclose whether identity was certificate-bound.
4. `DISCOVER` and `DESCRIBE` remain reachable for lifecycle introspection.
