# Development

## Layout

```
apps/
  api/s3vault/          application package
    config.py           fail-closed configuration
    db.py               pooled Postgres access + migration runner
    errors.py           typed errors with stable codes
    logging_setup.py    structured logging + redaction
    app.py              FastAPI factory, middleware, error handlers
    main.py             ASGI entrypoint
    bootstrap.py        first-administrator creation
    security/           passwords, tokens, envelope encryption, RBAC
    storage/            StorageAdapter protocol, SeaweedFS, in-memory
    services/           users, credentials, buckets, quotas, usage, audit, safety
    api/                routers and dependencies
  web/                  no-build console (index.html, assets/)
  worker/worker.py      background jobs
migrations/             numbered .sql files
tests/
  unit/                 no external services
  integration/          needs the stack
  s3-compatibility/     needs the stack, AWS SDK against the engine
  security/             needs the stack for most cases
scripts/                stack, backup, restore, verification
docs/                   this documentation
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

Python 3.14. Everything is pinned.

## Running the tests

```bash
make test-unit          # 88 tests, no containers
make test-integration   # 99 tests, starts the stack itself
make ci                 # the fast gate
```

`scripts/test-integration.sh` brings up an isolated PostgreSQL + SeaweedFS
stack, runs the suites, and tears the stack down. It never touches a
deployment.

```bash
scripts/test-stack.sh up       # keep the stack running while iterating
scripts/test-stack.sh status
scripts/test-stack.sh down
```

## Running the application

```bash
cp .env.example .env          # then fill in the required values

docker compose up -d postgres seaweedfs
S3VAULT_ENV=test \
DATABASE_URL='postgresql://s3vault:s3vault@127.0.0.1:55432/s3vault' \
STORAGE_ENGINE_ENDPOINT='http://127.0.0.1:8333' \
STORAGE_ROOT_ACCESS_KEY=s3vaulttest \
STORAGE_ROOT_SECRET_KEY=s3vaulttestsecret \
PUBLIC_HOSTNAME=s3vault.test COOKIE_SECURE=false \
KEK_BASE64="$(python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())')" \
PYTHONPATH=apps/api .venv/bin/uvicorn s3vault.main:app --port 3001 --reload
```

Console at <http://localhost:3001>. Create the first administrator with
`python -m s3vault.bootstrap`.

## Conventions

- Python 3.14, type hints on public functions, `from __future__ import annotations`.
- `slots=True, frozen=True` dataclasses for immutable values.
- No `print` in application code; use the structlog logger.
- Errors: raise a typed `AppError`. Never leak an internal message to a caller.
- Never log a secret, a token or a signed URL. The redactor is a backstop, not a
  licence.
- SQL: parameterised. Identifiers go through `Migrator.safe_identifier`.
- New endpoints need: request validation, an authorisation check, an audit
  record if they change state, and tests.

## Adding a field

1. Edit `migrations/0001_init.sql` **or** add a new numbered file. Never edit a
   migration that has been applied: the checksum check will refuse.
2. Apply: `make migrate`.
3. Update the type and the service that reads it.
4. Update the API response shape and the console if it is user-visible.
5. Add a test.

## Adding an engine feature

If the engine does not support it, do not pretend. Add a `Feature` member,
probe it, and let `Capabilities.require()` return `501 unsupported_feature`.
Document it in `docs/S3-COMPATIBILITY.md` and add a test that asserts the
measured outcome — including when the measured outcome is "unsupported".

## Tests

Naming and location:

| Kind | Location | Needs the stack |
|---|---|---|
| Unit | `tests/unit/` | no |
| Integration | `tests/integration/` | yes |
| S3 compatibility | `tests/s3-compatibility/` | yes |
| Security | `tests/security/` | mostly |

Rules that matter:

- A test that needs the stack **skips** rather than fails when it is absent.
  It is still recorded as skipped, never as passed.
- Do not point a test at a production endpoint. The fixtures pin
  `S3VAULT_ENV=test`, loopback addresses, and test-only secrets.
- Never write a test that passes because it asserts nothing.

## The console

No build step. `apps/web/index.html` plus `assets/app.js` and
`assets/styles.css`, served by the API under a strict CSP.

The CSP is `default-src 'none'; script-src 'self'`, so **inline scripts and
inline styles do not work**. If you need a change, edit the files.

Build DOM nodes programmatically; do not assign untrusted values through
`innerHTML`. There is no framework here to sanitise for you.

## Inherited template contract

This repository descends from ztemplate. `make validate-template` validates
structure, Markdown links and bootstrap behaviour and **must keep passing**.

- Do not break `docs/startup.md`, `docs/repository-rollout.md`, or the ZEAZ
  execution layer.
- Adding documents is fine, provided links resolve. The validator checks this.
- `scripts/validate_repo.py` rejects broken local Markdown links.

## Repository administration

`scripts/github_admin.py` applies and reads back branch protection. It is
dry-run by default and requires an authenticated admin identity:

```bash
python3 scripts/github_admin.py --repo cvsz/zbase          # dry run
python3 scripts/github_admin.py --repo cvsz/zbase --apply
python3 scripts/github_admin.py --repo cvsz/zbase --verify
```

Verify after any change to it. Script presence is not evidence; read-back is.

## Adding CI

The gate, in order: `fmt-check`, `lint`, `typecheck`, `test-unit`,
`validate-template`, then the integration and S3-compatibility suites with
services available. Pin action SHAs. Never let a failing check be made to pass
by relaxing it.