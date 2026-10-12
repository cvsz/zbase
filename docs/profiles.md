# Project Profiles

Profiles are adoption guides, not production-ready starter applications. Every generated project must replace placeholders and verify its own runtime/security/operations.

## Common baseline for every profile

- initialize identity with `scripts/bootstrap.py`
- run `make validate-template`
- configure real CODEOWNERS/security contacts
- apply and verify GitHub administration controls
- customize CI/security checks for the real stack
- replace placeholder Dockerfile/Makefile application targets
- complete architecture/development/release documentation
- establish rollback and recovery evidence appropriate to the system

## Service / API

Add:

- runtime/package-manager pinning
- unit/integration/API contract tests
- authentication/authorization tests
- database/migration handling
- container/build validation
- health/readiness endpoints
- deployment/rollback strategy
- logs/metrics/traces and alerting
- backup/restore if stateful

## Web application

Add:

- frontend build/lint/typecheck/test
- browser/E2E coverage for critical paths
- accessibility checks where applicable
- security headers/session/CSRF controls
- asset/build provenance as appropriate
- real backend/API integration verification
- deployment/rollback and monitoring

## Library / SDK

Add:

- supported runtime matrix
- API/compatibility tests
- packaging validation
- release provenance/signing if required
- versioning/deprecation policy

## Monorepo

Add:

- workspace-aware change detection
- package/app ownership boundaries
- dependency graph validation
- consistent release/version policy
- scoped CI without hiding required cross-package integration tests

## Infrastructure / platform

Add:

- IaC formatting/validation/plan checks
- explicit state/ownership boundaries
- least-privilege deployment credentials
- change approval/environment protection
- rollback/failover/recovery procedures
- drift detection and observability

Choose the smallest profile that matches the product. Do not add modules merely for checklist completeness.
