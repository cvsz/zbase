# Backup and restore

Two independent things need backing up, and one thing must never be in the
same place as one of them.

| Item | Method | Where |
|---|---|---|
| Application metadata | `pg_dump` (custom format) | `scripts/backup.sh` |
| Object data | Engine volume archive | `scripts/backup.sh` |
| Deployment configuration | File copy | `scripts/backup.sh` |
| **KEK** | **Separate, off-host** | **Never in the backup** |

## Why the KEK is excluded

`scripts/backup.sh` deliberately does not back up `KEK_BASE64`. A backup
containing both the database and the KEK is a plaintext copy of every S3
credential secret. Keep the key in a password manager or a KMS, backed up on a
different schedule, and test that you can retrieve it.

## Creating a backup

```bash
scripts/backup.sh
```

Output:

```
backups/20261013T081500Z/
├── MANIFEST.txt        what was captured, and by which method
├── database.dump       pg_dump custom format + sha256
├── engine.tar          engine data (when volume-level archiving was used)
├── SHA256SUMS
├── env.snapshot        deployment configuration
├── docker-compose.yml
└── nginx.conf
```

Schedule it daily:

```bash
sudo crontab -e
# 02:17 daily — off the hour, so it does not collide with everything else
17 2 * * * cd /opt/s3vault && scripts/backup.sh >> /var/log/s3vault-backup.log 2>&1
```

## Verifying a backup

**An unverified backup is a guess.** Verify on a schedule:

```bash
make verify-backup          # newest backup
scripts/verify-backup.sh backups/20261013T081500Z
```

The check compares the recorded `database_sha256` against the file, and checks
that the engine archive is readable. Exit status is meaningful; wire it into
monitoring.

## Restore drill

Restores run against an **isolated** stack on different ports with their own
volumes. The script never touches a running deployment:

```bash
scripts/restore.sh backups/20261013T081500Z
```

It verifies the backup first, refuses to continue if it fails verification,
starts disposable containers, restores the database, checks that a plausible
number of tables arrived, and tears everything down.

Run it monthly. A restore procedure that has never been executed is a
hypothesis.

## Restoring into production

Deliberately not scripted, because it is destructive and must be an informed
decision:

```bash
# 1. Stop writes.
docker compose stop api worker

# 2. Confirm your backup verifies.
scripts/verify-backup.sh backups/20261013T081500Z

# 3. Restore the database.
docker compose exec -T postgres pg_restore -U s3vault -d s3vault \
  --clean --if-exists < backups/20261013T081500Z/database.dump

# 4. Restore engine data, with the engine stopped so nothing is writing.
docker compose stop seaweedfs
docker run --rm -v s3vault_swdata:/data -v "$PWD/backups/.../engine.tar:/backup.tar:ro" \
  alpine:3.20 sh -c 'rm -rf /data/* && tar -xf /backup.tar -C /data'
docker compose start seaweedfs

# 5. Bring the application back and verify.
docker compose up -d api worker
scripts/verify-install.sh https://s3.example.com
```

Step 3 replaces all metadata, including users and credentials. If the KEK has
changed since the backup, credential secrets will not decrypt — see
[SECURITY.md#kek-rotation](SECURITY.md#kek-rotation).

## Consistency of the engine archive

SeaweedFS has no documented snapshot API in this configuration.
`scripts/backup.sh` prefers an archive taken through `seaweedfs shell` and falls
back to a volume-level `tar`. The manifest records which method was used:

```
engine=archive-from-shell
engine=engine.tar (volume-level, consistency not guaranteed)
```

A volume-level archive taken while the engine is writing can capture a torn
state. For a consistent engine archive, quiesce writes first:

```bash
docker compose stop api worker      # no new uploads
scripts/backup.sh                   # engine is now idle
docker compose start api worker
```

## Retention

`S3VAULT_BACKUP_RETENTION_DAYS` (default 14). Pruning never removes the newest
backup, so a size- or time-driven prune cannot leave you with nothing.

Off-site copies are the operator's responsibility; S3Vault has no built-in
upload target. Copy to another host or object store:

```bash
rsync -a --delete backups/ backup@offsite:/backups/s3vault/
```

## RPO and RTO

| | Value | Driver |
|---|---|---|
| **RPO** | ≤ 24 h with the daily schedule | Backup frequency |
| **RTO** | ~15 min for metadata, ~1 h including object data | Engine archive size and restore speed |

Tighten RPO by scheduling more often. Cost is I/O against the engine during
the archive.

## Restoring keys

If the KEK is lost and no separate copy exists, **every stored S3 credential is
unrecoverable**. Users keep their console accounts; the S3 credentials must be
re-issued. This is why the key is excluded from backups and must be stored
separately.

## What is not covered

1. Engine snapshots — no snapshot API is available, so an archive is taken
   instead and may be torn if taken while writing.
2. Off-site replication — not automated.
3. Automated backup verification — `verify-backup.sh` exists but nothing
   alerts on its failure.
4. Restore drills — available as a script, not scheduled.