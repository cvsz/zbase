"""Audit event recording.

Audit rows are append-only and must never contain secrets. Every ``detail``
payload passes through the shared redactor before it is written.
"""

from __future__ import annotations

import json
from typing import Any

from ..db import Database
from ..logging_setup import _redact

#: Keys that must never reach an audit row, regardless of caller intent.
FORBIDDEN_DETAIL_KEYS = frozenset(
    {
        "password",
        "new_password",
        "current_password",
        "secret",
        "secret_access_key",
        "access_key_secret",
        "authorization",
        "cookie",
        "token",
        "csrf_token",
        "kek",
        "presigned_url",
        "url",
        "x_amz_signature",
    }
)

#: Action names are stable identifiers used by the console and by exports.
LOGIN_SUCCESS = "auth.login.success"
LOGIN_FAILURE = "auth.login.failure"
LOGOUT = "auth.logout"
PASSWORD_RESET_REQUESTED = "auth.password_reset.requested"
PASSWORD_RESET_COMPLETED = "auth.password_reset.completed"
SESSION_REVOKED = "auth.session.revoked"
USER_CREATED = "user.created"
USER_UPDATED = "user.updated"
USER_DEACTIVATED = "user.deactivated"
USER_LOGIN_DELETED = "user.login_deleted"
CREDENTIAL_CREATED = "credential.created"
CREDENTIAL_REVOKED = "credential.revoked"
CREDENTIAL_ROTATED = "credential.rotated"
BUCKET_CREATED = "bucket.created"
BUCKET_DELETED = "bucket.deleted"
BUCKET_CONFIG_CHANGED = "bucket.config_changed"
OBJECT_DELETED = "object.deleted"
OBJECT_DELETED_BULK = "object.deleted_bulk"
QUOTA_CHANGED = "quota.changed"
SETTINGS_CHANGED = "settings.changed"
BACKUP_STARTED = "backup.started"
BACKUP_COMPLETED = "backup.completed"
RESTORE_COMPLETED = "restore.completed"
PRODUCTION_CHANGE_BLOCKED = "safety.production_change_blocked"
DRY_RUN_OPERATION = "safety.dry_run"


class AuditService:
    """Writes and reads audit events."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- writing ------------------------------------------------------
    def record(
        self,
        *,
        action: str,
        resource_type: str,
        outcome: str,
        actor_id: str | None = None,
        actor_type: str = "system",
        actor_label: str = "",
        resource_id: str = "",
        request_id: str = "",
        ip_address: str | None = None,
        user_agent: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if outcome not in {"success", "denied", "failure"}:
            raise ValueError(f"invalid audit outcome: {outcome!r}")
        safe_detail = self._sanitise(detail or {})
        self._db.execute(
            """
            INSERT INTO audit_events
                (actor_id, actor_type, actor_label, action, resource_type,
                 resource_id, outcome, request_id, ip_address, user_agent, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                actor_id,
                actor_type,
                actor_label,
                action,
                resource_type,
                resource_id,
                outcome,
                request_id,
                _ip(ip_address),
                _truncate(user_agent, 512),
                json.dumps(safe_detail),
            ),
        )

    @staticmethod
    def _sanitise(detail: dict[str, Any]) -> dict[str, Any]:
        """Drop forbidden keys outright, then redact anything credential-shaped."""
        kept = {k: v for k, v in detail.items() if k.lower() not in FORBIDDEN_DETAIL_KEYS}
        redacted = _redact(kept)
        return redacted if isinstance(redacted, dict) else {"value": redacted}

    # -- reading ------------------------------------------------------
    def list(
        self,
        *,
        actor_id: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        outcome: str | None = None,
        since: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        where = ["TRUE"]
        params: list[Any] = []
        for column, value in (
            ("actor_id", actor_id),
            ("action", action),
            ("resource_type", resource_type),
            ("outcome", outcome),
        ):
            if value:
                params.append(value)
                where.append(f"{column} = %s")
        if since:
            params.append(since)
            where.append("occurred_at >= %s")

        clause = " AND ".join(where)
        total_row = self._db.fetch_one(
            f"SELECT count(*) AS n FROM audit_events WHERE {clause}", params
        )
        total = int(total_row["n"]) if total_row else 0

        rows = self._db.fetch_all(
            f"""
            SELECT id, occurred_at, actor_id, actor_type, actor_label, action,
                   resource_type, resource_id, outcome, request_id,
                   ip_address::text AS ip_address, user_agent, detail
            FROM audit_events
            WHERE {clause}
            ORDER BY occurred_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        )
        return rows, total

    def count_since(self, since_iso: str) -> int:
        row = self._db.fetch_one(
            "SELECT count(*) AS n FROM audit_events WHERE occurred_at >= %s", (since_iso,)
        )
        return int(row["n"]) if row else 0


def _ip(value: str | None) -> str | None:
    """Validate an IP for the INET column; ``None`` when unusable."""
    if not value:
        return None
    import ipaddress

    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]