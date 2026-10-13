#!/usr/bin/env bash
# Restore a backup into a SEPARATE, disposable stack.
#
# This script never restores over a running deployment. A restore drill is the
# only supported use: it starts its own containers on different ports with
# their own volumes, verifies the restored data, and then tears everything
# down. Restoring into production requires explicit operator action outside
# this script (see docs/BACKUP-RESTORE.md#production-restore).
set -uo pipefail

BACKUP_DIR="${1:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DRILL_PG_PORT="${DRILL_PG_PORT:-56432}"
DRILL_SW_PORT="${DRILL_SW_PORT:-8433}"
PG_NAME="s3vault-restore-pg"
SW_NAME="s3vault-restore-sw"
PG_VOL="s3vault-restore-pgdata"
SW_VOL="s3vault-restore-swdata"

log() { printf '%s %s\n' "[restore]" "$*"; }
die() { printf '%s ERROR: %s\n' "[restore]" "$*" >&2; cleanup; exit 1; }

[ -n "$BACKUP_DIR" ] || { echo "usage: $0 <backup-directory>" >&2; exit 2; }
[ -d "$BACKUP_DIR" ] || die "not a directory: $BACKUP_DIR"

cleanup() {
  log "tearing down the drill stack"
  docker rm -f "$PG_NAME" "$SW_NAME" >/dev/null 2>&1 || true
  docker volume rm "$PG_VOL" "$SW_VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT

log "verifying the backup before touching anything"
scripts/verify-backup.sh "$BACKUP_DIR" || die "refusing to restore from a backup that does not verify"

log "starting an isolated drill stack (ports $DRILL_PG_PORT/$DRILL_SW_PORT)"
docker volume rm "$PG_VOL" "$SW_VOL" >/dev/null 2>&1 || true

docker run -d --name "$PG_NAME" \
  -e POSTGRES_USER=s3vault -e POSTGRES_PASSWORD=s3vault -e POSTGRES_DB=s3vault \
  -p "127.0.0.1:${DRILL_PG_PORT}:5432" -v "$PG_VOL":/var/lib/postgresql \
  postgres:18-alpine >/dev/null || die "could not start the drill PostgreSQL"

docker run -d --name "$SW_NAME" \
  -e AWS_ACCESS_KEY_ID=s3vaulttest -e AWS_SECRET_ACCESS_KEY=s3vaulttestsecret \
  -p "127.0.0.1:${DRILL_SW_PORT}:8333" -v "$SW_VOL":/data \
  chrislusf/seaweedfs:3.97 server -dir=/data -s3 -s3.port=8333 \
  -s3.allowEmptyFolder=true -ip=0.0.0.0 >/dev/null || die "could not start the drill SeaweedFS"

for i in $(seq 1 90); do
  docker exec "$PG_NAME" pg_isready -U s3vault -d s3vault >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$PG_NAME" pg_isready -U s3vault -d s3vault >/dev/null 2>&1 \
  || die "drill PostgreSQL never became ready"

log "restoring the database"
docker exec -i "$PG_NAME" pg_restore -U s3vault -d s3vault --clean --if-exists \
  < "$BACKUP_DIR/database.dump" || die "pg_restore failed"

log "verifying restored metadata"
TABLES=$(docker exec "$PG_NAME" psql -U s3vault -d s3vault -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
USERS=$(docker exec "$PG_NAME" psql -U s3vault -d s3vault -tAc "SELECT count(*) FROM users")
CREDS=$(docker exec "$PG_NAME" psql -U s3vault -d s3vault -tAc "SELECT count(*) FROM credentials")
log "restored tables=$TABLES users=$USERS credentials=$CREDS"

if [ "${TABLES:-0}" -lt 10 ]; then
  die "restored database looks incomplete ($TABLES tables)"
fi

if [ -f "$BACKUP_DIR/engine.tar" ]; then
  log "restoring engine data"
  docker cp "$BACKUP_DIR/engine.tar" "$SW_NAME":/tmp/engine.tar >/dev/null || die "copy failed"
  docker exec "$SW_NAME" sh -c 'mkdir -p /data && tar -xf /tmp/engine.tar -C /data' \
    || die "engine archive restore failed"
  log "engine data restored"
else
  log "NOTE: no engine.tar to restore; only metadata was restored"
fi

log "RESULT: restore drill completed against an isolated stack"
log "The production deployment was not touched."
exit 0