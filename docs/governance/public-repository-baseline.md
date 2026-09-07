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
| Default-branch protection | PASS | GitHub ruleset `protect-main` observed active on 2026-09-07 targeting `~DEFAULT_BRANCH`; deletion and non-fast-forward updates are blocked; linear history and pull requests are required; review-conversation resolution is required; status check `build` is required; bypass actors are empty | The required-status-check policy is not configured as strict/up-to-date; this was not part of issue #5's required control, but remains an explicit hosted-policy characteristic. Ruleset changes require renewed observation. |
| Documentation validation | PASS | `.github/workflows/docs.yml` runs link checking and strict MkDocs build. | None identified. |
| Upstream authority boundary | PASS | `.upstream/`, `.github/upstream-tracking.yml`, repository documentation | Repository-local changes do not imply upstream adoption. |

## Completion boundary

Repository-owned baseline gaps are closed by the associated remediation PR. The separated GitHub-hosted default-branch control has now been independently re-observed through the rulesets API and is recorded as PASS on that evidence.

Missing or stale hosted-control evidence MUST NOT be interpreted as PASS. A future change to the ruleset target, required check, bypass authority or enforcement state requires reassessment.
