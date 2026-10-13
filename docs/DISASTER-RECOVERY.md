# Disaster recovery

What actually happens when things break, and what you will have lost.

## Honest durability statement

**The default deployment is single-node SeaweedFS on one disk.** Durability is
whatever that disk provides. Running more containers does not create
redundancy: `docker-compose.yml` runs exactly one engine and one volume.

Explicitly, these are **not** true of this configuration and must not be
claimed:

- Multiple containers do not provide high availability.
- RAID does not replace independent backups.
- Snapshots do not replace backups.
- A replica on the same host protects against a crashed process, not a lost
  disk.

## Failure scenarios

| Failure | Impact | Recovery | Data loss |
|---|---|---|---|
| API process crash | Console unavailable; **engine keeps serving data** | Restart; `restart: unless-stopped` | none |
| Worker crash | Usage figures stale; reservations linger | Restart; reservations expire after 1 h | none |
| PostgreSQL corruption | Console, credentials, audit, quotas lost | Restore `database.dump` | object data intact if engine volume is fine |
| Engine volume loss | **All objects lost** | Restore `engine.tar` (quiesced) | bounded by last backup |
| Both lost | Everything | Restore both | bounded by last backup |
| Whole host lost | Everything | Restore on new host from off-site backup | bounded by last backup + RPO |
| KEK lost only | **All S3 credentials unusable**; console accounts fine | Re-issue credentials | credential availability, not data |
| Disk full | Uploads fail; writes rejected | Free space or extend | none, but failed writes |
| Bad configuration | Process refuses to start | Fix `.env`, restart | none |

The asymmetry matters: **the database is cheap to back up and expensive to lose
in function; the engine volume is expensive to back up and holds the data.**

## Recovery objectives

| | Value | How |
|---|---|---|
| **RPO** | ≤ 24 h (daily schedule) | Backup frequency |
| **RTO** | ~15 min metadata, ~1 h with object data | Restore steps |

## Recovery runbook

### 1. Confirm the backup before touching anything

```bash
scripts/verify-backup.sh backups/<timestamp>
```

If verification fails, stop. A restore from a corrupt backup is worse than no
restore.

### 2. Rehearse first

```bash
scripts/restore.sh backups/<timestamp>
```

This runs in isolation and proves the backup is usable. It never touches
production.

### 3. Decide scope

| Situation | Restore |
|---|---|
| Database corrupt, engine fine | Database only. Objects untouched. |
| Engine volume lost, database fine | Engine only. Credentials still valid. |
| Both lost, or new host | Both, plus new host bootstrap. |
| Unknown | Both. Verify what survives. |

### 4. Restore — see [BACKUP-RESTORE.md](BACKUP-RESTORE.md)

Stop writes first (`docker compose stop api worker`) so the engine archive is
consistent.

### 5. Verify

```bash
scripts/verify-install.sh https://s3.example.com
```

Then check data, not just the service:

```bash
export AWS_ACCESS_KEY_ID=<key> AWS_SECRET_ACCESS_KEY=<secret>
aws --endpoint-url <presigned-endpoint> s3 ls
```

Compare object count against the last usage snapshot:

```sql
SELECT measured_at, object_count, storage_bytes FROM usage_snapshots
 WHERE scope = 'global' ORDER BY measured_at DESC LIMIT 1;
```

### 6. Reconstruct what cannot be restored

- **Credentials:** if the KEK changed or was lost, old secrets are
  unrecoverable. Re-issue and redeploy.
- **Console accounts:** if the database is gone and the backup is older than
  your account changes, recreate them.
- **Audit history:** only as far back as the last backup.

## New host from scratch

```bash
# 1. Install the prerequisites (see INSTALLATION.md).

# 2. Restore configuration, including the KEK from your separate key store.
git clone <repo> /opt/s3vault && cd /opt/s3vault
cp backups/<ts>/env.snapshot .env
# Insert the correct KEK_BASE64 — from your key store, not from the backup.

# 3. Recreate the volumes.
docker compose up -d postgres seaweedfs

# 4. Restore data.
docker compose exec -T postgres pg_restore -U s3vault -d s3vault \
  --clean --if-exists < backups/<ts>/database.dump
docker compose stop seaweedfs
docker run --rm -v s3vault_swdata:/data -v "$PWD/backups/<ts>/engine.tar:/b.tar:ro" \
  alpine:3.20 sh -c 'rm -rf /data/* && tar -xf /b.tar -C /data'
docker compose start seaweedfs

# 5. Start and verify.
docker compose up -d
scripts/verify-install.sh https://s3.example.com
```

## Reducing risk

Ordered by value for effort:

1. **Take backups on a schedule and verify them.** An unverified backup is a
   hypothesis. Everything else here matters less.
2. **Copy backups off the host.** A backup on the same disk protects against
   nothing that matters.
3. **Store the KEK separately.** Losing it loses every S3 credential
   regardless of how good the backup is.
4. **Monitor disk space on the engine volume.** Disk-full failures are sudden
   and confusing.
5. **Quiesce writes during the engine archive** for a consistent snapshot.
6. **Then, if you need more than the RPO**, add an engine that supports
   replication or erasure coding and adapt the adapter.

## Testing this document

The restore drill is the only way to know these steps work:

```bash
make restore-drill
```

Run it monthly. If it has never passed on your infrastructure, assume it will
not.