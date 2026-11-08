# About zbase (S3Vault)

Self-hosted S3-compatible object storage platform with a web management console.
Initialized from [ztemplate](https://github.com/cvsz/ztemplate).

S3Vault is a **control plane and signing proxy**. It does not implement the S3
wire protocol: SeaweedFS owns object bytes, durability and the wire protocol,
while S3Vault owns identity, policy, credentials, quotas and audit.

## Maintainers

- GitHub organization or owner: [cvsz](https://github.com/cvsz)
- Technical ownership: S3Vault platform team — on-call rotation and maintainer
  rotation are managed through the repository CODEOWNERS and team settings.

## Product definition

- Problem and target users: teams that need self-hosted, S3-compatible object
  storage with centralised access control, credential lifecycle management,
  quota enforcement and an auditable admin console, without exposing the
  storage engine to the public internet.
- Scope and non-goals: in scope are identity, sessions, RBAC, scoped S3
  credentials, bucket/object administration, presigned URLs, quotas, audit
  logging, safety gates, backups and a no-build management console. Out of
  scope are the S3 wire protocol on the public endpoint, multi-engine
  replication, MFA, OIDC SSO and a shared (cross-replica) rate limiter.
- Success metrics: all security controls covered by tests; fail-closed
  configuration validated at startup; 195 tests passing against PostgreSQL 18
  and SeaweedFS 3.97; production deployment starts only with a fully validated
  configuration.
- Data classification and compliance: application metadata (users, sessions,
  credentials, buckets, quotas, audit) lives in PostgreSQL. S3 credential
  secrets are AES-256-GCM envelopes under a key-encryption key held outside the
  database. Object bytes never enter PostgreSQL. No secret is logged, audited or
  returned by an API.
- Support and service expectations: this is a self-hosted platform. The
  operator is responsible for TLS termination, backups, restore drills, engine
  upgrades and security patching. Vulnerabilities are reported through
  SECURITY.md, not public issues.

## Status

Working, with evidence. Every claim is backed by an executed test. See
[README.md](README.md) for the test-evidence summary and the known gaps (no MFA,
no OIDC SSO, per-process rate limiting, no mail transport for password reset).
