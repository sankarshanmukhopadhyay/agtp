---
title: Upstream synchronisation governance
---

# Upstream synchronisation governance

This repository uses a deliberately one-way control plane. The declared upstream, `nomoticai/agtp`, is readable; this fork is the only writable target. Upstream changes are detected automatically, raised as evidence-backed issues, and integrated only through a draft pull request reviewed under normal branch protections.

## Authority

The workflows may fetch upstream, create or update issues in this fork, create a branch in this fork, and open a draft pull request against this fork. They may not push to upstream, create an upstream issue, open an upstream pull request, approve their own change, or merge automatically.

## Procedure

1. The monitor runs every ten days or on manual dispatch.
2. A drift report distinguishes observed, reviewed and integrated commits.
3. A maintainer runs **Upstream synchronisation** to prepare a merge-based draft pull request.
4. Protected local paths and semantic conflicts receive human review.
5. Existing validation and publication checks remain mandatory.
6. Only a human-authorised merge integrates the upstream change.

## Revocation and evidence

Disabling the policy or workflows, or removing write permissions, immediately revokes automation authority. Checkpoint data, workflow artifacts, issues, pull requests, checks and merge history form the audit chain.
