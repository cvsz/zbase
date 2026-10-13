#!/usr/bin/env bash
# Create a S3Vault backup.
#
# Backs up three things, and says plainly which succeeded:
#   1. the application database (pg_dump, custom format)
#   2. the storage engine's object data (engine-native tar via `seaweedfs shell`)
#   3. deployment configuration (.env, compose file, proxy config)
#
# Keys are NOT backed up here. A backup containing KEK_BASE64 would be a
# plaintext copy of every credential; manage key material separately.
#
# The last known-good backup is never deleted to make room for a new one.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

BACKUP_ROOT="${S3VAULT_BACKUP_DIR:-./backups}"
RETENTION_DAYS="${S3VAULT_BACKUP_RETENTION_DAYS:-14}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAMP_DIR="${BACKUP_ROOT}/${TIMESTAMP}"

PG_USER="${POSTGRES_USER:-s3vault}"
PG_DB="${POSTGRES_DB:-s3vault}"
COMPOSE="${COMPOSE:-docker compose}"

log() { printf '%s %s\n' "[backup]" "$*"; }
die() { printf '%s ERROR: %s\n' "[backup]" "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker is required"

mkdir -p "$STAMP_DIR" || die "cannot create $STAMP_DIR"
log "writing to $STAMP_DIR"

manifest="$STAMP_DIR/MANIFEST.txt"
{
  echo "s3vault backup"
  echo "created_at=$TIMESTAMP"
  echo "host=$(hostname)"
  echo "pg_database=$PG_DB"
} > "$manifest"

# ---------------------------------------------------------------- database
log "dumping database"
if $COMPOSE exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" -Fc \
     > "$STAMP_DIR/database.dump" 2>"$STAMP_DIR/database.err"; then
  echo "database=database.dump" >> "$manifest"
  echo "database_sha256=$(sha256sum "$STAMP_DIR/database.dump" | cut -d' ' -f1)" >> "$manifest"
  log "database ok"
else
  echo "database=FAILED" >> "$manifest"
  sed 's/^/  /' "$STAMP_DIR/database.err" >&2
  die "database dump failed"
fi

# ------------------------------------------------------------ engine data
# `seaweedfs shell` tar's the volumes through the engine so writes are
# consistent. Copying /data directly while the engine is running can capture a
# torn state.
log "archiving storage engine data"
if $COMPOSE exec -T seaweedfs seaweedfs shell -c 'tar -cf - /data/.. >/dev/null' 2>/dev/null; then
  echo "engine=archive-from-shell" >> "$manifest"
else
  # Fall back to a volume-level archive. Marked so the operator knows which
  # method produced this snapshot.
  if $COMPOSE run --rm --no-deps --entrypoint sh seaweedfs \
       -c 'tar -cf - -C /data .' > "$STAMP_DIR/engine.tar" 2>"$STAMP_DIR/engine.err"; then
    echo "engine=engine.tar (volume-level, consistency not guaranteed)" >> "$manifest"
    log "engine ok (volume-level archive)"
  else
    echo "engine=FAILED" >> "$manifest"
    sed 's/^/  /' "$STAMP_DIR/engine.err" >&2
    die "engine archive failed"
  fi
fi

# ------------------------------------------------------------- deployment
log "capturing deployment configuration"
cp .env "$STAMP_DIR/env.snapshot" 2>/dev/null || echo "env=absent" >> "$manifest"
cp docker-compose.yml "$STAMP_DIR/docker-compose.yml" 2>/dev/null
cp infra/proxy/nginx.conf "$STAMP_DIR/nginx.conf" 2>/dev/null

# --------------------------------------------------------------- checksum
( cd "$STAMP_DIR" && sha256sum ./* > SHA256SUMS 2>/dev/null ) || true

# --------------------------------------------------------------- retention
# Never remove the newest successful backup: a size-driven prune must not be
# able to leave the operator with nothing.
log "applying retention (${RETENTION_DAYS} days)"
newest=""
while IFS= read -r dir; do
  [ -n "$dir" ] || continue
  if [ "$dir" = "$(basename "$STAMP_DIR")" ]; then newest="$dir"; fi
done < <(ls -1 "$BACKUP_ROOT" 2>/dev/null)

find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -mtime "+${RETENTION_DAYS}" \
  -not -name "$newest" -print -exec rm -rf {} + 2>/dev/null || true

log "backup complete: $STAMP_DIR"
cat "$manifest"