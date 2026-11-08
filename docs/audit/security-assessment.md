# Security Assessment — S3Vault

**Repository:** cvsz/zbase
**Baseline SHA:** `256054fcb9f807fa280ce30ec3ca37795dceaa62` (HEAD of `feat/zbase-enterprise-hardening`)
**Reviewer date:** Sun Nov 08 2026
**Framework:** OWASP Top 10 + ZEAZ evidence states

---

## 1. Authentication (OWASP A07:2021 — Identification and Authentication Failures)

| Control | Implementation | File | State |
|---------|---------------|------|-------|
| Password hashing | Argon2id when available, scrypt by default; hash carries algorithm ID for transparent upgrade | `security/passwords.py` | VERIFIED |
| Minimum password length | Configurable, default 12, blocked common passwords | `services/users.py:74-81` | VERIFIED |
| Brute-force lockout | 8 failures → 15-minute lockout; uniform error messages | `services/users.py:29-31, 164-231` | VERIFIED |
| Login rate limiting | Per-IP sliding window, 5 attempts/minute default | `api/deps.py:203-249`, `routes_auth.py:99` | VERIFIED |
| Account enumeration resistance | Identical response for known/unknown users | `routes_auth.py:105-118`, `services/users.py:176-178` | VERIFIED |
| Session tokens | Opaque 256-bit, SHA-256 hashed storage, HttpOnly + Secure + SameSite cookies | `security/tokens.py`, `api/deps.py:131-157` | VERIFIED |
| Session idle expiry | Configurable idle TTL, updated on each request | `services/users.py:296-300` | VERIFIED |
| Session absolute expiry | Configurable absolute TTL | `services/users.py:247-248` | VERIFIED |
| Session revocation on password change | All sessions revoked | `services/users.py:409`, `382` | VERIFIED |
| Password reset | Single-use hashed token, 1-hour TTL, revokes all sessions | `services/users.py:337-382` | VERIFIED |
| MFA / TOTP | **Not implemented** | — | NOT APPLICABLE (documented product scope gap) |
| OIDC / SSO | **Not implemented** | — | NOT APPLICABLE (documented product scope gap) |

---

## 2. Authorization (OWASP A01:2021 — Broken Access Control)

| Control | Implementation | File | State |
|---------|---------------|------|-------|
| Capability-based RBAC | 11 capabilities across 4 roles (admin, owner, operator, auditor) | `security/rbac.py` | VERIFIED |
| Route-level capability enforcement | `require_capability()` dependency on all mutating routes | `api/deps.py:168-179` | VERIFIED |
| Bucket ownership isolation | `assert_can_read` / `assert_can_write` / `assert_owner_or_admin` | `services/buckets.py:73-89` | VERIFIED |
| Cross-user bucket access blocked | Tested: non-owner gets 403 | `tests/integration/test_api.py:447-487` | VERIFIED |
| Credential scope enforcement | Bucket + prefix scope, permission checks | `services/credentials.py:278-301` | VERIFIED |
| Auditor read-only enforcement | Tested: auditor cannot mutate | `tests/security/test_security_controls.py:317-334` | VERIFIED |
| Admin self-service restrictions | Non-admin cannot modify other users' credentials | `routes_system.py:131`, `170` | VERIFIED |
| IDOR protection | Tested: non-admin cannot read other users | `tests/security/test_security_controls.py:292-314` | VERIFIED |

---

## 3. Cryptographic Controls

| Control | Implementation | File | State |
|---------|---------------|------|-------|
| KEK (key-encryption key) | 32-byte base64, from env or KEK_FILE, validated at startup | `config.py:297-307`, `security/envelope.py:61-82` | VERIFIED |
| DEK (data-encryption key) | Fresh 256-bit random per credential | `security/envelope.py:96` | VERIFIED |
| Envelope encryption | AES-256-GCM: KEK wraps DEK, DEK encrypts secret; separate nonces | `security/envelope.py:90-107` | VERIFIED |
| AAD binding | Ciphertext bound to credential_id + access_key_id | `security/envelope.py:85-87` | VERIFIED |
| Nonce uniqueness | Fresh nonce per operation, never reused | `security/envelope.py:99, 102` | VERIFIED |
| Secret shown once | Plaintext returned in create/rotate response only; never stored | `services/credentials.py:118-129` | VERIFIED |
| Secret not retrievable | `resolve_secret` decrypts on demand; not stored in plaintext | `services/credentials.py:232-275` | VERIFIED |
| KEK rotation | Requires re-issuing all credentials; no re-encryption tool | Documented | NOT APPLICABLE (known gap) |
| Credential rotation | Now atomic with `SELECT FOR UPDATE` | `services/credentials.py:188-233` | **VERIFIED (fixed)** |

---

## 4. Session Management

| Control | Implementation | File | State |
|---------|---------------|------|-------|
| Opaque tokens (no JWT) | SHA-256 hash stored, token only in client cookie | `security/tokens.py`, `services/users.py:244-269` | VERIFIED |
| CSRF double-submit | Separate CSRF cookie (readable by JS) + header on mutating requests | `api/deps.py:100-128` | VERIFIED |
| CSRF on all mutating routes | Verified: logout, credential revoke, bucket delete, user create all require CSRF | `tests/security/test_security_controls.py:124-162` | VERIFIED |
| Session cookie attributes | HttpOnly, Secure, SameSite, Path=/ | `api/deps.py:139-157` | VERIFIED |
| CSRF cookie attributes | Not HttpOnly (readable by console), Secure, SameSite | `api/deps.py:152-157` | VERIFIED |
| Token revocation | `revoke_session`, `revoke_all_sessions` with reason tracking | `services/users.py:302-319` | VERIFIED |
| Expired session cleanup | Worker purges expired sessions | `worker.py:114-125` | VERIFIED |

---

## 5. Input Validation (OWASP A03:2021 — Injection)

| Control | Implementation | File | State |
|---------|---------------|------|-------|
| Parameterised SQL | All queries use `%s` placeholders via psycopg | `db.py:87-100` | VERIFIED |
| SQL identifier validation | `safe_identifier()` regex guard for dynamic identifiers | `db.py:187-191` | VERIFIED |
| Object key traversal rejection | Rejects `..`, `/` prefix, `%2f`, `%5c`, control characters | `security/tokens.py:85-113` | VERIFIED |
| Bucket name validation | DNS-compatible S3 naming, no silent normalization | `security/tokens.py:74-83`, `services/buckets.py:93-110` | VERIFIED |
| CORS origin validation | Must be `*` or absolute http(s) URL | `services/buckets.py:206-211` | VERIFIED |
| Bucket policy validation | Statement/Effect/Action/Resource structure checked | `services/buckets.py:299-315` | VERIFIED |
| Presigned URL TTL cap | Clamped to `PRESIGN_MAX_TTL_S` | `routes_buckets.py:522` | VERIFIED |
| Body size limit | Streaming byte counter, 413 on breach, chunked-safe | `app.py:153-234` | VERIFIED |
| Secret leakage sweep | API responses scanned for KEK, root keys, credential secrets | `tests/security/test_security_controls.py:378-403` | VERIFIED |

---

## 6. Security Headers

| Header | Value | File |
|--------|-------|------|
| `X-Content-Type-Options` | `nosniff` | `app.py:249` |
| `X-Frame-Options` | `DENY` | `app.py:250` |
| `Referrer-Policy` | `no-referrer` | `app.py:251` |
| `Cross-Origin-Opener-Policy` | `same-origin` | `app.py:252` |
| `Cross-Origin-Resource-Policy` | `same-origin` | `app.py:253` |
| `Content-Security-Policy` | `default-src 'none'; script-src 'self'; ... frame-ancestors 'none'` | `app.py:256-259` |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` (production only) | `app.py:260-263` |
| `Permissions-Policy` | `geolocation=(), microphone=(), camera=()` | `app.py:264` |
| `X-Request-ID` | Correlation ID on every response | `app.py:287` |

---

## 7. Findings

| ID | Severity | Title | File:Line | Evidence State | Recommendation |
|-----|----------|-------|-----------|----------------|----------------|
| ZB-SEC-01 | P0 / Critical | Credential rotation not atomic (no transaction, no row lock) | `services/credentials.py:188-217` | **RESOLVED** — wrapped in `self._db.transaction()` with `SELECT ... FOR UPDATE` |
| ZB-SEC-02 | P0 / Critical | No MFA/TOTP or recovery codes | Documented gap | Open — implement TOTP if phishing resistance is required |
| ZB-SEC-03 | High | Per-process rate limiting only | `api/deps.py:203-235` | Open — add Redis-backed shared limiter for multi-replica deployments |
| ZB-SEC-04 | High | No OpenTelemetry / Prometheus metrics | `METRICS_ENABLED` config, no endpoint | Open — add `/metrics` endpoint and OTel tracing |
| ZB-SEC-05 | High | Quota bypass via presigned upload URLs | `routes_buckets.py:540-583` | Open — enforce quota headroom before issuing presigned PUT |
| ZB-SEC-06 | Medium | Bucket deletion TOCTOU window | `services/buckets.py:147-175` | Open — window reduced by adapter re-check; engine-level conditional delete not available |
| ZB-SC-01 | Medium | CI workflow over-privileged (write permissions + secrets for read-only validation) | `.github/workflows/auto-fix-open-prs.yml` | **RESOLVED** — reduced to `contents: read`; write-capable secrets removed |
| ZB-ARC-01 / ZB-ARC-02 | Low | Service-layer coupling (private attribute access; local config import) | `services/usage.py`, `services/buckets.py` | **RESOLVED** — public `capabilities` property; injected `max_buckets_per_user`; covered by `tests/unit/test_service_coupling.py` |

---

## 8. Tenant Isolation Proof

**Test: `test_user_cannot_touch_another_users_bucket`** (`tests/integration/test_api.py:447-487`)

Two users are created (victim, intruder). The victim creates a bucket. The intruder:
1. GET `/api/v1/buckets/{victim_bucket}` → **403**
2. GET `/api/v1/buckets` → victim's bucket not listed
3. DELETE `/api/v1/buckets/{victim_bucket}` → **403**

**Result: VERIFIED — tenant isolation is enforced at the application layer.**

Note: Isolation is application-level (owner_id checks in service layer), not database-level (no row-level security). A SQL injection or severe application bug could bypass this. All queries are parameterised, reducing injection risk to negligible.

---

## 9. Audit Trail Integrity

| Control | Implementation | File | State |
|---------|---------------|------|-------|
| Append-only events | `audit_events` table with BIGSERIAL PK | `migrations/0001_init.sql:206-220` | VERIFIED |
| Secret redaction | `FORBIDDEN_DETAIL_KEYS` + recursive `_redact()` | `services/audit.py:16-33`, `logging_setup.py:19-76` | VERIFIED |
| No credentials in audit | Tested: secret access keys never appear in audit events | `tests/integration/test_api.py:507-512` | VERIFIED |
| Request ID correlation | `X-Request-ID` header + structlog contextvars | `app.py:268-288` | VERIFIED |
| IP address capture | Validated INET, discarded if unparseable | `api/deps.py:40-64`, `services/audit.py:174-183` | VERIFIED |
| Safety gate denials audited | Production change blocks and dry runs recorded | `services/safety.py:55-65`, `97-116` | VERIFIED |
