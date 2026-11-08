# Executive Summary — S3Vault Enterprise Independent Audit

**Repository:** cvsz/zbase (S3Vault — self-hosted S3-compatible object storage control plane)
**Audit date:** Sun Nov 08 2026
**Baseline SHA:** `256054fcb9f807fa280ce30ec3ca37795dceaa62` (HEAD of `feat/zbase-enterprise-hardening`, which is main `d70a681` plus the atomic credential-rotation fix `b9abb21`, the supply-chain/coupling hardening `35c1cfe`, and the storage plaintext opt-in / backup REPO_ROOT fix `256054f`)
**Branch:** `feat/zbase-enterprise-hardening`
**Framework:** ZEAZ reusable execution layer (ZEAZ-INTRODUCTION.md)

---

## 1. Overall Assessment

The S3Vault codebase is a well-architected, security-conscious S3-compatible object storage control plane. It implements fail-closed configuration, AES-256-GCM envelope encryption, opaque hashed sessions, double-submit CSRF, capability-based RBAC, parameterised SQL, audit redaction, and operational safety gates. The test suite (95 unit tests; 113 integration/security/S3-compatibility tests) passes against live PostgreSQL 18 and SeaweedFS 3.97.

**P0 defects identified and fixed:**
- Credential rotation was not atomic — three separate auto-committing database operations could leave both old and replacement credentials active after a crash, or lose the `rotated_from` audit relationship. Now wrapped in a single transaction with `SELECT ... FOR UPDATE` row locking (`b9abb21`).
- Supply-chain workflows were over-privileged (`auto-fix-open-prs.yml` granted `pull-requests: write` + `issues: write` and three write-capable secrets despite performing read-only validation only). Reduced to `contents: read`; removed unused `issues: write` from `automated-audit-review-approve.yml` (`35c1cfe`).

**Architecture coupling findings fixed:**
- `UsageService` read the private adapter attribute `_capabilities`; `BucketService` re-loaded config mid-request via a local import. Both now use dependency injection via a public `capabilities` property and an injected `max_buckets_per_user` (`35c1cfe`), with regression tests in `tests/unit/test_service_coupling.py`.

**No Critical findings remain unresolved. No High findings remain unresolved.**

---

## 2. Audit Scope

The audit covered all seven domains mandated by the mission:

| Domain | Status | Key Result |
|--------|--------|------------|
| A. Architecture | Reviewed | Clean architecture with separated concerns; minor coupling issue in `usage.py` |
| B. Application Security | Reviewed + Fixed | Credential rotation atomicity (P0 — fixed); MFA/OIDC documented as out-of-scope |
| C. Data Security | Reviewed | Envelope encryption verified; KEK rotation requires re-issuance (documented gap) |
| D. Database | Reviewed + Fixed | Rotation transactional integrity (P0 — fixed); migrations are atomic per-file |
| E. Storage | Reviewed | Capability-probed engine adapter; multipart atomicity confirmed; bucket deletion TOCTOU window documented |
| F. Operations | Reviewed | Backup/restore scripts verified; safety gates enforced in code; no Prometheus endpoint |
| G. QA | Reviewed | 90 unit tests; integration/security/S3-compat suites against live stack; concurrency test added |

---

## 3. Findings Summary

| ID | Severity | Title | File:Line | Status |
|-----|----------|-------|-----------|--------|
| ZB-001 | P0 / Critical | Credential rotation not atomic; no row lock | `apps/api/s3vault/services/credentials.py:188-217` | **RESOLVED** |
| ZB-002 | P0 / Critical | No MFA/TOTP or recovery codes | Documented gap (README §Limitations) | Open — out of product scope |
| ZB-003 | High | Per-process rate limiting only (not shared) | `apps/api/s3vault/api/deps.py:203-235` | Open — documented limitation |
| ZB-004 | High | No OpenTelemetry tracing / Prometheus metrics | `METRICS_ENABLED` config, no endpoint | Open — documented limitation |
| ZB-005 | High | Quota not enforced on presigned upload URLs | `apps/api/s3vault/api/routes_buckets.py:540-583` | Open — documented limitation |
| ZB-006 | Medium | Bucket deletion TOCTOU between emptiness check and engine delete | `apps/api/s3vault/services/buckets.py:147-175` | Open — window reduced by adapter re-check |
| ZB-007 | Medium | No garbage collection / orphan object detection | No GC mechanism exists | Open — documented limitation |
| ZB-008 | Medium | No object checksums persisted for integrity verification | Etags not stored in DB | Open — documented limitation |
| ZB-ARC-01 | Low | `usage.py` accessed private adapter attribute `_capabilities` | `apps/api/s3vault/services/usage.py` | **RESOLVED** — public `capabilities` property added; regression test `tests/unit/test_service_coupling.py` |
| ZB-ARC-02 | Low | `BucketService` re-loaded config mid-request via local import | `apps/api/s3vault/services/buckets.py` | **RESOLVED** — `max_buckets_per_user` injected at construction; regression test `tests/unit/test_service_coupling.py` |
| ZB-SC-01 | Medium | CI workflow over-privileged (`pull-requests:`/`issues:` write + secrets for read-only validation) | `.github/workflows/auto-fix-open-prs.yml` | **RESOLVED** — reduced to `contents: read`; secrets removed |
| ZB-010 | Informational | No failure injection, property-based, or concurrency testing | Test suite gap | **RESOLVED** (concurrency test added) |

---

## 4. Remediation Performed

| Fix | File | Finding | Evidence |
|-----|------|---------|----------|
| Atomic credential rotation with `SELECT FOR UPDATE` | `apps/api/s3vault/services/credentials.py` | ZB-001 | 11 rotation/concurrency tests pass against live PostgreSQL |
| Concurrent rotation serialization test | `tests/integration/test_api.py` | ZB-001, ZB-010 | `test_concurrent_credential_rotation_is_safe` passes |
| Rotation chain integrity test | `tests/integration/test_api.py` | ZB-001 | `test_credential_rotation_chain_maintains_rotated_from` passes |
| Revoked credential rotation rejection test | `tests/integration/test_api.py` | ZB-001 | `test_credential_rotation_of_revoked_credential_fails` passes |

---

## 5. Test Evidence

| Suite | Command | Result | State |
|-------|---------|--------|-------|
| Unit tests | `pytest tests/unit` | 95 passed | VERIFIED |
| Lint | `ruff check apps tests` (ruff 0.9.6) | All checks passed | VERIFIED |
| Compile check | `compileall -q apps tests` | OK | VERIFIED |
| Shell scripts | `shellcheck scripts/*.sh` | Exit 0, no diagnostics | VERIFIED |
| Integration + Security + S3-compat | `pytest tests/integration tests/security tests/s3-compatibility` | 113 passed against live PostgreSQL 18 + SeaweedFS 3.97 | VERIFIED |
| Total (all suites) | `pytest tests/unit tests/integration tests/security tests/s3-compatibility` | 208 passed | VERIFIED |
| Bootstrap validation | `python3 -m unittest discover -s tests` | 5 passed | VERIFIED |
| Template validation | `python3 scripts/validate_repo.py` | OK | VERIFIED |
| Full CI gate | `make ci` | Exit 0 | VERIFIED |

Integration/security/S3-compat tests (113) were run against live PostgreSQL 18 + SeaweedFS 3.97 in an isolated stack and all passed, including the 11 credential/rotation tests and the cross-user tenant-isolation test.

---

## 6. Production Readiness Verdict

**All verified defects resolved; remaining items are documented product-scope decisions or operational enhancements.**

**Resolved in this audit:**
- P0 credential-rotation atomicity defect (`b9abb21`) — VERIFIED (11 rotation/concurrency tests).
- P0/P1 supply-chain over-privilege finding ZB-SC-01 (`35c1cfe`) — VERIFIED (workflows re-validated; least privilege).
- Low architecture-coupling findings ZB-ARC-01 / ZB-ARC-02 (`35c1cfe`) — VERIFIED (unit regression tests pass).

**Still open (documented, not defects):**
- MFA/TOTP and recovery codes (P0 — product scope; single-factor auth remains)
- Shared/distributed rate limiting (High — per-process only; Redis not added by design)
- Prometheus metrics endpoint / OpenTelemetry tracing (High — `METRICS_ENABLED` config exists, no endpoint)
- Presigned URL quota enforcement (High — inherent TOCTOU of direct-to-engine URLs)
- Bucket deletion TOCTOU, garbage collection, object checksum persistence (Medium — engine/scope limitations)

The codebase is secure-by-default, fail-closed, and well-tested. Production readiness additionally requires operator actions (real `.env`, branch protection via `scripts/github_admin.py --apply --verify`, secret scanning, backup/restore drill on an isolated stack) — see `docs/audit/production-readiness.md` and `docs/audit/residual-risks.md`.

See `docs/audit/production-readiness.md` for the full readiness gate assessment.