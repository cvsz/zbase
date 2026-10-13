# Configuration

Configuration is read once at startup into an immutable, validated object. The
process **refuses to start** rather than fall back to an insecure default.

Validation reports variable **names and reasons only**. It never prints a
secret value, so startup logs are safe to ship.

## Profiles

`S3VAULT_ENV` selects the profile: `development`, `test`, `production`.

The production profile applies additional requirements on top of the shared
ones. Failing any of them is a startup error:

| Requirement | Reason |
|---|---|
| `COOKIE_SECURE=true` | Session cookie must not cross plaintext |
| `STORAGE_ENGINE_ENDPOINT` over https | Engine credentials would cross the network in clear |
| `PUBLIC_HOSTNAME` not a placeholder | Otherwise certificates and CORS are wrong |
| `TRUSTED_PROXY_HOPS >= 1` | Otherwise client IPs come from spoofable headers |
| `S3VAULT_ALLOW_PRODUCTION_CHANGES` not set | Otherwise the mutation guard is disabled |
| No placeholder or empty secrets | Otherwise credentials are guessable |

`test` additionally disables external side effects and is the only profile in
which password reset returns a token in the response.

## Reference

Copy `.env.example` to `.env`. Values below are the defaults; production
overrides are marked.

### Public surface

| Variable | Default | Notes |
|---|---|---|
| `PUBLIC_HOSTNAME` | *required* | Rejects `localhost`, `127.0.0.1`, `example.com` in production |
| `S3_ENDPOINT` | `https://$PUBLIC_HOSTNAME` | Advertised to clients |
| `CONSOLE_URL` | `https://$PUBLIC_HOSTNAME` | |
| `S3VAULT_HTTP_PORT` / `S3VAULT_HTTPS_PORT` | `80` / `443` | Host ports for the proxy |

### Storage engine

| Variable | Default | Notes |
|---|---|---|
| `STORAGE_ENGINE` | `seaweedfs` | `seaweedfs` or `memory` (tests only) |
| `STORAGE_ENGINE_ENDPOINT` | *required* | Must be `http(s)://`; must be https in production |
| `STORAGE_ADDRESSING_STYLE` | `path` | `virtual` only with wildcard DNS |
| `STORAGE_REGION` | `us-east-1` | Must match the engine |
| `STORAGE_ROOT_ACCESS_KEY` | *required* | Never returned by any endpoint |
| `STORAGE_ROOT_SECRET_KEY` | *required*, ≥ 8 bytes | Must differ from the access key |
| `STORAGE_VERIFY_TLS` | `true` | |
| `STORAGE_CONNECT_TIMEOUT_S` | `5` | |
| `STORAGE_READ_TIMEOUT_S` | `60` | Raise for large objects |
| `STORAGE_MAX_ATTEMPTS` | `3` | SDK retries |

### Database

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | *required* | `postgresql://user:pass@host:5432/db` |
| `DATABASE_POOL_MIN` / `_MAX` | `1` / `10` | `min` must not exceed `max` |
| `DATABASE_CONNECT_TIMEOUT_S` | `5` | |
| `DATABASE_STATEMENT_TIMEOUT_MS` | `15000` | Guards runaway queries |

### Secrets

| Variable | Default | Notes |
|---|---|---|
| `KEK_BASE64` | *required* | Exactly 32 bytes decoded; encrypts S3 credential secrets |
| `KEK_FILE` | unset | Alternative: a mounted file containing the key |

Rotating the KEK makes every existing credential undecryptable. See
[SECURITY.md#kek-rotation](SECURITY.md#kek-rotation).

### Bootstrap administrator

`BOOTSTRAP_ADMIN_USER`, `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD`.
All three or none — partial configuration is a startup error. Acts only when no
administrator exists. Remove the password after first sign-in.

### Sessions and passwords

| Variable | Default | Notes |
|---|---|---|
| `SESSION_TTL_S` | `3600` | Absolute lifetime |
| `SESSION_IDLE_TTL_S` | `900` | Must not exceed the absolute lifetime |
| `COOKIE_SECURE` | `true` | `false` is rejected in production |
| `COOKIE_SAMESITE` | `lax` | `none` requires `COOKIE_SECURE=true` |
| `SESSION_COOKIE_NAME` / `CSRF_COOKIE_NAME` | `s3vault_session` / `s3vault_csrf` | |
| `CSRF_HEADER_NAME` | `X-CSRF-Token` | |
| `LOGIN_RATE_LIMIT_PER_MIN` | `5` | Per process, per IP |
| `API_RATE_LIMIT_PER_MIN` | `600` | Per process, per IP |
| `PASSWORD_MIN_LENGTH` | `12` | |

### Uploads

| Variable | Default | Notes |
|---|---|---|
| `MAX_OBJECT_BYTES` | `5 GiB` | |
| `MIN_PART_BYTES` | `5 MiB` | **Engine requirement** once a multipart upload has 2+ parts |
| `MAX_PART_BYTES` | `100 MiB` | Must be ≥ `MIN_PART_BYTES` |
| `MULTIPART_THRESHOLD_BYTES` | `MAX_PART_BYTES` | Above this, use multipart |
| `REQUEST_BODY_LIMIT_BYTES` | `8 MiB` | Bodies above this return `413`. Keep below `MAX_OBJECT_BYTES`: large objects use multipart or presigned upload. |
| `PRESIGN_TTL_S` | `900` | |
| `PRESIGN_MAX_TTL_S` | `3600` | Cannot be below `PRESIGN_TTL_S` |

`MIN_PART_BYTES` is set from a measured engine constraint, not a guess. See
[S3-COMPATIBILITY.md#multipart-constraints](S3-COMPATIBILITY.md#multipart-constraints).

### Quotas

| Variable | Default | Notes |
|---|---|---|
| `DEFAULT_USER_QUOTA_BYTES` | `-1` | `-1` unlimited; `0` is rejected |
| `DEFAULT_BUCKET_QUOTA_BYTES` | `-1` | |
| `DEFAULT_MAX_OBJECT_BYTES` | `MAX_OBJECT_BYTES` | |
| `MAX_BUCKETS_PER_USER` | `100` | |

### Proxy

`TRUSTED_PROXY_HOPS` — number of reverse proxies in front of the API. `0` means
`X-Forwarded-For` is ignored entirely. Production requires at least `1`. Set
the real count; a wrong value lets a client forge its address.

### Safety gates

| Variable | Default | Notes |
|---|---|---|
| `S3VAULT_REQUIRE_DRY_RUN` | `0` | `1` makes mutations plan-only |
| `S3VAULT_REQUIRE_BACKUP` | `0` | `1` needs a verified backup under 26 h old; requires `S3VAULT_BACKUP_DIR` |
| `S3VAULT_ALLOW_PRODUCTION_CHANGES` | `0` | Must be `1` for any production mutation |
| `S3VAULT_BACKUP_DIR` | unset | Required when `REQUIRE_BACKUP=1` |
| `S3VAULT_BACKUP_RETENTION_DAYS` | `14` | |

`REQUIRE_DRY_RUN=1` with `ALLOW_PRODUCTION_CHANGES=1` is a startup error.

### Operations

| Variable | Default |
|---|---|
| `LOG_LEVEL` | `INFO` |
| `LOG_JSON` | `true` |
| `METRICS_ENABLED` | `true` |
| `S3VAULT_MIGRATIONS_DIR` | discovered |

## Validating a configuration

```bash
docker compose run --rm api python -c "
import sys; sys.path.insert(0,'/app/apps/api')
from s3vault.config import load_config
c = load_config()
print('environment :', c.env)
print('hostname    :', c.public_hostname)
print('engine      :', c.storage.endpoint_url)
print('addressing  :', c.storage.addressing_style)
print('db          :', c.db.safe_dsn)
print('warnings    :', list(c.issues) or 'none')
print('CONFIGURATION OK')
"
```

`safe_dsn` masks the password. Validation never echoes a secret.

An invalid configuration exits with `2` and a list of offending variable names.

## Inspecting effective settings

The console's Settings tab, or:

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  https://s3.example.com/api/v1/settings | python3 -m json.tool
```

Secrets appear only as `{"configured": true, "value": null}`. A secret's value
is never returned after configuration.

## Changing settings

1. Edit `.env`.
2. `docker compose up -d api worker` — the API validates on start and exits if
   the result would be insecure.
3. Re-run `scripts/verify-install.sh`.

Most changes take effect on restart. There is no hot reload; this is
intentional for a service holding credentials.