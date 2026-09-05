# Security Policy

## Supported versions

Security fixes are applied to the current `main` branch and, where a release line is maintained, the latest supported release. Older releases should be treated as unsupported unless a maintainer explicitly states otherwise.

## Reporting a vulnerability

Do not disclose an undisclosed vulnerability in a public issue. Use GitHub private vulnerability reporting when available, or contact the repository maintainer through a private channel identified on the maintainer profile.

Include the affected revision/version, reproduction steps, impact, and any known mitigation. Reports are triaged against implementation impact, protocol/authority boundaries, evidence integrity, compatibility, and any required revocation or supersession action.

## Scope and authority

This repository contains an adapted implementation with explicit upstream-tracking machinery. A repository-local security fix does not imply that upstream specifications or implementations have adopted the same change. Where a defect belongs to an upstream authority, the repository will preserve the local mitigation/evidence trail and route the upstream issue separately.
