# zbase

Self-hosted S3-compatible object storage platform with a web management
console. Initialized from [ztemplate](https://github.com/cvsz/ztemplate).

S3Vault is a **control plane and signing proxy**. It does not implement the S3
protocol: SeaweedFS owns object bytes, durability and the wire protocol, while
S3Vault owns identity, policy, credentials, quotas and audit.

## Status

Working, with evidence. Every claim below is backed by a test that was
executed.

| Area | State |
|---|---|
| Configuration (fail-closed) | Implemented, 30 unit tests |
| PostgreSQL schema + migrations | Implemented, applied on PostgreSQL 18 |
| Auth, sessions, CSRF, RBAC | Implemented |
| Credential lifecycle + envelope encryption | Implemented |
| Buckets, objects, multipart, presigned URLs | Implemented |
| Quotas with reservations | Implemented |
| Audit log with redaction | Implemented |
| Safety gates (dry run, production guard, backup) | Implemented |
| Storage capability probing | Implemented against a live engine |
| Web console | Implemented, no-build, real API |
| Background worker | Implemented |
| Deployment bundle (Docker, nginx, scripts) | Implemented |
| MFA, OIDC SSO | **Not implemented** — see below |

**Test evidence: 195 tests passing** (88 unit, 72 integration + S3
compatibility, 35 security) against PostgreSQL 18 and SeaweedFS 3.97.

```bash
make test-unit           # 88 tests, no containers
make test-integration    # 107 tests, starts an isolated stack
```

## Quick start

```bash
cp .env.example .env     # then fill in the required values
make venv
scripts/test-stack.sh up      # isolated PostgreSQL + SeaweedFS
make up                        # or docker compose up -d --build
scripts/verify-install.sh https://s3.example.com
```

Full instructions in [docs/INSTALLATION.md](docs/INSTALLATION.md).

## Configuration

Configuration is validated at startup and the process **refuses to run** rather
than fall back to an insecure default. Production additionally requires
`COOKIE_SECURE=true`, an HTTPS engine endpoint, a real hostname, at least one
trusted proxy hop, and no placeholder secrets.

Validation reports variable *names* only — never values — so startup logs are
safe to ship. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Using it

```bash
# Sign in
curl -c cookies -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"..."}' \
  https://s3.example.com/api/v1/auth/login

CSRF=$(grep s3vault_csrf cookies | awk '{print $7}')
AUTH="-b cookies -H X-CSRF-Token:$CSRF"

# Create a bucket
curl $AUTH -H 'Content-Type: application/json' \
  -d '{"name":"documents"}' https://s3.example.com/api/v1/buckets

# Issue a scoped access key (the secret is shown once)
curl $AUTH -H 'Content-Type: application/json' \
  -d '{"permissions":["readwrite"],"bucket_scope":["documents"]}' \
  https://s3.example.com/api/v1/credentials
```

The console covers the same ground interactively.

## Measured engine behaviour

S3Vault probes the engine at startup rather than assuming. Against SeaweedFS
3.97, measured and documented in
[docs/S3-COMPATIBILITY.md](docs/S3-COMPATIBILITY.md):

- **Supported:** bucket CRUD, object CRUD, range reads, copy, metadata,
  pagination, multipart, presigned PUT/GET, versioning, CORS, lifecycle.
- **Not supported:** bucket policies, engine usage metrics.
- **Multipart:** with 2+ parts, every part must be ≥ 5 MiB or the engine
  returns `EntityTooSmall`.
- **Bucket deletion:** the engine deletes non-empty buckets without
  complaint, so S3Vault performs its own emptiness check and requires an exact
  name confirmation.
- **Lifecycle:** rule IDs are normalised to the filter prefix by the engine.

Unsupported features return `501 unsupported_feature`. S3Vault never
advertises a capability the engine lacks.

## Security

See [docs/SECURITY.md](docs/SECURITY.md) for the full threat model and the
test that covers each control.

- Argon2id when available, otherwise **scrypt** by default. The stored hash
  carries an algorithm id, so hashes upgrade transparently on next sign-in.
  (Argon2id is the default in deployments that install `argon2-cffi`; the
  rationale for the default is in the security doc.)
- S3 secret keys are AES-256-GCM envelopes under a KEK held outside the
  database, bound by AAD to the credential identity.
- Opaque session tokens, stored hashed; `HttpOnly` + `Secure` + `SameSite`.
- Double-submit CSRF on every mutating request.
- Capability-based RBAC with least-privilege roles.
- Log redaction masking JWTs, signed query strings and `Authorization`.
- Safety gates that fail closed: dry run, production mutation guard, backup
  requirement.

**Known gaps:** no MFA, no OIDC SSO, per-process (not shared) rate limiting,
no mail transport for password reset.

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data ownership, request path |
| [adr/0001-storage-engine.md](docs/adr/0001-storage-engine.md) | Engine decision record |
| [S3-COMPATIBILITY.md](docs/S3-COMPATIBILITY.md) | Measured compatibility matrix |
| [SECURITY.md](docs/SECURITY.md) | Threat model, controls, gaps |
| [INSTALLATION.md](docs/INSTALLATION.md) | Deployment, Compose and hardened Linux |
| [CONFIGURATION.md](docs/CONFIGURATION.md) | Every setting, validation rules |
| [OPERATIONS.md](docs/OPERATIONS.md) | Daily/weekly/monthly, safety gates |
| [BACKUP-RESTORE.md](docs/BACKUP-RESTORE.md) | Backup, verification, restore drill |
| [DISASTER-RECOVERY.md](docs/DISASTER-RECOVERY.md) | Failure scenarios and recovery |
| [UPGRADES.md](docs/UPGRADES.md) | Upgrading, rollback, pinning |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Symptoms and fixes |
| [DEVELOPMENT.md](docs/DEVELOPMENT.md) | Layout, conventions, adding features |

Inherited from ztemplate: [startup.md](docs/startup.md),
[repository-rollout.md](docs/repository-rollout.md),
[cloudflare-terraform.md](docs/cloudflare-terraform.md),
[release.md](docs/release.md).

## Repository contract

This repository inherits the ztemplate agent contract. Read
[AGENTS.md](AGENTS.md) before editing, and run:

```bash
make validate-template   # structure, Markdown links, bootstrap behaviour
make ci                  # the fast gate
```

## Limitations

Stated plainly so nothing is assumed:

1. **No public S3 wire protocol.** The public endpoint is the administrative
   API. Direct client access is via presigned URLs.
2. **Single-node engine.** One disk's worth of durability. Multiple containers
   do not create redundancy.
3. **Usage is measured by listing** objects, which is O(objects).
4. **No MFA or SSO.**
5. **No Prometheus endpoint**, despite `METRICS_ENABLED`; health, structured
   logs and audit records are the observability surface.
6. **KEK rotation requires re-issuing credentials**; there is no re-encryption
   tool.

## Licence

MIT. See [LICENSE](LICENSE).