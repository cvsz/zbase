# Remediation Report — S3Vault

**Repository:** cvsz/zbase
**Baseline SHA:** `d70a681cc9f88884f644d5c43b0a259b62d78635`
**Branch:** `feat/zbase-enterprise-hardening`
**Remediation date:** Sun Nov 08 2026

---

## 1. Changes Summary

| # | File | Change | Finding | Severity |
|---|------|--------|---------|----------|
| 1 | `apps/api/s3vault/services/credentials.py` | Rewrote `rotate()` to use a single database transaction with `SELECT ... FOR UPDATE` row lock on the original credential. All three mutations (insert replacement, revoke original, set `rotated_from`) now commit or roll back atomically. | ZB-001 | P0 / Critical |
| 2 | `tests/integration/test_api.py` | Added `test_credential_rotation_chain_maintains_rotated_from` — verifies multi-rotation chain integrity. | ZB-001 | P0 / Critical |
| 3 | `tests/integration/test_api.py` | Added `test_credential_rotation_of_revoked_credential_fails` — verifies revoked credential cannot be re-rotated. | ZB-001 | P0 / Critical |
| 4 | `tests/integration/test_api.py` | Added `test_concurrent_credential_rotation_is_safe` — verifies `SELECT FOR UPDATE` serializes concurrent rotations; exactly one succeeds, the other gets ConflictError. | ZB-001, ZB-010 | P0 / Critical |

---

## 2. Detailed Change: Credential Rotation Atomicity

### Before (defective)

```python
def rotate(self, credential_id, *, user_id, created_by=None):
    original = self.get(credential_id)           # auto-commits (separate transaction)
    ...
    replacement = self.create(...)                # auto-commits (separate transaction)
    self._db.execute("UPDATE ... revoked_at = now()")   # auto-commits (separate transaction)
    self._db.execute("UPDATE ... rotated_from = %s")    # auto-commits (separate transaction)
    return replacement
```

**Failure modes:**
- Crash after `create()`, before revoke → both credentials active (secret still valid)
- Crash after revoke, before `rotated_from` → audit relationship lost
- Concurrent rotations → race condition, no serialization, two replacements possible

### After (fixed)

```python
def rotate(self, credential_id, *, user_id, created_by=None):
    with self._db.transaction() as conn:
        # SELECT ... FOR UPDATE locks the row for the transaction duration
        row = conn.execute("SELECT ... FROM credentials WHERE id = %s FOR UPDATE", ...).fetchone()
        if row is None: raise NotFoundError(...)
        if row["revoked_at"] is not None: raise ConflictError(...)
        
        # All inserts/updates within the same transaction
        conn.execute("INSERT INTO credentials ... rotated_from = %s", (..., credential_id))
        conn.execute("UPDATE credentials SET revoked_at = now() WHERE id = %s", (credential_id,))
        # Transaction commits on successful exit; rolls back on exception
    
    return { ... replacement details ... }
```

**Safety properties guaranteed:**
1. **Atomicity:** Either all three mutations (insert replacement, revoke original, set rotated_from) commit, or none do. No crash can leave both credentials active.
2. **Audit integrity:** `rotated_from` is always set on the replacement within the same transaction as the revocation. The relationship is never lost.
3. **Concurrency safety:** `SELECT ... FOR UPDATE` serializes concurrent rotations of the same credential. The second rotation blocks on the row lock, then finds the target already revoked and raises `ConflictError`.
4. **Expiry validation:** The replacement preserves the original's `expires_at`; if already expired, rotation is rejected.

---

## 3. New Regression Tests

| Test | What it verifies | File:Line |
|------|-----------------|-----------|
| `test_credential_rotation_chain_maintains_rotated_from` | Multiple rotations form a chain: each replacement points at its predecessor; original row has `rotated_from = NULL` | `tests/integration/test_api.py:422-445` |
| `test_credential_rotation_of_revoked_credential_fails` | Rotating an already-revoked credential returns 409 Conflict; rotating the replacement succeeds | `tests/integration/test_api.py:447-472` |
| `test_concurrent_credential_rotation_is_safe` | Two threads rotate the same credential; exactly one succeeds, the other gets ConflictError; original access key is unresolvable; exactly one active credential remains | `tests/integration/test_api.py:474-531` |

---

## 4. Verification Results

| Test | Command | Result | State |
|------|---------|--------|-------|
| All credential tests | `pytest tests/integration/test_api.py -k "credential or rotation or rotated"` | 11 passed | VERIFIED |
| Unit tests | `pytest tests/unit -q` | 90 passed | VERIFIED |
| Lint | `ruff check apps tests` | All checks passed | VERIFIED |
| Compile check | `compileall -q apps tests` | OK | VERIFIED |
| Shellcheck | `shellcheck scripts/*.sh` | Exit 0 | VERIFIED |

---

## 5. Backward Compatibility

- The `rotate()` method signature is unchanged: `(credential_id, *, user_id, created_by=None)`.
- The return value is unchanged: `{id, user_id, access_key_id, secret_access_key, permissions, bucket_scope, prefix_scope, description, expires_at}`.
- The `rotated_from` column semantics are unchanged: reads "this credential was rotated from <old id>" on the replacement row.
- All existing callers (route handler `routes_system.py:123-138`) continue to work without modification.

---

## 6. Remaining Open Items (Post-Remediation)

| Finding | Severity | Status | Reason |
|---------|----------|--------|--------|
| ZB-002 No MFA/TOTP | P0 / Critical | Open | Documented product scope gap |
| ZB-003 Per-process rate limiting | High | Open | Documented architectural trade-off |
| ZB-004 No OpenTelemetry / Prometheus | High | Open | Documented architectural trade-off |
| ZB-005 Presigned URL quota bypass | High | Open | TOCTOU inherent to presigned URLs |
| ZB-006 Bucket deletion TOCTOU | Medium | Open | Window reduced; engine-level fix not available |
| ZB-007 No garbage collection | Medium | Open | Control plane scope |
| ZB-008 No object checksum persistence | Medium | Open | Control plane scope |
| ZB-009 Private attribute coupling | Low | Open | Cosmetic; works correctly |
