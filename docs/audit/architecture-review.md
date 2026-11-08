# Architecture Review — S3Vault

**Repository:** cvsz/zbase
**Baseline SHA:** `d70a681cc9f88884f644d5c43b0a259b62d78635`
**Reviewer date:** Sun Nov 08 2026

---

## 1. Architectural Style

S3Vault follows **Clean Architecture** with a clear separation of concerns:

```
┌─────────────────────────────────────────────────────────┐
│  API Layer (FastAPI)                                    │
│  routes_auth.py, routes_buckets.py, routes_system.py    │
├─────────────────────────────────────────────────────────┤
│  Service Layer                                          │
│  users, credentials, buckets, quotas, usage, audit, safety│
├─────────────────────────────────────────────────────────┤
│  Security Layer                                         │
│  envelope (AES-256-GCM), passwords (scrypt/argon2id),   │
│  rbac (capability-based), tokens (opaque, hashed)       │
├─────────────────────────────────────────────────────────┤
│  Storage Abstraction (Protocol)                         │
│  MemoryAdapter (test), SeaweedFSAdapter (production)    │
├─────────────────────────────────────────────────────────┤
│  Database Layer (psycopg3 connection pool)              │
│  Migrator (numbered SQL migrations with checksums)      │
└─────────────────────────────────────────────────────────┘
```

The control plane / data plane separation is well-defined: SeaweedFS owns object bytes, durability, and the S3 wire protocol; S3Vault owns identity, policy, credentials, quotas, and audit.

---

## 2. SOLID Assessment

| Principle | Assessment | Evidence |
|-----------|-----------|----------|
| **Single Responsibility** | Pass | Each service has one concern (users, credentials, buckets, quotas, audit, safety) |
| **Open/Closed** | Pass | Storage adapter protocol allows new engines without modifying application code |
| **Liskov Substitution** | Pass | MemoryAdapter and SeaweedFSAdapter both implement the same StorageAdapter protocol |
| **Interface Segregation** | Pass | Fine-grained capabilities (rbac.Capability) instead of coarse roles |
| **Dependency Inversion** | Pass | Services depend on abstractions (Database, StorageAdapter protocol), not concretions |

---

## 3. Module Boundaries and Coupling

**Strong boundaries:**
- `storage/__init__.py` factory (`build_adapter`) hides concrete engine choice from the application layer
- `SecurityConfig` and `AppConfig` are immutable frozen dataclasses
- Services receive dependencies via constructor injection

**Coupling concerns (Medium):**

1. **`services/usage.py:127`** accesses `self._adapter._capabilities` — a private attribute of the concrete adapter. This breaks the abstraction boundary. The `StorageAdapter` Protocol does not expose capabilities publicly; callers should use `adapter.probe()` instead, though that re-probes the engine.

   **Recommendation:** Add a `capabilities` property to the `StorageAdapter` Protocol or pass `Capabilities` into `UsageService` at construction time.

2. **`services/buckets.py:122`** imports `load_config` locally to avoid an import cycle. This works but is a code smell indicating a potential cycle between `buckets` and `config`.

   **Recommendation:** Pass `max_buckets_per_user` into `BucketService` at construction time instead of re-loading config.

3. **`routes_buckets.py:560`** calls both `adapter.head_object()` and `bucket_service.get()` for presigned upload validation. The logic is correct but could be clearer.

---

## 4. Dependency Graph

```
main.py
  ├── app.py (create_app)
  │   ├── config.py (load_config, fail-closed validation)
  │   ├── db.py (Database, Migrator, ConnectionPool)
  │   ├── logging_setup.py (structlog, redaction)
  │   ├── security/
  │   │   ├── envelope.py (AES-256-GCM)
  │   │   ├── passwords.py (scrypt/argon2id)
  │   │   ├── rbac.py (capabilities)
  │   │   └── tokens.py (opaque tokens, key validation)
  │   ├── services/
  │   │   ├── users.py (accounts, sessions, password reset)
  │   │   ├── credentials.py (S3 credential lifecycle, envelope encryption)
  │   │   ├── buckets.py (bucket metadata, ownership)
  │   │   ├── quotas.py (reservation-based quota enforcement)
  │   │   ├── usage.py (engine listing measurement)
  │   │   ├── audit.py (append-only audit events, redaction)
  │   │   └── safety.py (dry-run, production guard, backup freshness)
  │   ├── storage/
  │   │   ├── base.py (StorageAdapter Protocol, Capabilities)
  │   │   ├── memory.py (test adapter)
  │   │   └── seaweedfs.py (boto3/SigV4 adapter)
  │   └── api/
  │       ├── deps.py (auth, CSRF, rate limiting, capabilities)
  │       ├── routes_auth.py (login, users, password reset)
  │       ├── routes_buckets.py (buckets, objects, presigned, multipart)
  │       └── routes_system.py (credentials, audit, settings)
  └── worker.py (usage aggregation, housekeeping, background jobs)
```

No circular dependencies detected beyond the local `load_config` import in `buckets.py`.

---

## 5. Maintainability and Technical Debt

**Strengths:**
- Typed code with `from __future__ import annotations` throughout
- Stable error codes (`errors.py`) that are part of the public API contract
- Comprehensive secret redaction in logging and audit
- Fail-closed configuration with no weak-default fallbacks
- Migration checksums prevent silent migration drift

**Technical debt (low/medium):**
1. `usage.py:127` private attribute access (noted above)
2. Local `load_config` import in `buckets.py:122`
3. No shared/distributed rate limiter (per-process only — documented limitation)
4. KEK rotation requires re-issuing all credentials (no re-encryption tool — documented limitation)

---

## 6. Findings

| ID | Severity | Title | File:Line | Recommendation |
|-----|----------|-------|-----------|----------------|
| ZB-ARC-01 | Medium | Private attribute coupling in usage service | `services/usage.py:127` | Expose capabilities via public API or inject at construction |
| ZB-ARC-02 | Low | Local config import to avoid cycle | `services/buckets.py:122` | Inject `max_buckets_per_user` into `BucketService` constructor |
