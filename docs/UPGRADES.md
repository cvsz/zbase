# Upgrades

## Before any upgrade

```bash
# 1. A verified backup is the precondition. Not a hope, a verified one.
scripts/backup.sh
scripts/verify-backup.sh "$(ls -1dt backups/*/ | head -1)"

# 2. Know exactly what you are running now.
docker compose images
docker compose config | grep image
```

If the backup does not verify, stop. An upgrade without a verified rollback is
a gamble.

## Updating images

Images are pinned by tag **and** digest. Do not switch to `latest`.

```bash
# 1. Pull the pinned tags and read the new digests.
docker compose pull

# 2. Record what changed.
docker image inspect <image>:<tag> --format '{{index .RepoDigests 0}}'

# 3. Apply, recreating containers.
docker compose up -d

# 4. Verify before declaring success.
scripts/verify-install.sh https://s3.example.com
```

To adopt a new upstream version, change the tag in `docker-compose.yml`,
pull, then **re-pin to the resulting digest** so the deployment is reproducible:

```yaml
image: chrislusf/seaweedfs@sha256:<digest>
```

Then re-check capabilities, because an engine upgrade can change them:

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  https://s3.example.com/api/v1/settings/engine-capabilities | jq '.notes'
```

And re-run the compatibility suite, which is the real evidence:

```bash
make test-integration
```

## Rolling back

```bash
# 1. Restore the previous image references in docker-compose.yml.

# 2. Bring the previous images back.
docker compose up -d

# 3. Verify.
scripts/verify-install.sh https://s3.example.com
```

Database migrations are the hazard: if an upgrade applied a migration, the
previous application version may not understand the new schema.

Migrations are forward-only. Rolling back the application does **not** roll
back the schema. Assess compatibility before rolling back:

```bash
docker compose exec -T postgres psql -U s3vault -d s3vault \
  -c "SELECT version, applied_at FROM schema_migrations ORDER BY applied_at DESC LIMIT 5;"
```

If a migration must be reversed, do it deliberately with reviewed SQL, not by
assuming symmetry.

## Upgrading the application

```bash
cd /opt/s3vault
git fetch --tags
git log --oneline HEAD..origin/main        # read what you are taking
git checkout <tag>

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

docker compose build
docker compose up -d
scripts/verify-install.sh https://s3.example.com
```

## Upgrading the engine

The riskiest upgrade, because the engine owns the data.

1. Verify the backup.
2. Read the engine's release notes for S3 API changes.
3. Upgrade the **worker first**, then the **API**, then the engine:

```bash
docker compose pull seaweedfs
docker compose stop seaweedfs
docker compose up -d seaweedfs
# wait for healthy
docker compose restart api worker
```

4. Re-run the compatibility suite and compare capabilities:

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  https://s3.example.com/api/v1/settings/engine-capabilities > /tmp/caps-after.json
jq -r '.supported[]' /tmp/caps-after.json | sort > /tmp/after.txt
```

Compare against `docs/S3-COMPATIBILITY.md`. A feature disappearing should be a
deliberate decision, not a surprise.

5. Verify a sample of objects, not just the health endpoint.

## Upgrading PostgreSQL

Postgres major upgrades require `pg_upgrade` or a logical dump/restore. The
simplest safe path:

```bash
scripts/backup.sh
docker compose stop api worker

# Bring up the new major version against a fresh volume.
docker compose exec postgres pg_dumpall -U s3vault > /tmp/all.sql
# ...
psql -U s3vault -d s3vault < /tmp/all.sql

scripts/verify-install.sh https://s3.example.com
```

Note the mount point requirement for Postgres 18+: the volume must be at
`/var/lib/postgresql`, not `/var/lib/postgresql/data`. Mounting at the old
path makes the container refuse to start, which is a deliberate guard against
an inconsistent upgrade.

## Dependency updates

```bash
.venv/bin/pip list --outdated
# Edit requirements.txt with exact pins, then:
.venv/bin/pip install -r requirements-dev.txt
make test-integration
```

Keep pins exact. Do not ship ranges in `requirements.txt`.

## Version and provenance

This release does not implement automated releases or a changelog generator.
`CHANGELOG.md` is maintained by hand. Record, for each deployment: the
application tag, the engine image digest, the PostgreSQL version, the schema
version from `schema_migrations`, and the outcome of
`scripts/verify-install.sh`.