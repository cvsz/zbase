#!/usr/bin/env bash
# Run the integration and S3-compatibility suites against an isolated stack.
#
# The stack is started automatically if it is not already reachable, so a run
# never depends on leftover state from a previous session.
#
# Usage:
#   scripts/test-integration.sh            # integration + s3 compatibility
#   scripts/test-integration.sh --unit     # also include unit tests
#   scripts/test-integration.sh --keep     # leave the stack running afterwards
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PG_PORT="${PG_PORT:-55432}"
SW_PORT="${SW_PORT:-8333}"
KEEP_STACK=0
TARGETS=("tests/integration" "tests/s3-compatibility" "tests/security")

for arg in "$@"; do
  case "$arg" in
    --unit) TARGETS=("tests/unit" "${TARGETS[@]}") ;;
    --keep) KEEP_STACK=1 ;;
    *) TARGETS=("$arg") ;;
  esac
done

PY="${PY:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "ERROR: virtualenv missing at $PY" >&2
  echo "Create it first:  python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2
  exit 2
fi

stack_reachable() {
  "$PY" - <<PYEOF
import socket, sys
ok = True
for port in ($PG_PORT, $SW_PORT):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    except OSError:
        ok = False
sys.exit(0 if ok else 1)
PYEOF
}

cleanup() {
  if [ "$KEEP_STACK" -eq 0 ]; then
    scripts/test-stack.sh down >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

ensure_stack() {
  # Up to three attempts: this host's Docker daemon is occasionally restarted
  # underneath a run, which stops the containers.
  for attempt in 1 2 3; do
    if stack_reachable; then
      return 0
    fi
    if [ "$attempt" -gt 1 ]; then
      echo "==> stack unreachable again, restarting it (attempt $attempt)"
    else
      echo "==> starting the isolated test stack"
    fi
    scripts/test-stack.sh up >/dev/null || true
    for _ in $(seq 1 45); do
      stack_reachable && return 0
      sleep 1
    done
  done
  echo "ERROR: the test stack never became reachable" >&2
  return 1
}

ensure_stack || exit 1

echo "==> running ${TARGETS[*]}"
S3VAULT_TEST_DATABASE_URL="postgresql://s3vault:s3vault@127.0.0.1:${PG_PORT}/s3vault" \
S3VAULT_TEST_S3_ENDPOINT="http://127.0.0.1:${SW_PORT}" \
S3VAULT_TEST_S3_ACCESS_KEY="s3vaulttest" \
S3VAULT_TEST_S3_SECRET_KEY="s3vaulttestsecret" \
PYTHONPATH="apps/api" \
  "$PY" -m pytest "${TARGETS[@]}" -v -p no:cacheprovider
status=$?

echo
if [ "$status" -eq 0 ]; then
  echo "==> integration gate PASSED"
else
  echo "==> integration gate FAILED (exit $status)"
fi
exit "$status"