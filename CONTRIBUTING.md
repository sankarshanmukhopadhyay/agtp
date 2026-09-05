# Contributing

Contributions should preserve the distinction between repository-local implementation authority and upstream normative authority.

For substantive changes, use the visible engineering trail:

1. open or reference an issue that states the problem/proposition, scope, authority boundary, and acceptance criteria;
2. implement the smallest coherent change on a branch;
3. add or update tests/evidence for consequential claims and important negative cases;
4. open a pull request describing the change, evidence, compatibility impact, residual risk, and any upstream consequence;
5. merge only after the repository's applicable validation checks pass.

Documentation-only corrections may use a lighter process when they do not alter semantics, compatibility, security posture, or governance.

Do not treat missing evidence as a pass. If an assertion cannot be verified, record it as indeterminate/evidence-required or track it explicitly as residual work.

Security reports must follow [`SECURITY.md`](SECURITY.md).
