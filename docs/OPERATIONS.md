# Operations

Day-two operations for a running S3Vault.

## Health endpoints

| Endpoint | Meaning | Auth |
|---|---|---|
| `GET /healthz` | The process is up. No dependency checks. | none |
| `GET /readyz` | Dependencies reachable. `503` when not ready. | none |
| `GET /api/v1/health` | Admin API liveness. | none |

Neither reveals secrets, object contents, internal hostnames or configuration.
`/readyz` returns only booleans:

```json
{"status": "ready", "checks": {"database": true, "storage": true}}
```

Use `/healthz` for liveness probes and `/readyz` for readiness. Do not restart
the process on a failed `/readyz` — that turns a dependency outage into a crash
loop.

## Daily

```bash
scripts/verify-install.sh https://s3.example.com   # TLS, readiness, isolation
docker compose ps                                  # all services healthy
df -h /var/lib/docker                             # engine volume has room
```

## Weekly

```bash
scripts/backup.sh
scripts/verify-backup.sh "$(ls -1dt backups/*/ | head -1)"
```

## Monthly

```bash
scripts/restore.sh "$(ls -1dt backups/*/ | head -1)"   # isolated drill
docker compose pull && docker image prune              # see UPGRADES.md first
```

## Capacity

The engine exposes no usage API, so S3Vault measures by listing objects. The
worker writes snapshots every 15 minutes; the console's Overview shows the
latest with its source labelled.

```sql
-- Most recent global snapshot
SELECT measured_at, storage_bytes, object_count
  FROM usage_snapshots
 WHERE scope = 'global'
 ORDER BY measured_at DESC LIMIT 1;

-- Growth over the last week
SELECT date_trunc('day', measured_at) AS day,
       max(storage_bytes) AS bytes
  FROM usage_snapshots
 WHERE scope = 'global' AND measured_at > now() - interval '7 days'
 GROUP BY 1 ORDER BY 1;
```

Alert on the engine volume, not just the database. Docker's data root is
usually the constraint:

```bash
docker system df
df -h /var/lib/docker
```

## Logs

Structured JSON to stderr, collected by Docker. Every record passes a redactor
that masks JWTs, signed query strings, `Authorization` headers and
credential-shaped keys.

```bash
docker compose logs -f api
docker compose logs --since 1h api | jq -r 'select(.event=="http.request") | "\(.status) \(.method) \(.path)"'
```

Audit events are separate from application logs and live in PostgreSQL:

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  'https://s3.example.com/api/v1/audit-events?limit=50' | jq '.events[] | {occurred_at, action, outcome}'

# NDJSON export for offline retention
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  https://s3.example.com/api/v1/audit-events/export > audit-$(date +%F).jsonl
```

Audit access is restricted to `AUDIT_READ` (admin and auditor).

## Background worker

The worker runs usage measurement, quota reservation expiry and session purge.
Jobs are claimed with `FOR UPDATE SKIP LOCKED`, so replicas do not collide.

```bash
docker compose logs --since 1h worker | jq -r 'select(.event=="worker.job_failed")'
```

Healthy output looks like:

```json
{"event": "worker.usage_snapshot", "buckets": 4, "bytes": 10485760, "objects": 12}
{"event": "worker.housekeeping", "expired_reservations": 0, "purged_sessions": 3}
```

A growing `background_jobs.state = 'failed'` count means a handler is erroring;
inspect `last_error`:

```sql
SELECT job_type, attempts, last_error FROM background_jobs
 WHERE state = 'failed' ORDER BY id DESC LIMIT 10;
```

## Safety gates in practice {#safety-gates}

The gates are enforced in code, not documentation.

| Setting | Effect |
|---|---|
| `S3VAULT_REQUIRE_DRY_RUN=1` | Every mutating endpoint returns a plan and touches nothing. Useful for rehearsing a change. |
| `S3VAULT_ALLOW_PRODUCTION_CHANGES=1` | Required for any mutation in production. Without it, mutations fail with `423`. |
| `S3VAULT_REQUIRE_BACKUP=1` | Destructive operations need a verified backup under 26 hours old. |

A rehearsed deletion returns its plan:

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -X DELETE \
  -H 'Content-Type: application/json' \
  -d '{"confirm_name":"documents","dry_run":true}' \
  https://s3.example.com/api/v1/buckets/documents | jq
```

```json
{"dry_run": true, "plan": {"bucket": "documents", "existed": true,
 "non_empty": true, "forced": false, "dry_run": true}}
```

`tests/security/test_security.py` proves dry run does not reach the backend by
asserting the bucket still exists in the engine afterwards.

## Firewall

Only 80/tcp and 443/tcp are public. PostgreSQL (5432) and the engine (8333,
9333) are not published by `docker-compose.yml` and sit on an internal Docker
network. `make test-stack.sh status` and `docker compose ps` will show that no
engine or database port is bound to the host.

## Routine tasks

### Rotate an S3 access key

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -X POST \
  https://s3.example.com/api/v1/credentials/<id>/rotate | jq
```

The replacement secret is shown once. Deploy the new key to consumers, then
confirm the old one is revoked. Rotation preserves scope and permissions.

### Add a user

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"username":"alice","email":"alice@example.com","password":"<strong>","role":"operator"}' \
  https://s3.example.com/api/v1/users
```

Roles: `admin`, `owner`, `operator`, `auditor`. `auditor` is strictly read-only.

### Deactivate a user

`PATCH /api/v1/users/{id}` with `{"is_active": false}`. This revokes their
sessions immediately.

### Grant a scoped key

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"permissions":["read"],"bucket_scope":["public-site"],"prefix_scope":"assets/"}' \
  https://s3.example.com/api/v1/credentials | jq
```

### Verify engine capabilities are unchanged

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  https://s3.example.com/api/v1/settings/engine-capabilities | jq
```

Run after any engine upgrade. A feature disappearing should be noticed here,
not by a client.

## Shutdown

```bash
docker compose stop api worker    # graceful, 30s grace period
docker compose down               # remove containers, keep volumes
```

`SIGTERM` triggers an orderly shutdown: in-flight requests finish, the
database pool closes, the worker stops claiming jobs. Do not use `kill -9` on
the API; if you must, check for held quota reservations afterwards, which
expire automatically after an hour.

## Not implemented

Stated so nobody assumes otherwise:

- No Prometheus `/metrics` endpoint, despite `METRICS_ENABLED`. Health,
  structured logs and audit records are the observability surface.
- No Grafana dashboard.
- No alerting on backup failure or disk pressure. These are operator tasks.
- No log rotation beyond Docker's `json-file` limits.