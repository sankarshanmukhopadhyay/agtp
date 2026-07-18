# Deferred-work register

## Decision rule

A reference to future work is not automatically an incomplete repository task. This register separates deliberate protocol-version boundaries from actionable implementation or documentation debt.

## Closed deferred security work

The v0.2.0 security-hardening work is implemented and evidenced by tests and operator documentation. The completed controls include merchant replay protection, durable RCNS abuse-control state, audit-chain recovery, and strict OAuth validator boot posture. See [Security hardening](security-hardening.md) and [v0.2.0 release notes](../RELEASE_NOTES_v0.2.0.md).

## Deliberate version boundaries

| Item | Current disposition | Completion trigger | Evidence required |
| --- | --- | --- | --- |
| Multi-version method catalogs | Single-version validation is intentional | A migration profile requires concurrent catalog validation | Compatibility fixtures and cross-version conformance tests |
| Gateway streaming responses | Excluded from gateway protocol v1 | A framed streaming contract is approved | Backpressure, cancellation, ordering, and reconnect tests |
| Module-initiated outbound calls | Excluded from gateway protocol v1 | Daemon pooling and authority policy are specified | Pool isolation, authorization, timeout, and audit evidence |
| Durable PROPOSE storage | In-memory v00 behavior is documented | Persistence semantics and recovery rules are approved | Restart, expiry, concurrency, and corruption-recovery tests |
| Reverse AGTP-to-OpenAPI generation | Out of scope for current converter | A stable loss-mapping policy exists | Round-trip fixtures and explicit loss reports |
| Certificate SCT extension | Reserved for a later certificate profile | SCT encoding and verification rules are normative | Certificate vectors and failure-mode tests |
| SCITT audit mode | Reserved; unsupported mode fails closed | Receipt profile and trust-anchor operations are defined | Interoperability vectors and boot/runtime conformance tests |
| Elemen certificate/signature pane | UI placeholder tied to later certificate UX | Certificate inspection requirements are approved | Browser tests and signature-validation evidence |

## Actionable maintenance items

| Item | Status after this commit | Evidence |
| --- | --- | --- |
| Full GitHub Pages publication | Completed | Strict MkDocs build and Pages workflow |
| Markdown link integrity | Completed | `scripts/check_markdown_links.py` |
| Test import-path configuration | Completed | Repository-level `pytest.ini` |
| Documentation inventory and authority model | Completed | [Documentation status](documentation-status.md) |

## Governance of future deferrals

Any new deferred item should identify an owner or authority, affected scope, enforcement impact, completion trigger, and machine-verifiable evidence. Deferral language without these fields should be treated as an unresolved documentation defect.
