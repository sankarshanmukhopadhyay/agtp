# mod_audit

Append-only audit log of every endpoint dispatch.

`mod_audit` is an operational module — loaded into `agtpd`'s
process via `--load-module`. It observes every response and writes
one JSONL entry to a configured log file.

## What this is (and is not) yet

Receipts can now be **Ed25519-signed**. The daemon's
`[signing].enabled` block (see [`../server/agtp-server.toml`](../../server/agtp-server.toml))
loads a private key; setting `AGTP_AUDIT_SIGN_RECEIPTS=1` tells
`mod_audit` to sign each entry with that key. Signed entries carry
`kid`, `alg`, `signature`, and `payload` fields; unsigned entries
flatten the payload to the top level (the v1 shape, retained for
operators who haven't enabled signing yet).

The full **AGTP-LOG** specification ([draft-hood-agtp-log-00](../../ietf/draft-hood-agtp-log-00.md))
calls for COSE_Sign1 receipts written to a SCITT-style transparency
log. The current Ed25519-over-canonical-JSON shape is the bridge —
same key material, same signing service — until the COSE/SCITT
wrapper lands. Existing signed entries replay-verify trivially
when COSE arrives; the wrapper change is at the encoding layer,
not the key-management layer.

## Install

```bash
python -m server 4480 \
    --agents-dir agents/ \
    --endpoints-dir endpoints/ \
    --load-module mod_audit
```

Environment variables:

| Variable                       | Default              | Meaning                                                  |
|--------------------------------|----------------------|----------------------------------------------------------|
| `AGTP_AUDIT_ENABLED`           | `1`                  | Set to `0` to keep the module loaded but inactive        |
| `AGTP_AUDIT_PATH`              | `./agtp-audit.log`   | JSONL output file                                        |
| `AGTP_AUDIT_INCLUDE_INPUT`     | `0`                  | Include request `input` (PII risk; opt-in)               |
| `AGTP_AUDIT_INCLUDE_BODY`      | `0`                  | Include response `body` (PII risk; opt-in)               |
| `AGTP_AUDIT_SIGN_RECEIPTS`     | `0`                  | Ed25519-sign each receipt (requires `[signing]` enabled) |

## Log shape

Default minimal entry:

```json
{
  "timestamp": "2026-05-15T14:23:11Z",
  "method": "BOOK",
  "path": "/room",
  "agent_id": "d8dc6f0d...",
  "request_id": "req-7f3a91b2",
  "principal_id": "chris@example.com",
  "outcome": "ok",
  "status": 200
}
```

With `INCLUDE_INPUT=1`, the entry also carries:

```json
{
  "input": {"guest": "Chris", "room_type": "double"}
}
```

For declared errors:

```json
{
  "outcome": "endpoint_error",
  "error_code": "room_unavailable",
  "error_message": "The presidential suite is not available.",
  "error_details": {"room_type": "presidential_suite"}
}
```

## Consume with normal tools

```bash
# tail
tail -F agtp-audit.log

# parse with jq
jq 'select(.outcome == "endpoint_error")' agtp-audit.log

# count requests per method
jq -r '.method' agtp-audit.log | sort | uniq -c
```

## What this module does not do

- **No signing.** Entries are unsigned. Future revision adds COSE_Sign1.
- **No log rotation.** Use logrotate or a similar tool. The log is
  opened with binary mode and append; rotating the file out from
  under the daemon is safe (the next write recreates the file).
- **No remote sink.** Local file only. Forward via your existing
  log-shipping pipeline (Fluent Bit, Vector, journald → SCITT).
- **No replay verification.** When signing lands, a future
  `agtp-audit-verify` tool will validate signatures and inclusion
  proofs.

## Implementation notes

- `mod_audit.AuditLog` is a thread-safe append-only writer with a
  single internal lock.
- `mod_audit.AuditHook` only implements `after_dispatch` — it never
  short-circuits. The hook protocol allows omitting `before_dispatch`.
- I/O failures degrade gracefully: one stderr warning, then silently
  skip subsequent writes. Audit availability never blocks dispatch.
