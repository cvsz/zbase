# Governance

## Scope

This document defines the default governance model for projects created from this template. Generated projects must replace generic ownership and decision guidance with their real maintainers, escalation paths, and operating model.

## Roles

- **Maintainers**: review changes, protect quality/security, manage releases, and maintain repository controls.
- **Contributors**: propose focused changes through issues and pull requests and follow `CONTRIBUTING.md`.
- **Security contacts**: receive vulnerability reports through the private path defined in `SECURITY.md`.
- **Repository administrators**: maintain branch/ruleset, Actions, security-feature, and emergency-access settings.

## Change control

Normal changes flow through pull requests with required checks and review. Repository administration controls should be verified with:

```bash
python3 scripts/github_admin.py --repo OWNER/REPO --verify
```

A generated repository should not rely on documentation alone to prove that its branch protections or security settings are effective.

## Decisions

Prefer documented, reviewable decisions. Significant architecture, security, compatibility, data, or operational decisions should use an ADR under `docs/adr/`.

Changes affecting public contracts, security boundaries, authentication/authorization, release policy, production infrastructure, or recovery procedures require maintainer review and evidence appropriate to the risk.

## Emergency changes

Emergency changes should use the smallest reversible scope, record the reason and operator, preserve security controls where possible, and be reviewed afterward. Do not normalize routine bypass of required checks or protected-branch controls.

## Conflicts of interest

Reviewers should disclose material conflicts and avoid sole approval when independent review is reasonably available.

## Evidence and readiness

Governance approval, accepted risk, and production readiness are separate concepts. Accepted risk never converts an unverified readiness gate into `VERIFIED`.

Use the canonical evidence states in `ZEAZ-INTRODUCTION.md`.

## Amendments

Governance changes are made by pull request and should explain reason, impact, migration expectations, validation, and rollback.
