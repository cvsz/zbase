# Gap Analysis — S3Vault

**Repository:** cvsz/zbase
**Baseline SHA:** `d70a681cc9f88884f644d5c43b0a259b62d78635`
**Reviewer date:** Sun Nov 08 2026

---

## 1. Methodology

Gaps are classified as:

- **Defect** — verified incorrect behaviour (fixable, P0/P1)
- **Missing capability** — feature not implemented (documented product scope or future work)
- **Limitation** — known architectural constraint (accepted trade-off)

Evidence states follow ZEAZ-INTRODUCTION.md: VERIFIED, PARTIALLY VERIFIED, UNVERIFIED, BLOCKED, NOT APPLICABLE.

---

## 2. P0 — Critical Security / Data Integrity

### ZB-001: Credential rotation not atomic (RESOLVED)

**Gap:** `CredentialService.rotate()` performed three separate auto-committing database operations:
1. Insert replacement credential
2. Revoke original credential
3. Set `rotated_from` on replacement

A crash between steps 1–2 left both credentials active (secret still valid). A crash between steps 2–3 lost the `rotated_from` audit relationship. No row lock meant concurrent rotations of the same credential could race.

**Fix:** Wrapped all three operations in `self._db.transaction()` with `SELECT ... FOR UPDATE` on the original credential row.

**File:** `apps/api/s3vault/services/credentials.py:188-233`
**Evidence:** 11 rotation/concurrency tests pass against live PostgreSQL 18
**State:** VERIFIED — RESOLVED

### ZB-002: No MFA/TOTP or recovery codes

**Gap:** No multi-factor authentication. Password is the sole authentication factor.

**Status:** Documented product scope gap (README §Limitations, item 4). Not a defect — an intentional product decision. Implementing TOTP would require UI changes, secret storage for TOTP seeds, backup codes, and recovery flows.

**State:** NOT APPLICABLE to this release scope. Open for future work.

---

## 3. P1 — Major Functionality / Reliability Gaps

### ZB-003: Per-process rate limiting (not shared)

**Gap:** `SlidingWindowLimiter` in `api/deps.py:203-235` is in-process only. Multiple replicas each enforce their own window, so a distributed attacker can bypass per-instance limits by spreading requests across replicas.

**Status:** Documented limitation (docs/SECURITY.md). Not a defect — a deliberate trade-off to avoid adding Redis to the deployment.

**State:** PARTIALLY VERIFIED — per-instance limiting works; shared limiting not implemented.

### ZB-004: No OpenTelemetry tracing / Prometheus metrics

**Gap:** `METRICS_ENABLED=true` config exists but no metrics endpoint or tracing instrumentation. Observability is limited to structured JSON logs and audit records.

**Status:** Documented limitation (README §Limitations, item 5).

**State:** NOT APPLICABLE to this release scope. Open for future work.

### ZB-005: Quota not enforced on presigned upload URLs

**Gap:** `presigned_upload()` (`routes_buckets.py:540-583`) issues direct-to-engine URLs that bypass the quota reservation system. A user with a presigned URL can upload unlimited data regardless of configured quota.

**Status:** Known limitation. Presigned uploads go directly to the engine, bypassing application-level admission control. Quota enforcement at presign time would be best-effort only (TOCTOU between URL issuance and actual upload).

**State:** PARTIALLY VERIFIED — quota reservation works for API-mediated uploads; presigned bypass documented.

### ZB-006: Bucket deletion TOCTOU window

**Gap:** `buckets.py:147-175` checks emptiness (`list_objects(max_keys=1)`) and then calls `adapter.delete_bucket()`. Between these calls, objects could be uploaded. SeaweedFS deletes non-empty buckets without complaint.

**Mitigation:** The adapter's `delete_bucket()` re-checks emptiness when `force=False` (`seaweedfs.py:452-456`), reducing the window to the gap between the adapter's check and the S3 DELETE Bucket API call.

**State:** PARTIALLY VERIFIED — double-check reduces risk; engine-level conditional delete not available.

---

## 4. P2 — Storage Reliability / API Correctness

### ZB-007: No garbage collection / orphan object detection

**Gap:** No mechanism to detect objects in the engine that are not tracked in S3Vault metadata. Orphan objects (uploaded directly to the engine, or left from aborted multipart uploads) are not reclaimed.

**Status:** Documented limitation. S3Vault is a control plane, not the S3 protocol itself; objects uploaded directly to the engine are expected.

**State:** NOT APPLICABLE to control plane scope.

### ZB-008: No object checksums persisted for integrity verification

**Gap:** Etags are returned by the engine but not stored in S3Vault metadata. No end-to-end checksum verification on read.

**Status:** Documented limitation. The engine (SeaweedFS) provides its own integrity via etags; S3Vault does not mirror object metadata.

**State:** NOT APPLICABLE to control plane scope.

### ZB-009: `usage.py` accesses private adapter attribute

**Gap:** `services/usage.py:127` accesses `self._adapter._capabilities`, a private attribute of the concrete adapter.

**State:** Cosmetic coupling. Works correctly but breaks abstraction boundary.

**Recommendation:** Add a `capabilities` property to the `StorageAdapter` Protocol or inject `Capabilities` into `UsageService`.

---

## 5. P3 — Observability / Operations / Developer Experience

### ZB-010: No failure injection / property-based testing (RESOLVED)

**Gap:** No failure injection, property-based, or dedicated concurrency testing.

**Fix:** Added `test_concurrent_credential_rotation_is_safe` verifying concurrent rotation serialization.

**State:** VERIFIED — RESOLVED (concurrency coverage added).

### ZB-011: No Prometheus alerting rules

**Gap:** No Prometheus scrape config or alerting rules.

**Status:** Documented limitation (README §Limitations, item 5).

**State:** NOT APPLICABLE to this release scope.

### ZB-012: KEK rotation requires re-issuing credentials

**Gap:** Rotating the KEK makes all existing credentials undecryptable. No re-encryption tool exists.

**Status:** Documented limitation (README §Limitations, item 7).

**State:** NOT APPLICABLE to this release scope.

### ZB-013: Single-node engine durability

**Gap:** Single SeaweedFS node — one disk's worth of durability. No replication or erasure coding.

**Status:** Documented limitation (README §Limitations, item 4).

**State:** NOT APPLICABLE — infrastructure decision, not code defect.

---

## 6. Gap Summary by Severity

| Severity | Count | Resolved | Open |
|----------|-------|----------|------|
| P0 / Critical | 2 | 1 (ZB-001) | 1 (ZB-002 — product scope) |
| High | 3 | 0 | 3 (ZB-003, ZB-004, ZB-005) |
| Medium | 4 | 0 | 4 (ZB-006 through ZB-009) |
| P3 / Informational | 4 | 1 (ZB-010) | 3 (ZB-011 through ZB-013) |
| **Total** | **13** | **2** | **11** |
