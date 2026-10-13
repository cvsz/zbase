# Troubleshooting

Start with the health endpoints and the logs; most problems are visible there.

## Triage

```bash
# 1. Is the process up?
curl -fsS https://s3.example.com/healthz

# 2. Are the dependencies reachable? (no secrets, booleans only)
curl -sS https://s3.example.com/readyz | jq

# 3. Container state.
docker compose ps

# 4. Recent logs.
docker compose logs --since 15m api | tail -50
```

`/readyz` returning `{"database": false}` or `{"storage": false}` tells you
which dependency to chase.

## The process will not start

```
ConfigError: insecure production configuration:
  - COOKIE_SECURE must be true in production
```

Configuration is validated at startup and fails closed. This is intended. Fix
the named variable in `.env` and restart. Full rules in
[CONFIGURATION.md](CONFIGURATION.md).

Common variants:

| Message | Fix |
|---|---|
| `KEK_BASE64 must decode to exactly 32 bytes` | `python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"` |
| `DATABASE_URL is required but was not set` | Missing from `.env`, or the variable is empty |
| `STORAGE_ROOT_ACCESS_KEY and STORAGE_ROOT_SECRET_KEY must differ` | Two different values |
| `S3VAULT_REQUIRE_DRY_RUN=true conflicts with ...` | Pick one gate |
| `S3VAULT_REQUIRE_BACKUP=true requires S3VAULT_BACKUP_DIR` | Set the directory |
| `PUBLIC_HOSTNAME is still a placeholder value` | Use the real hostname |

If a value looks correct, check for an invisible problem:

```bash
grep -n 'KEK_BASE64' .env | cat -A | head -1    # trailing whitespace/CR
```

## Database will not start

```
There appears to be PostgreSQL data in: /var/lib/postgresql/data (unused mount/volume)
```

Postgres 18 changed its data layout: the volume must be mounted at
`/var/lib/postgresql`, not `/var/lib/postgresql/data`. The container refuses to
start rather than silently ignoring your data.

```yaml
volumes:
  - s3vault_pgdata:/var/lib/postgresql     # correct
```

After changing the mount point, migrate the data:

```bash
docker compose down
docker run --rm -v s3vault_pgdata:/from -v s3vault_pgdata_new:/to alpine \
  sh -c 'cp -a /from/. /to/'
docker volume rm s3vault_pgdata
docker volume create s3vault_pgdata_new
```

### `connection timeout`

```bash
docker compose exec postgres pg_isready -U s3vault -d s3vault
docker compose logs --tail=50 postgres
```

If PostgreSQL accepts connections but the API times out, the API is on a
different network. Check `docker compose ps` shows both on the internal
network.

## Storage engine problems

### `SignatureDoesNotMatch`

The engine credentials do not match. The engine writes its identity from the
environment on first start and keeps it, so changing `STORAGE_ROOT_*` later
does not take effect.

```bash
docker compose logs seaweedfs | grep -i 'admin identity'
docker compose down -v seaweedfs   # destroys engine data: only if you accept that
```

Also check `STORAGE_REGION` matches the engine.

### Engine unreachable

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8333/   # expect 403
docker compose logs --tail=40 seaweedfs
```

SeaweedFS takes ~20 seconds to initialise its S3 gateway. Startup ordering uses
health checks, so a slow start should resolve itself.

### `EntityTooSmall` on a multipart upload

Measured engine behaviour: a multipart upload with **two or more parts** needs
every part to be at least 5 MiB. A single-part upload has no minimum.

Set `MIN_PART_BYTES=5242880` and `MAX_PART_BYTES` accordingly. Full table in
[S3-COMPATIBILITY.md#multipart-constraints](S3-COMPATIBILITY.md#multipart-constraints).

### A bucket disappeared with its objects

SeaweedFS **does** protect non-empty buckets. S3Vault does, because the engine
does not: it lists the bucket first and refuses unless `force` is set, and it
requires `confirm_name` to match exactly.

If you are calling the engine directly, you have no such protection. That is
one reason the engine is not published to the host.

## Authentication problems

| Symptom | Cause |
|---|---|
| 429 on login | Rate limited, or the account is locked after 8 failures |
| 401 immediately after login | Cookie not sent — check `COOKIE_SECURE` matches how you are connecting |
| 403 on a write | CSRF header missing, or the role lacks the capability |
| Session dies after a minute | `SESSION_IDLE_TTL_S` is short |

### "Invalid username or password" with the right password

The password hash may be unreadable. If the stored hash was made with Argon2id
and `argon2-cffi` is not installed in the image, verification raises rather than
fails open:

```
PasswordHashError: password was hashed with argon2id but argon2-cffi is not installed
```

Either install `argon2-cffi` or set `S3VAULT_DISABLE_ARGON2=1` and let hashes
upgrade on next sign-in. See [SECURITY.md](SECURITY.md#password-hashing).

### Account locked

```sql
UPDATE users SET failed_logins = 0, locked_until = NULL WHERE username = 'alice';
```

Or wait 15 minutes.

## CSRF failures

```
403 forbidden — X-CSRF-Token header is required
```

The console reads the `s3vault_csrf` cookie and echoes it in the header. For
scripted calls:

```bash
CSRF=$(grep s3vault_csrf cookies.txt | awk '{print $7}')
curl -b cookies.txt -H "X-CSRF-Token: $CSRF" ...
```

A cookie-name mismatch between proxy and app will break this. Check
`CSRF_COOKIE_NAME` and `SESSION_COOKIE_NAME` are consistent.

## 413 on upload

Bodies over `REQUEST_BODY_LIMIT_BYTES` (default 8 MiB) are refused. This is
deliberate: the API does not proxy large objects. Use multipart upload or a
presigned URL.

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"key":"big.bin","content_type":"application/octet-stream"}' \
  https://s3.example.com/api/v1/buckets/my-bucket/presigned-upload | jq -r .url
curl -X PUT --upload-file big.bin "$URL"
```

## Certificate problems

```bash
curl -vI https://s3.example.com/healthz 2>&1 | grep -i 'SSL\|certificate'
docker compose --profile tls logs certbot | tail -20
```

The proxy reads `/etc/nginx/certs/fullchain.pem` and `privkey.pem`. After
renewal, copy the live files across (see [INSTALLATION.md](INSTALLATION.md)).

## Mutations blocked

```
423 safety_gate_blocked
```

Expected in production without `S3VAULT_ALLOW_PRODUCTION_CHANGES=1`. Read the
gate table in [OPERATIONS.md#safety-gates](OPERATIONS.md#safety-gates). If
`S3VAULT_REQUIRE_DRY_RUN=1`, you get a plan instead of an action.

## Usage figures show "unavailable"

They are absent, not zero. Either no worker snapshot has been taken yet, or
the engine is unreachable. Check:

```sql
SELECT measured_at, storage_bytes, object_count FROM usage_snapshots
 WHERE scope = 'global' ORDER BY measured_at DESC LIMIT 1;
```

```bash
docker compose logs --since 1h worker | grep usage_snapshot
```

Measurement is O(objects); a large bucket takes a while to enumerate.

## Backup problems

```bash
scripts/verify-backup.sh backups/<ts>
make restore-drill
```

The drill runs in isolation and never touches production.

## Getting help

Include: `docker compose ps`, the output of `/readyz`, the validation error
verbatim, and `docker compose logs --tail=100 api` with secrets already
redacted by the log pipeline. **Do not paste `.env`.**

Report vulnerabilities through `SECURITY.md`, not a public issue.