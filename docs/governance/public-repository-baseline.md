# Public repository baseline

This record captures the repository-owned controls reviewed under issue #3. It is evidence for repository hygiene and change-control posture; it is not an external certification and does not transfer upstream authority.

| Control | State | Evidence | Residual risk |
|---|---|---|---|
| Purpose, status, intended use and implementation context | PASS | `README.md` | Upstream normative intent remains upstream-owned. |
| Licensing | PASS | `LICENSE` | None identified. |
| Security reporting and supported-version policy | PASS | `SECURITY.md` | GitHub private-vulnerability-reporting enablement remains a hosted setting. |
| Contribution and support guidance | PASS | `CONTRIBUTING.md`, `SUPPORT.md`, existing issue/PR templates | None identified. |
| Code of conduct | PASS | `CODE_OF_CONDUCT.md` | None identified. |
| Dependency update management | PASS | `.github/dependabot.yml` | Dependabot platform enablement is GitHub-hosted. |
| Workflow permissions | PASS / bounded | Documentation workflow declares explicit permissions; upstream workflows retain the permissions needed for synchronization. | Privileged upstream-sync behaviour remains an explicit trust boundary and must not execute untrusted PR code. |
| Default-branch protection | EVIDENCE REQUIRED | GitHub rulesets API returned no active ruleset on 2026-09-05. | Tracked separately as a repository-setting governance issue; this document MUST NOT represent branch protection as PASS until observed. |
| Documentation validation | PASS | `.github/workflows/docs.yml` runs link checking and strict MkDocs build. | None identified. |
| Upstream authority boundary | PASS | `.upstream/`, `.github/upstream-tracking.yml`, repository documentation | Repository-local changes do not imply upstream adoption. |

## Completion boundary

Repository-owned baseline gaps are closed by the associated remediation PR. The remaining default-branch protection control is deliberately separated because it requires a GitHub repository-setting action rather than a code change.
