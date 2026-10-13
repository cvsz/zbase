# Security

Security is a release requirement here, not a follow-on. Every control below is
implemented in code and covered by a test; where a control is absent, it is
listed as a gap rather than implied.

Test evidence: `tests/security/` (35 tests), plus redaction and KDF unit tests.

## Threat model

| Threat | Control | Test |
|---|---|---|
| Credential stuffing | Login rate limit per client IP; account lock after 8 failures for 15 minutes | `test_login_rate_limit_is_enforced`, `test_repeated_failures_lock_the_account` |
| Session theft | `HttpOnly`, `Secure`, `SameSite` session cookie; opaque token stored hashed server-side | `test_session_cookie_is_not_readable_by_script` |
| CSRF | Double-submit token required on every mutating request, resolved independently of route ordering | `test_every_mutating_route_requires_csrf` |
| Cross-user access | Capability checks plus per-bucket ownership assertions | `test_non_admin_cannot_read_other_users`, `test_user_cannot_touch_another_users_bucket` |
| Privilege escalation | Least-privilege roles; `auditor` is read-only | `test_auditor_cannot_mutate` |
| Path traversal | Object keys validated: no absolute paths, no `.`/`..`/empty segments, no control characters, no percent-encoded separators | `test_object_keys_cannot_escape_the_namespace` |
| SQL injection | Parameterised queries everywhere; SQL identifiers validated against `^[a-z_][a-z0-9_]*$` | `test_sql_identifiers_are_validated`, `test_query_parameters_are_parameterised` |
| XSS in CORS config | Origin must be `*` or an absolute `http(s)` URL | `test_cors_origin_injection_is_rejected` |
| Oversized payloads | `Content-Length` and buffered-length guards return `413` | `test_oversized_body_is_rejected` |
| Secret leakage in logs | Every log record passes a redactor masking JWTs, signed query strings and `Authorization` | `test_logs_redact_credentials` |
| Secret leakage in API | No endpoint returns a secret; sweep test hunts all known secrets across every route | `test_no_secret_appears_in_any_api_response` |
| Secret leakage in audit | Forbidden keys dropped on write | `test_audit_detail_drops_forbidden_keys` |
| Presigned URL leakage | URLs never logged or audited; `Referrer-Policy: no-referrer`; signatures redacted | `test_presigned_download_url_is_issued_and_not_logged` |
| Expired credentials | Expiry checked on every resolution | `test_expired_credential_cannot_be_resolved` |
| Accidental deletion | Emptiness check, exact-name confirmation, safety gates | `test_delete_requires_name_confirmation` |
| Client IP spoofing | `X-Forwarded-For` honoured only when `TRUSTED_PROXY_HOPS` declares a proxy | `client_ip()` |
| Error disclosure | Typed errors; unexpected errors return a generic message | `test_error_responses_do_not_leak_internals` |

## Password hashing

The stored hash carries an algorithm identifier, so credentials can be upgraded
transparently on the next successful sign-in.

**Default: scrypt** from the standard library (memory-hard, via the platform's
OpenSSL), with `n=2^15, r=8, p=1` and a 16-byte random salt.

**Argon2id** is used automatically whenever `argon2-cffi` is importable and
`S3VAULT_DISABLE_ARGON2` is not set.

### Why scrypt by default

The deployment target is CPython 3.14, for which no prebuilt Argon2 wheel is
published, and the runtime image has no Rust toolchain. Shipping Argon2id as
the default would require either compiling from source at image build time or
pinning an older interpreter — and in the latter case the tested interpreter
would differ from the shipped one. scrypt is a recognised memory-hard KDF and
needs no extra dependency.

To enable Argon2id, install `argon2-cffi` in the image. Existing scrypt hashes
upgrade on next sign-in; no forced reset.

`S3VAULT_DISABLE_ARGON2=1` forces scrypt even when the module is importable,
which is what the test suite does.

## Credential secret storage

S3 secret access keys are never stored in plaintext.

```
KEK (32 bytes, from KEK_BASE64 or KEK_FILE — outside the database)
 └── wraps a fresh per-credential DEK (AES-256-GCM, own nonce)
      └── encrypts the secret (AES-256-GCM, separate nonce)
           AAD = "<credential id>|<access key id>"
```

Both operations use additional authenticated data binding the ciphertext to
the credential's identity, so a row transplanted into another credential row
fails authentication. Tampering with either field fails its GCM tag.

Layout:

```
b"S3V1" | nonce_dek(12) | wrapped_dek(48) | nonce_data(12) | ciphertext+tag
```

Distinct nonces are used for the two operations; reusing one nonce under the
KEK would be catastrophic for GCM.

Tests: `tests/unit/test_security.py::TestEnvelopeEncryption` — round trip,
wrong KEK, row transplant, tampering, truncation, unknown format.

### KEK rotation {#kek-rotation}

Rotating the KEK makes every existing credential secret undecryptable. Rotation
therefore requires re-issuing credentials: create new ones, deploy consumers,
revoke the old. There is no re-encryption tool in this release.

Back up the KEK separately from the database. A backup that contains both is a
plaintext copy of every credential, which is why `scripts/backup.sh` explicitly
does not include it.

## Password reset

Reset tokens are random, stored hashed, single-use, and expire in one hour. A
successful reset revokes every existing session.

The request endpoint returns an identical response whether or not the address
is registered, so it cannot enumerate accounts.

**Gap:** there is no mail transport. In `development` the token is returned in
the response to make the flow testable. In any other environment no token is
returned, which means **password reset cannot complete without an out-of-band
delivery mechanism**. Treat self-service reset as unavailable until one is
added; administrative reset is the supported path.

## Sessions

| Property | Value |
|---|---|
| Token | 256-bit opaque, URL-safe |
| Server storage | SHA-256 hash only |
| Absolute lifetime | `SESSION_TTL_S`, default 3600 |
| Idle timeout | `SESSION_IDLE_TTL_S`, default 900 |
| Cookie | `HttpOnly`, `Secure` (production), `SameSite=Lax` |
| CSRF | Separate readable cookie, verified server-side against a stored hash |

Idle timeout is enforced on every request, so a stolen session expires quickly
even within its absolute lifetime. Deactivating a user revokes their sessions
immediately.

## Rate limiting

In-memory sliding window, per client IP, per route class. Login uses
`LOGIN_RATE_LIMIT_PER_MIN` (default 5). **Limitation:** the limiter is
per-process, so N replicas enforce N windows. This is a deliberate trade to
avoid adding Redis to the deployment. A shared limiter is required before
horizontal scaling.

Account lockout is separate and durable: 8 consecutive failures locks the
account for 15 minutes.

## Transport

TLS terminates at nginx. `Strict-Transport-Security` is set on the proxy and by
the application in production. The application also sets
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer`, a strict CSP with `frame-ancestors 'none'`,
`Cross-Origin-Opener-Policy` and `Permissions-Policy`.

The CSP is `default-src 'none'` with `script-src 'self'`, which is why the
console ships no inline script.

## Firewall policy {#firewall}

Only the reverse proxy publishes ports. Everything else is on an internal
Docker network with `internal: true`.

| Port | Exposure | Notes |
|---|---|---|
| 80 | Public | ACME challenge, then 308 to HTTPS |
| 443 | Public | The only application endpoint |
| 8333 (SeaweedFS S3) | Internal only | Never published; the engine's admin UI (9333) is not exposed at all |
| 5432 (PostgreSQL) | Internal only | Not published |

On a bare-metal host, allow 80/443 (and 22 from a restricted range) and nothing
else. `docker-compose.yml` publishes no engine or database ports, so a
misconfigured host firewall cannot expose them.

## Trusted proxies

`X-Forwarded-For` is honoured **only** when `TRUSTED_PROXY_HOPS >= 1`. With no
proxy declared, the peer address is used and a client-supplied header is
ignored. Production configuration validation requires at least one trusted hop.

Set this to the real number of proxies in front of the API. An incorrect value
lets a client forge its apparent address, which weakens rate limiting and audit
attribution.

## Operational safety gates

| Gate | Behaviour | Test |
|---|---|---|
| `S3VAULT_REQUIRE_DRY_RUN=1` | Mutations return a plan; the engine is not called | `test_dry_run_does_not_reach_the_backend` |
| `S3VAULT_ALLOW_PRODUCTION_CHANGES=1` | Required for any production mutation; otherwise `423` | `test_production_mutation_guard_blocks_deletes` |
| `S3VAULT_REQUIRE_BACKUP=1` | Destructive operations need a verified backup under 26 hours old | covered by `SafetyGuard.assert_backup_fresh` |
| Conflicting settings | Startup error | `test_dry_run_and_production_changes_conflict` |

Missing or invalid settings disable destructive behaviour. They never enable it.

## Known gaps

Stated plainly rather than implied:

1. **No MFA.** Called for in the specification; not implemented.
2. **No OIDC/OAuth SSO.** Not implemented.
3. **Per-process rate limiting.** Does not aggregate across replicas.
4. **No mail transport**, so self-service password reset cannot complete
   outside `development`.
5. **No dependency or container scanning wired into the release gate.** Images
   are digest-pinned, but scanning is not automated here.
6. **The console is a no-build SPA**, so it receives no XSS review from a
   framework's templating guarantees. It builds DOM nodes programmatically and
   never assigns untrusted values through `innerHTML`.

## Reporting

Report vulnerabilities through `SECURITY.md`. Not through public issues.