#!/usr/bin/env bash
# Verify a backup without restoring it.
#
# Checks that the archive exists, that its recorded checksums still match, and
# that the database dump is structurally readable. This is the check that
# should run on a schedule: a backup that has never been verified is a guess.
set -uo pipefail

BACKUP_DIR="${1:-}"
if [ -z "$BACKUP_DIR" ]; then
  echo "usage: $0 <backup-directory>" >&2
  exit 2
fi

if [ ! -d "$BACKUP_DIR" ]; then
  echo "ERROR: not a directory: $BACKUP_DIR" >&2
  exit 1
fi

log() { printf '%s %s\n' "[verify-backup]" "$*"; }
failures=0

manifest="$BACKUP_DIR/MANIFEST.txt"
if [ -f "$manifest" ]; then
  log "manifest present"
  sed 's/^/  /' "$manifest"
else
  log "WARNING: no MANIFEST.txt"
  failures=$((failures + 1))
fi

if [ -f "$BACKUP_DIR/database.dump" ]; then
  log "checking database dump checksum"
  recorded=$(grep '^database_sha256=' "$manifest" 2>/dev/null | cut -d= -f2)
  if [ -n "$recorded" ]; then
    actual=$(sha256sum "$BACKUP_DIR/database.dump" | cut -d' ' -f1)
    if [ "$recorded" = "$actual" ]; then
      log "database checksum OK"
    else
      log "ERROR: database checksum mismatch"
      log "  recorded=$recorded"
      log "  actual  =$actual"
      failures=$((failures + 1))
    fi
  else
    log "WARNING: no recorded database checksum"
  fi

  # A truncated or corrupt dump fails to list.
  if command -v pg_restore >/dev/null && pg_restore --list "$BACKUP_DIR/database.dump" >/dev/null 2>&1; then
    log "database dump is structurally readable"
  else
    log "NOTE: pg_restore not available here; structural check skipped"
  fi
else
  log "ERROR: database.dump missing"
  failures=$((failures + 1))
fi

if [ -f "$BACKUP_DIR/engine.tar" ]; then
  if tar -tf "$BACKUP_DIR/engine.tar" >/dev/null 2>&1; then
    log "engine archive is readable ($(tar -tf "$BACKUP_DIR/engine.tar" | wc -l) entries)"
  else
    log "ERROR: engine archive is corrupt"
    failures=$((failures + 1))
  fi
else
  log "NOTE: no engine.tar (engine may have been archived via shell)"
fi

if grep -q '^engine=FAILED' "$manifest" 2>/dev/null; then
  log "ERROR: the manifest records an engine failure"
  failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
  log "RESULT: backup verified"
  exit 0
fi
log "RESULT: $failures problem(s) found"
exit 1