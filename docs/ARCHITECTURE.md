# Architecture

S3Vault is a self-hosted S3-compatible object storage platform. This document
describes how it is put together, why, and where the boundaries are.

## Storage engine decision

**Selected: SeaweedFS 3.97**, S3 gateway mode.

An architecture decision record lives in [`docs/adr/0001-storage-engine.md`](adr/0001-storage-engine.md).

### Candidates considered

| Engine | S3 compatibility | Verdict |
|---|---|---|
| **SeaweedFS 3.97** | Measured against the live gateway; see [S3-COMPATIBILITY.md](S3-COMPATIBILITY.md) | **Selected.** Mature, permissive licence (Apache-2.0), single-node and multi-node, good multipart support. Gaps: no bucket policies, no usage API. |
| Garage | V2 S3 API is partial; SigV4 quirks documented upstream | Rejected: the S3 surface is smaller and the compatibility risk is higher for a product whose main job is S3 compatibility. |
| MinIO | Strong S3 parity | Rejected for now: the community edition's licensing and distribution terms have changed repeatedly, and the current release requires accepting AGPLv3. Re-evaluate if a permissively licensed build becomes available. |

### Licence position

SeaweedFS is Apache-2.0, which is compatible with S3Vault's own licence and
places no obligations on operators beyond attribution and notice.

### Adapter boundary

Backend-specific code lives entirely behind
[`apps/api/s3vault/storage/`](../apps/api/s3vault/storage/):

- `base.py` — the `StorageAdapter` protocol, the `Feature` enum and the
  `Capabilities` value object
- `seaweedfs.py` — the production adapter
- `memory.py` — an in-memory adapter for unit tests, which is never selected
  outside the test environment

`STORAGE_ENGINE` selects the implementation through a single factory
(`build_adapter`). No service or route imports a concrete engine.

### Capabilities are probed, never assumed

`SeaweedFSAdapter.probe()` creates a disposable bucket and exercises each
optional feature against the live engine, deleting the probe bucket afterwards.
A feature is reported as supported only if a real call succeeded.

This matters. An earlier probe treated "no CORS configured" (an error code) as
"unsupported" and reported two working features as missing. The probe now
distinguishes *not configured* from *not implemented*. See
[S3-COMPATIBILITY.md#not-configured-is-not-unsupported](S3-COMPATIBILITY.md#not-configured-is-not-supported).

## Component map

```
                    ┌───────────────────────────┐
   HTTPS            │  reverse proxy (nginx)    │  TLS, redirects, streaming
   ────────────────▶│  ports 80/443 only        │  no request buffering
                    └─────────────┬─────────────┘
                                  │
                    ┌─────────────▼─────────────┐
                    │  S3Vault API              │  FastAPI, single process
                    │  ─────────────────────    │  serves the console too
                    │  • sessions, RBAC         │
                    │  • credentials            │
                    │  • bucket + object admin  │
                    │  • quotas, audit          │  /internal network
                    └──────┬───────────┬────────┘
                           │           │
        ┌──────────────────▼──┐   ┌────▼─────────────────┐
        │ PostgreSQL 18       │   │ SeaweedFS 3.97        │
        │ application metadata│   │ object bytes + S3     │
        └─────────────────────┘   └──────────────────────┘
                    ┌───────────────────────────┐
                    │  S3Vault worker           │  usage, reconciliation,
                    │  (same image, job role)   │  session purge
                    └───────────────────────────┘
```

| Component | Technology | Responsibility |
|---|---|---|
| Storage engine | SeaweedFS 3.97 | Object bytes, durability, the S3 protocol |
| Application backend | Python 3.14, FastAPI, psycopg 3 | Identity, policy, credentials, quotas, audit |
| Console | No-build ES-module SPA, served by the API | Management UI |
| Metadata database | PostgreSQL 18 | Users, sessions, credentials, bucket ownership, quotas, snapshots, audit, jobs |
| Worker | Same image, `python apps/worker/worker.py` | Usage measurement, reconciliation, session purge |
| Deployment | Docker Compose + nginx + certbot | TLS, persistence, ordering, limits |

## Why Python

The target environment has no Node runtime, and installing one would add a
toolchain purely to satisfy a framework preference. CPython 3.14 with the
standard library plus four pinned libraries (FastAPI, psycopg, boto3,
cryptography) keeps the dependency surface small, matches the interpreter used
in tests and in the image, and makes the security review tractable.

The console is deliberately **no-build**: a static ES module and a stylesheet,
served under a strict `Content-Security-Policy` with `script-src 'self'`. This
removes the Node toolchain, the lockfile supply-chain surface, and the inline
script that a strict CSP forbids.

## Data ownership

The division of truth is deliberate and one-directional:

| Fact | Owner | S3Vault's copy |
|---|---|---|
| Object bytes, object existence, ETag | SeaweedFS | never mirrored |
| Bucket existence | SeaweedFS | advisory mirror in `buckets`, reconciled by the worker |
| Users, roles, sessions | PostgreSQL | authoritative |
| Credential secrets | PostgreSQL, AES-256-GCM envelope | authoritative |
| Bucket ownership, quotas | PostgreSQL | authoritative |
| Usage figures | derived by listing | snapshot history |

S3Vault never writes object bytes into PostgreSQL and never maintains a second
source of truth for object contents. `BucketService.reconcile()` reports drift
between engine buckets and managed metadata without changing either.

## Request path for an administrative action

1. nginx terminates TLS and streams the body through without buffering.
2. `ProxyHeadersMiddleware` trusts `X-Forwarded-*` only because
   `TRUSTED_PROXY_HOPS` declares a proxy is present.
3. The body-size middleware rejects anything over
   `REQUEST_BODY_LIMIT_BYTES` with a structured `413`, using `Content-Length`
   when present and measuring the buffered body otherwise.
4. The session cookie is hashed and looked up; expiry and idle timeout are
   checked; `last_seen_at` is refreshed.
5. For a mutating request, the CSRF token is verified against the hash stored
   with the session. This resolves the session independently, so the guard does
   not depend on dependency ordering.
6. The capability required by the route is checked against the caller's role.
7. The service applies the safety gate, the quota reservation, and the audit
   record.
8. Only then does the storage adapter call the engine.

## Authentication and authorisation

Three distinct credentials, deliberately not interchangeable:

| Credential | Purpose | Storage |
|---|---|---|
| Console session | Browser → API | SHA-256 hash of an opaque token in `sessions`; plaintext only in an `HttpOnly` cookie |
| S3 credential | Server-side signing | AES-256-GCM envelope in `credentials`, under the KEK |
| Engine root credential | API → SeaweedFS | process environment only |

Console sessions and S3 credentials are separate tables, separate lifecycles and
separate revocation paths. Engine root credentials never leave the API process
and are never returned by any endpoint; `GET /api/v1/settings` reports
`{"configured": true, "value": null}` for each secret.

Authorisation is capability-based (`security/rbac.py`), so routes depend on
`Capability.OBJECT_WRITE` rather than on a role name, and only `admin` holds
`ENGINE_ADMIN`.

## Quotas

Quotas are **application-enforced admission control**, not an engine ceiling.

The engine exposes no quota API, so:

1. Before an upload, `QuotaService.reserve()` records a `held` reservation.
2. On success the reservation is `committed`; on failure it is `released`.
3. Reservations that are never settled expire after an hour, so a crashed
   worker cannot leak capacity permanently.
4. The worker periodically re-measures usage by listing, writing snapshots, and
   expiring stale reservations.

Because usage is derived from listings rather than an engine counter, two
concurrent uploads are serialised by reservations rather than by the engine.
This is strong, but it is not a hard storage limit: code that writes to the
engine directly bypasses it. The engine is not reachable from anywhere but the
API, which is what makes this acceptable.

## Safety gates

Implemented in `services/safety.py` and covered by
`tests/security/test_security.py`:

| Setting | Effect |
|---|---|
| `S3VAULT_REQUIRE_DRY_RUN=1` | Mutating endpoints compute a plan and return it; the engine is never called. Verified by asserting the bucket still exists afterwards. |
| `S3VAULT_ALLOW_PRODUCTION_CHANGES=1` | Required before any mutation in `production`. Absent means mutations fail closed with `423`. |
| `S3VAULT_REQUIRE_BACKUP=1` | Destructive operations require a verified backup newer than 26 hours. |

Missing or conflicting settings disable behaviour rather than enabling it.
`S3VAULT_REQUIRE_DRY_RUN=1` together with
`S3VAULT_ALLOW_PRODUCTION_CHANGES=1` is a startup error.

## Metadata schema

Sixteen tables in `migrations/0001_init.sql`. The interesting choices:

- `credentials.secret_ciphertext` is `BYTEA`; there is no plaintext column.
- `sessions` stores `token_hash` and `csrf_token_hash`, never the tokens.
- `password_reset_tokens` stores only a hash.
- `quota_reservations` exists specifically so concurrent uploads cannot both
  pass a headroom check.
- `audit_events` has a JSONB `detail` column that the service sanitises on
  write: forbidden keys are dropped outright, and the remainder is passed
  through the shared redactor.
- `application_settings` holds non-sensitive configuration only.

Migrations are applied forward-only inside a transaction together with their
checksum row, so a failure leaves the database on the last complete version. A
migration whose checksum changes after being applied is refused.

## Known architectural limitations

1. **Single-process rate limiting.** In-memory, per replica. Fine for one API
   process; N replicas enforce N windows.
2. **Usage measurement is O(objects).** Listing every object is expensive at
   scale; the engine exposes no cheaper counter.
3. **The API is a signing proxy, not an S3 gateway.** See
   [S3-COMPATIBILITY.md#known-limitations](S3-COMPATIBILITY.md#known-limitations).
4. **No MFA, no OIDC SSO.**
5. **Single-node engine.** No replication or erasure coding is configured, so
   this is one disk's worth of durability. See
   [DISASTER-RECOVERY.md](DISASTER-RECOVERY.md).