"""Operational safety gates, enforced in code.

These gates are executable, not documentation. Every mutating entry point that
can destroy persistent state consults :class:`SafetyGuard` first.

Rules (docs/OPERATIONS.md#safety-gates):

* ``S3VAULT_REQUIRE_DRY_RUN=1`` - mutating operations compute a plan and
  return it without touching persistent state. The plan is what tests assert on
  to prove the backend was never reached.
* ``S3VAULT_ALLOW_PRODUCTION_CHANGES=1`` - required before any mutation in the
  ``production`` environment. Absent or invalid, mutations fail closed.
* ``S3VAULT_REQUIRE_BACKUP=1`` - destructive operations require a recent,
  verified backup record.
* Missing configuration never enables destructive behaviour; it disables it.
"""

from __future__ import annotations

from typing import Any

from ..config import AppConfig
from ..db import Database
from ..errors import SafetyGateError
from .audit import (
    BACKUP_COMPLETED,
    DRY_RUN_OPERATION,
    PRODUCTION_CHANGE_BLOCKED,
    AuditService,
)

#: A backup must have completed within this window to satisfy REQUIRE_BACKUP.
BACKUP_FRESHNESS_HOURS = 26


class SafetyGuard:
    def __init__(self, config: AppConfig, db: Database, audit: AuditService) -> None:
        self._config = config
        self._db = db
        self._audit = audit

    # -- gates ------------------------------------------------------------
    def assert_mutation_allowed(
        self,
        *,
        operation: str,
        actor_id: str | None = None,
        request_id: str = "",
        ip: str | None = None,
    ) -> None:
        """Raise :class:`SafetyGateError` when a mutation must not proceed."""
        safety = self._config.safety

        if self._config.is_production and not safety.allow_production_changes:
            self._audit.record(
                action=PRODUCTION_CHANGE_BLOCKED,
                resource_type="safety_gate",
                resource_id=operation,
                outcome="denied",
                actor_id=actor_id,
                actor_type="user" if actor_id else "system",
                request_id=request_id,
                ip_address=ip,
                detail={"operation": operation},
            )
            raise SafetyGateError(
                "this operation is blocked in production because "
                "S3VAULT_ALLOW_PRODUCTION_CHANGES is not enabled",
                details={"operation": operation, "env": self._config.env},
            )

        if safety.require_backup and _is_destructive(operation):
            self.assert_backup_fresh(operation=operation)

    def assert_backup_fresh(self, *, operation: str) -> None:
        row = self._db.fetch_one(
            """
            SELECT finished_at FROM backup_records
            WHERE status = 'ok' AND verified
              AND finished_at >= now() - (%s * interval '1 hour')
            ORDER BY finished_at DESC LIMIT 1
            """,
            (BACKUP_FRESHNESS_HOURS,),
        )
        if row is None:
            raise SafetyGateError(
                f"{operation} requires a recent verified backup, but none was found; "
                f"run scripts/backup.sh or clear S3VAULT_REQUIRE_BACKUP",
                details={"operation": operation, "freshness_hours": BACKUP_FRESHNESS_HOURS},
            )

    # -- dry run -----------------------------------------------------------
    @property
    def dry_run_required(self) -> bool:
        return self._config.safety.require_dry_run

    def record_dry_run(
        self,
        *,
        operation: str,
        actor_id: str | None,
        plan: dict[str, Any],
        request_id: str = "",
        ip: str | None = None,
    ) -> None:
        self._audit.record(
            action=DRY_RUN_OPERATION,
            resource_type="safety_gate",
            resource_id=operation,
            outcome="success",
            actor_id=actor_id,
            actor_type="user" if actor_id else "system",
            request_id=request_id,
            ip_address=ip,
            detail={"operation": operation, "plan": plan},
        )

    # -- backup bookkeeping ---------------------------------------------------
    def begin_backup(self, location: str) -> str:
        import uuid

        backup_id = str(uuid.uuid4())
        self._db.execute(
            "INSERT INTO backup_records (id, status, location) VALUES (%s, 'running', %s)",
            (backup_id, location),
        )
        return backup_id

    def finish_backup(
        self,
        backup_id: str,
        *,
        status: str,
        bytes_total: int | None = None,
        checksum: str | None = None,
        verified: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        import json

        self._db.execute(
            """
            UPDATE backup_records
               SET status = %s, finished_at = now(), bytes_total = %s,
                   checksum = %s, verified = %s, detail = %s::jsonb
             WHERE id = %s
            """,
            (status, bytes_total, checksum, verified, json.dumps(detail or {}), backup_id),
        )
        if status == "ok":
            self._audit.record(
                action=BACKUP_COMPLETED,
                resource_type="backup",
                resource_id=backup_id,
                outcome="success",
                actor_type="system",
                detail={
                    "location": detail.get("location") if detail else None,
                    "bytes_total": bytes_total,
                    "verified": verified,
                },
            )

    def latest_backup(self) -> dict[str, Any] | None:
        return self._db.fetch_one(
            "SELECT * FROM backup_records ORDER BY started_at DESC LIMIT 1"
        )


def _is_destructive(operation: str) -> bool:
    """Operations that can lose data irrecoverably."""
    return operation.startswith(("bucket.delete", "object.delete", "restore", "bucket.purge"))