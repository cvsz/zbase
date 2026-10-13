#!/usr/bin/env bash
# Bring up the isolated stack used by the integration and S3-compatibility
# suites: PostgreSQL for application metadata and SeaweedFS for object storage.
#
# The stack is deliberately isolated from any deployment: separate container
# names, loopback-only port bindings, and a dedicated volume. It never touches
# production data.
#
# Usage:
#   scripts/test-stack.sh up       # start and wait until healthy
#   scripts/test-stack.sh status   # report container state
#   scripts/test-stack.sh down     # stop and remove containers
#   scripts/test-stack.sh reset    # down, then delete volumes, then up
set -uo pipefail

PG_NAME="${PG_NAME:-s3vault-test-pg}"
SW_NAME="${SW_NAME:-s3vault-test-sw}"
PG_IMAGE="${PG_IMAGE:-postgres:18-alpine}"
SW_IMAGE="${SW_IMAGE:-chrislusf/seaweedfs:3.97}"
PG_PORT="${PG_PORT:-55432}"
SW_PORT="${SW_PORT:-8333}"
PG_USER="${PG_USER:-s3vault}"
PG_PASSWORD="${PG_PASSWORD:-s3vault}"
PG_DB="${PG_DB:-s3vault}"
SW_ACCESS_KEY="${SW_ACCESS_KEY:-s3vaulttest}"
SW_SECRET_KEY="${SW_SECRET_KEY:-s3vaulttestsecret}"

log() { printf '%s %s\n' "[test-stack]" "$*"; }
die() { log "ERROR: $*"; exit 1; }

wait_for() {
  local what="$1" attempts="$2" probe="$3"
  for i in $(seq 1 "$attempts"); do
    if eval "$probe" >/dev/null 2>&1; then
      log "$what ready after ${i}s"
      return 0
    fi
    sleep 1
  done
  log "$what did NOT become ready after ${attempts}s"
  return 1
}

pg_probe() {
  docker exec "$PG_NAME" pg_isready -U "$PG_USER" -d "$PG_DB"
}

# The S3 gateway answers 403 to an anonymous root request once it is listening.
sw_probe() {
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:${SW_PORT}/" 2>/dev/null)
  case "$code" in 200|400|403) return 0 ;; *) return 1 ;; esac
}

cmd_up() {
  log "starting PostgreSQL (${PG_IMAGE}) on 127.0.0.1:${PG_PORT}"
  docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
  docker run -d --name "$PG_NAME" \
    --restart unless-stopped \
    -e POSTGRES_USER="$PG_USER" -e POSTGRES_PASSWORD="$PG_PASSWORD" -e POSTGRES_DB="$PG_DB" \
    -p "127.0.0.1:${PG_PORT}:5432" \
    -v s3vault-test-pg-data:/var/lib/postgresql \
    --health-cmd "pg_isready -U $PG_USER -d $PG_DB" \
    --health-interval 2s --health-timeout 3s --health-retries 30 \
    "$PG_IMAGE" >/dev/null || die "could not start PostgreSQL"

  log "starting SeaweedFS (${SW_IMAGE}) S3 gateway on 127.0.0.1:${SW_PORT}"
  docker rm -f "$SW_NAME" >/dev/null 2>&1 || true
  docker run -d --name "$SW_NAME" \
    --restart unless-stopped \
    -e AWS_ACCESS_KEY_ID="$SW_ACCESS_KEY" -e AWS_SECRET_ACCESS_KEY="$SW_SECRET_KEY" \
    -p "127.0.0.1:${SW_PORT}:8333" \
    -v s3vault-test-sw-data:/data \
    "$SW_IMAGE" \
    server -dir=/data -s3 -s3.port=8333 -s3.allowEmptyFolder=true -ip=0.0.0.0 \
    >/dev/null || die "could not start SeaweedFS"

  wait_for "PostgreSQL" 90 pg_probe || die "PostgreSQL never became ready"
  wait_for "SeaweedFS" 90 sw_probe || die "SeaweedFS never became ready"

  cat <<EOF

Stack is ready. Point the test environment at it with:

  export S3VAULT_TEST_DATABASE_URL="postgresql://${PG_USER}:${PG_PASSWORD}@127.0.0.1:${PG_PORT}/${PG_DB}"
  export S3VAULT_TEST_S3_ENDPOINT="http://127.0.0.1:${SW_PORT}"
  export S3VAULT_TEST_S3_ACCESS_KEY="${SW_ACCESS_KEY}"
  export S3VAULT_TEST_S3_SECRET_KEY="${SW_SECRET_KEY}"

Or simply run: make test-integration
EOF
}

cmd_status() {
  docker ps -a --filter "name=$PG_NAME" --filter "name=$SW_NAME" \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
  echo
  printf 'DATABASE_URL=postgresql://%s:%s@127.0.0.1:%s/%s\n' \
    "$PG_USER" "$PG_PASSWORD" "$PG_PORT" "$PG_DB"
  printf 'STORAGE_ENGINE_ENDPOINT=http://127.0.0.1:%s\n' "$SW_PORT"
}

cmd_down() {
  log "removing containers (volumes are kept unless you run 'reset')"
  docker rm -f "$PG_NAME" "$SW_NAME" >/dev/null 2>&1 || true
  log "down"
}

cmd_reset() {
  cmd_down
  log "deleting test volumes"
  docker volume rm s3vault-test-pg-data s3vault-test-sw-data >/dev/null 2>&1 || true
  log "reset complete"
}

case "${1:-up}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  reset) cmd_reset; cmd_up ;;
  *) die "usage: $0 {up|down|status|reset}" ;;
esac