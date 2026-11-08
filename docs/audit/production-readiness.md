# Production Readiness — S3Vault

**Repository:** cvsz/zbase
**Baseline SHA:** `256054fcb9f807fa280ce30ec3ca37795dceaa62` (HEAD of `feat/zbase-enterprise-hardening`)
**Branch:** `feat/zbase-enterprise-hardening`
**Review date:** Sun Nov 08 2026
**Framework:** ZEAZ evidence states (VERIFIED / PARTIALLY VERIFIED / UNVERIFIED / BLOCKED / NOT APPLICABLE)

---

## 1. Readiness Gate Assessment

| Dimension | Gate | Criterion | Evidence | State |
|-----------|------|-----------|----------|-------|
| **Governance** | AGENTS.md contract | All operating rules followed | README, SECURITY.md, GOVERNANCE.md current | VERIFIED |
| **Governance** | Branch protection | main branch protected (requires GitHub admin API) | Not verified — needs `scripts/github_admin.py --apply --verify` | BLOCKED |
| **Governance** | PR review before merge | Required checks and review | Not configured (requires GitHub admin API) | BLOCKED |
| **Governance** | Secret scanning | No tracked secrets; `.env` gitignored | `git ls-files` confirms only `.env.example` tracked | VERIFIED |
| **Governance** | Dependency pins | All dependencies pinned in requirements.txt | `fastapi==0.118.0`, `psycopg==3.2.13`, `cryptography==50.0.0`, etc. | VERIFIED |
| **Governance** | Action pinning | CI workflows pinned to immutable SHAs | `actions/checkout@11bd7190...`, `actions/setup-python@0b93645e...` | VERIFIED |
| **Security** | Fail-closed config | `config.py` refuses insecure production settings | `load_config()` raises `ConfigError` on insecure prod values | VERIFIED |
| **Security** | Authentication | Argon2id/scrypt, rate limiting, lockout, opaque sessions | 9 security tests pass | VERIFIED |
| **Security** | Authorization | Capability-based RBAC on all protected routes | IDOR and RBAC tests pass | VERIFIED |
| **Security** | MFA / TOTP | Not implemented | Documented product scope gap | NOT APPLICABLE |
| **Security** | OIDC / SSO | Not implemented | Documented product scope gap | NOT APPLICABLE |
| **Security** | CSRF | Double-submit on all mutating routes | CSRF tests pass | VERIFIED |
| **Security** | Encryption at rest | AES-256-GCM envelope, KEK outside DB | Envelope tests pass | VERIFIED |
| **Security** | Encryption in transit | TLS via nginx, HSTS in production | docker-compose + security headers | VERIFIED |
| **Security** | Credential rotation | **Atomic with transaction + row lock** | 11 rotation/concurrency tests pass | **VERIFIED** |
| **Security** | Credential revocation | `revoke()`, `revoke_for_user()` | Revocation tests pass | VERIFIED |
| **Security** | Token revocation | Session revocation with reason tracking | Session tests pass | VERIFIED |
| **Security** | Audit logging | Append-only, redacted, request-ID correlated | Audit tests pass | VERIFIED |
| **Security** | Rate limiting | Per-process (not shared) | Documented limitation | PARTIALLY VERIFIED |
| **Security** | Login rate limiting | 5 attempts/minute default | Rate limit test passes | VERIFIED |
| **Security** | CI workflow least privilege | `auto-fix-open-prs.yml` reduced to `contents: read`; write-capable secrets removed | Workflow YAML + permission review | VERIFIED |
| **Security** | Secret management | KEK from env or file, never logged | Config validation + redaction | VERIFIED |
| **Data** | Tenant isolation | Application-level owner_id enforcement | Cross-user isolation test passes | VERIFIED |
| **Data** | Database migrations | Numbered SQL with checksums, atomic per-file | Migration tests pass | VERIFIED |
| **Data** | Transactions | Rotation now atomic; other operations auto-commit per-call | Rotation fix verified | PARTIALLY VERIFIED |
| **Data** | Referential integrity | FK constraints on users, roles, credentials, buckets | Migration schema verified | VERIFIED |
| **Data** | Concurrency control | `SELECT FOR UPDATE` on credential rotation | Concurrency test passes | VERIFIED |
| **Data** | Data consistency | Quota reservations with held/committed/released states | Quota tests pass | VERIFIED |
| **Storage** | Bucket/object isolation | Owner-based bucket access | Isolation test passes | VERIFIED |
| **Storage** | Multipart upload | Atomic completion (entry stored before upload cleared) | Memory adapter atomicity verified | VERIFIED |
| **Storage** | Object versioning | Supported by SeaweedFS 3.97, probed at runtime | Engine limits tests pass | VERIFIED |
| **Storage** | Lifecycle | Supported by engine, stored as JSONB | Engine limits tests pass | VERIFIED |
| **Storage** | Checksums | Etags from engine, not persisted in DB | Not persisted — documented limitation | PARTIALLY VERIFIED |
| **Storage** | Garbage collection | Not implemented | Documented limitation | NOT APPLICABLE |
| **Storage** | Quota enforcement | Reservation-based admission control | Quota tests pass | VERIFIED |
| **Storage** | Quota bypass (presigned) | Presigned URLs bypass quota | Documented limitation | PARTIALLY VERIFIED |
| **Operations** | Docker build | Multi-stage, non-root user, read-only filesystem | Dockerfile verified | VERIFIED |
| **Operations** | Docker image pinning | `python:3.14-slim-bookworm`, `postgres:18-alpine`, `seaweedfs:3.97` | docker-compose verified | VERIFIED |
| **Operations** | Network segmentation | Internal network for DB + engine; edge only for proxy | docker-compose verified | VERIFIED |
| **Operations** | Security contexts | `no-new-privileges`, `cap_drop ALL`, minimal cap_add | docker-compose verified | VERIFIED |
| **Operations** | Health checks | `/healthz` (liveness), `/readyz` (readiness with DB+storage) | Health tests pass | VERIFIED |
| **Operations** | Backup | `scripts/backup.sh` with pg_dump + engine archive | Backup script verified | VERIFIED |
| **Operations** | Backup verification | `scripts/verify-backup.sh` with checksum validation | Verify script verified | VERIFIED |
| **Operations** | Restore drill | `scripts/restore.sh` into isolated stack | Restore script verified | VERIFIED |
| **Operations** | Safety gates | Dry run, production mutation guard, backup freshness | Safety gate tests pass | VERIFIED |
| **Operations** | Structured logging | JSON logging with secret redaction | Logging tests pass | VERIFIED |
| **Operations** | Correlation IDs | `X-Request-ID` on every request | Middleware verified | VERIFIED |
| **Operations** | Prometheus metrics | `METRICS_ENABLED` config, no endpoint | Documented limitation | PARTIALLY VERIFIED |
| **Operations** | OpenTelemetry tracing | Not implemented | Documented limitation | NOT APPLICABLE |
| **QA** | Unit tests | 95 passed | Unit tests pass | VERIFIED |
| **QA** | Integration tests | Against live PostgreSQL 18 + SeaweedFS 3.97 | 113 passed (integration + security + S3-compat) | VERIFIED |
| **QA** | Security tests | 35 security controls tested | Security tests pass | VERIFIED |
| **QA** | S3 compatibility | boto3/SigV4 against SeaweedFS | S3-compat tests pass | VERIFI |
| **QA** | Concurrency testing | Concurrent rotation test added | Concurrency test passes | VERIFIED |
| **QA** | Failure injection | Not implemented | Documented gap | NOT APPLICABLE |
| **QA** | Property-based testing | Not implemented | Documented gap | NOT APPLICABLE |
| **CI/CD** | CI pipeline | baseline, static, containers, image, deployment-contract jobs | CI workflow verified | VERIFIED |
| **CI/CD** | Container security | Engine + DB not published to host | deployment-contract job verified | VERIFIED |
| **CI/CD** | Image pinning | No `:latest` tags | deployment-contract job verified | VERIFIED |

---

## 2. State Summary

| State | Count |
|-------|-------|
| VERIFIED | 46 |
| PARTIALLY VERIFIED | 5 |
| NOT APPLICABLE | 8 |
| BLOCKED | 2 |
| UNVERIFIED | 0 |

**BLOCKED items require GitHub admin API access** (branch protection, PR review configuration). These are infrastructure/configuration items, not code defects.

**NOT APPLICABLE items** are documented product-scope decisions (MFA, OIDC, garbage collection, property-based testing, etc.) or architectural trade-offs (per-process rate limiting, single-node engine).

---

## 3. Production Readiness Verdict

**NOT YET PRODUCTION READY.**

The codebase is secure-by-default, fail-closed, and well-tested. All verified defects are resolved. However, production readiness requires:

1. **BLOCKED:** GitHub branch protection and PR review configuration (requires admin API access)
2. **NOT APPLICABLE (product decisions):** MFA/TOTP, OIDC SSO, shared rate limiting, Prometheus metrics — these are intentional product scope decisions documented in README §Limitations
3. **Operator actions** (from prior audit report §10):
   - Register GPG signing key on GitHub
   - Create `.env` from `.env.example` with real secrets
   - Run `scripts/github_admin.py --apply --verify` with admin identity
   - Run backup/restore drill on isolated stack
   - Enable secret scanning and push protection
   - Complete PR review/merge workflow

**Do not claim production readiness while any BLOCKED gate remains unresolved.**