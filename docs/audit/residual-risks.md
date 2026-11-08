# Residual Risks — S3Vault

**Repository:** cvsz/zbase
**Branch:** `feat/zbase-enterprise-hardening`
**Review date:** Sun Nov 08 2026

---

## 1. Accepted Risks

The following risks are known, documented, and accepted as product-scope decisions or architectural trade-offs. They are not defects and will not be fixed in this release.

| Risk | Description | Impact | Mitigation | Acceptance Basis |
|------|-------------|--------|------------|------------------|
| R-001 | No MFA / TOTP | Single-factor authentication; vulnerable to credential theft | Strong passwords, lockout, rate limiting, session TTL | Documented product scope (README §Limitations item 4) |
| R-002 | No OIDC / SSO | No federated identity; local user management only | Local RBAC, capability-based access control | Documented product scope (README §Limitations item 4) |
| R-003 | Per-process rate limiting | Distributed attacker can bypass per-instance limits | Per-instance login rate limit (5/min), account lockout | Documented architectural trade-off (avoids Redis dependency) |
| R-004 | No Prometheus / OpenTelemetry | Limited observability; no metrics endpoint | Structured JSON logs, audit records, health/readiness endpoints | Documented product scope (README §Limitations item 5) |
| R-005 | Presigned URL quota bypass | Direct-to-engine uploads bypass quota reservation | Quota enforced on API-mediated uploads; presigned TTL capped | Inherent TOCTOU in presigned URL model |
| R-006 | Bucket deletion TOCTOU | Small window between emptiness check and engine delete | Adapter re-checks emptiness before delete; name confirmation required | Engine (SeaweedFS) deletes non-empty buckets without complaint |
| R-007 | No garbage collection | Orphan objects not reclaimed | S3Vault is control plane; engine owns object lifecycle | Control plane scope |
| R-008 | No object checksum persistence | Etags not stored; no end-to-end integrity verification | Engine (SeaweedFS) provides etag-based integrity | Control plane scope |
| R-009 | KEK rotation requires re-issuance | Rotating KEK makes all credentials undecryptable | Documented operator procedure; no re-encryption tool | Documented product scope (README §Limitations item 7) |
| R-010 | Single-node engine durability | One disk's worth of durability; no replication | Single-node deployment documented | Documented product scope (README §Limitations item 4) |
| R-011 | No failure injection testing | No chaos/resilience testing | Standard unit + integration test coverage | Documented gap |
| R-012 | No property-based testing | No generative test coverage | Deterministic integration tests against live stack | Documented gap |

---

## 2. Mitigated Risks (Resolved in This Release)

| Risk | Description | Resolution | Verification |
|------|-------------|------------|--------------|
| R-M-001 | Credential rotation not atomic | Wrapped in `self._db.transaction()` with `SELECT FOR UPDATE` | 11 rotation/concurrency tests pass |
| R-M-002 | `rotated_from` relationship could be inverted | Fixed: replacement row records `rotated_from = <old id>` | `test_credential_rotation_records_rotated_from_on_replacement` passes |
| R-M-003 | Concurrent rotation race | `SELECT FOR UPDATE` serializes rotations | `test_concurrent_credential_rotation_is_safe` passes |

---

## 3. Risks Requiring Operator Action (Pre-Production)

These risks can be mitigated by the operator before production traffic:

| Risk | Description | Required Action | State |
|------|-------------|-----------------|-------|
| R-O-001 | Branch protection not configured | Run `scripts/github_admin.py --apply --verify` with admin identity | BLOCKED (needs admin API) |
| R-O-002 | GPG signing key not registered on GitHub | Register public key `F264197ECE172BFC616BF73CA6D60018D84C3BBD` on cvsz account | UNVERIFIED |
| R-O-003 | `.env` not created with real secrets | Copy `.env.example` to `.env`, fill in real values | Not done |
| R-O-004 | Backup/restore drill not performed | Run `scripts/backup.sh` + `scripts/restore.sh` on isolated stack | Not done |
| R-O-005 | Secret scanning not enabled | Enable GitHub secret scanning and push protection | Not done |
| R-O-006 | PR review workflow not completed | Complete cvsz → policedbc → cvsz review/merge workflow | BLOCKED (needs admin API) |

---

## 4. Risk Matrix

| Risk | Likelihood | Impact | Severity | State |
|------|-----------|--------|----------|-------|
| R-001 (No MFA) | Medium | High | High | Accepted (product scope) |
| R-002 (No OIDC) | Medium | Medium | Medium | Accepted (product scope) |
| R-003 (Per-process rate limit) | Low | Medium | Medium | Accepted (architectural) |
| R-004 (No metrics) | Low | Low | Low | Accepted (product scope) |
| R-005 (Presigned quota bypass) | Low | Medium | Medium | Accepted (inherent) |
| R-006 (Bucket delete TOCTOU) | Low | High | Medium | Accepted (engine limitation) |
| R-007 (No GC) | Low | Low | Low | Accepted (scope) |
| R-008 (No checksum persistence) | Low | Low | Low | Accepted (scope) |
| R-009 (KEK rotation) | Low | High | Medium | Accepted (documented) |
| R-010 (Single-node durability) | Low | High | High | Accepted (scope) |
| R-O-001 (Branch protection) | N/A | N/A | Critical | BLOCKED — must resolve pre-production |
| R-O-002 (GPG key) | N/A | N/A | Low | UNVERIFIED — operator action |
| R-O-003 (Real secrets) | N/A | N/A | Critical | Not done — operator action |
| R-O-004 (Backup drill) | N/A | N/A | High | Not done — operator action |

---

## 5. Risk Acceptance Statement

The residual risks listed in Section 1 are accepted as product-scope decisions or architectural trade-offs. They do not constitute defects and do not block the remediation of verified defects (Section 2).

The risks in Section 3 require operator action before production deployment. Until those are resolved, the system should not be considered production-ready.
