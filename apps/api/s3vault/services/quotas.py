"""Quota accounting with reservations.

Enforcement model (documented in docs/ARCHITECTURE.md#quotas):

* Quotas are enforced by the **application**, using reservations, because the
  engine does not expose quota APIs.
* A ``held`` reservation reserves bytes *before* an upload begins, so two
  concurrent uploads cannot both observe enough headroom.
* Reservations are settled (``committed``) after the engine accepts the
  object, or released on failure.
* Reservations that are never settled expire, so a crashed worker cannot leak
  capacity permanently.

Because usage is derived from engine listings rather than an engine-side
counter, these quotas are an admission-control mechanism, not a hard storage
ceiling. docs/S3-COMPATIBILITY.md states this explicitly.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..config import AppConfig
from ..db import Database
from ..errors import QuotaExceededError
from ..security.tokens import utcnow

RESERVATION_TTL_S = 3600


class QuotaService:
    def __init__(self, db: Database, config: AppConfig) -> None:
        self._db = db
        self._config = config

    # -- policy lookup -------------------------------------------------
    def user_policy(self, user_id: str) -> dict[str, int]:
        row = self._db.fetch_one(
            "SELECT max_bytes, max_objects, max_object_bytes, enforcement_enabled "
            "FROM quota_policies WHERE user_id = %s",
            (user_id,),
        )
        if row:
            return {
                "max_bytes": int(row["max_bytes"]),
                "max_objects": int(row["max_objects"]),
                "max_object_bytes": int(row["max_object_bytes"]),
                "enforced": bool(row["enforcement_enabled"]),
            }
        return {
            "max_bytes": self._config.quotas.default_user_bytes,
            "max_objects": -1,
            "max_object_bytes": self._config.quotas.default_max_object_bytes,
            "enforced": True,
        }

    def bucket_policy(self, bucket_id: str) -> dict[str, int]:
        row = self._db.fetch_one(
            "SELECT max_bytes, max_objects, max_object_bytes, enforcement_enabled "
            "FROM quota_policies WHERE bucket_id = %s",
            (bucket_id,),
        )
        if row:
            return {
                "max_bytes": int(row["max_bytes"]),
                "max_objects": int(row["max_objects"]),
                "max_object_bytes": int(row["max_object_bytes"]),
                "enforced": bool(row["enforcement_enabled"]),
            }
        return {
            "max_bytes": self._config.quotas.default_bucket_bytes,
            "max_objects": -1,
            "max_object_bytes": self._config.quotas.default_max_object_bytes,
            "enforced": True,
        }

    def set_user_policy(
        self,
        user_id: str,
        *,
        max_bytes: int,
        max_objects: int = -1,
        max_object_bytes: int = -1,
        enforced: bool = True,
    ) -> None:
        _validate_limit("max_bytes", max_bytes)
        _validate_limit("max_objects", max_objects)
        _validate_limit("max_object_bytes", max_object_bytes)
        self._db.execute(
            """
            INSERT INTO quota_policies
                (user_id, max_bytes, max_objects, max_object_bytes, enforcement_enabled)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE
               SET max_bytes = EXCLUDED.max_bytes,
                   max_objects = EXCLUDED.max_objects,
                   max_object_bytes = EXCLUDED.max_object_bytes,
                   enforcement_enabled = EXCLUDED.enforcement_enabled,
                   updated_at = now()
            """,
            (user_id, max_bytes, max_objects, max_object_bytes, enforced),
        )

    def set_bucket_policy(
        self,
        bucket_id: str,
        *,
        max_bytes: int,
        max_objects: int = -1,
        max_object_bytes: int = -1,
        enforced: bool = True,
    ) -> None:
        _validate_limit("max_bytes", max_bytes)
        _validate_limit("max_objects", max_objects)
        _validate_limit("max_object_bytes", max_object_bytes)
        self._db.execute(
            """
            INSERT INTO quota_policies
                (bucket_id, max_bytes, max_objects, max_object_bytes, enforcement_enabled)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (bucket_id) DO UPDATE
               SET max_bytes = EXCLUDED.max_bytes,
                   max_objects = EXCLUDED.max_objects,
                   max_object_bytes = EXCLUDED.max_object_bytes,
                   enforcement_enabled = EXCLUDED.enforcement_enabled,
                   updated_at = now()
            """,
            (bucket_id, max_bytes, max_objects, max_object_bytes, enforced),
        )

    # -- accounting ------------------------------------------------------
    def held_bytes(self, user_id: str, bucket_id: str | None = None) -> int:
        row = self._db.fetch_one(
            """
            SELECT coalesce(sum(bytes), 0) AS n FROM quota_reservations
            WHERE user_id = %s AND state = 'held' AND expires_at > now()
              AND (%s::uuid IS NULL OR bucket_id = %s::uuid)
            """,
            (user_id, bucket_id, bucket_id),
        )
        return int(row["n"]) if row else 0

    def usage_for_user(self, user_id: str) -> tuple[int, int]:
        """Return (bytes, object_count) currently committed for a user."""
        row = self._db.fetch_one(
            """
            SELECT coalesce(sum(s.storage_bytes), 0) AS bytes,
                   coalesce(sum(s.object_count), 0) AS objects
            FROM usage_snapshots s
            WHERE s.scope = 'user' AND s.user_id = %s
              AND s.measured_at = (
                    SELECT max(measured_at) FROM usage_snapshots
                    WHERE scope = 'user' AND user_id = %s
              )
            """,
            (user_id, user_id),
        )
        return (int(row["bytes"]), int(row["objects"])) if row else (0, 0)

    def usage_for_bucket(self, bucket_id: str) -> tuple[int, int]:
        row = self._db.fetch_one(
            """
            SELECT coalesce(sum(s.storage_bytes), 0) AS bytes,
                   coalesce(sum(s.object_count), 0) AS objects
            FROM usage_snapshots s
            WHERE s.scope = 'bucket' AND s.bucket_id = %s
              AND s.measured_at = (
                    SELECT max(measured_at) FROM usage_snapshots
                    WHERE scope = 'bucket' AND bucket_id = %s
              )
            """,
            (bucket_id, bucket_id),
        )
        return (int(row["bytes"]), int(row["objects"])) if row else (0, 0)

    # -- admission control -------------------------------------------------
    def reserve(
        self,
        *,
        user_id: str,
        bucket_id: str,
        nbytes: int,
        object_key: str | None = None,
    ) -> str | None:
        """Reserve ``nbytes`` or raise :class:`QuotaExceededError`.

        Returns the reservation id, or ``None`` when no policy applies.
        """
        if nbytes <= 0:
            raise ValueError("reservation size must be positive")

        user_policy = self.user_policy(user_id)
        bucket_policy = self.bucket_policy(bucket_id)

        for label, policy in (("user", user_policy), ("bucket", bucket_policy)):
            max_object = policy["max_object_bytes"]
            if max_object != -1 and nbytes > max_object:
                raise QuotaExceededError(
                    f"object is larger than the {label} maximum object size",
                    details={"limit": "max_object_bytes", "max": max_object, "requested": nbytes},
                )

        user_bytes, _ = self.usage_for_user(user_id)
        bucket_bytes, _ = self.usage_for_bucket(bucket_id)
        user_held = self.held_bytes(user_id)
        bucket_held = self.held_bytes(user_id, bucket_id)

        if user_policy["max_bytes"] != -1 and user_policy["enforced"]:
            headroom = user_policy["max_bytes"] - user_bytes - user_held
            if nbytes > headroom:
                raise QuotaExceededError(
                    "user storage quota exceeded",
                    details={
                        "limit": "user.max_bytes",
                        "max": user_policy["max_bytes"],
                        "used": user_bytes,
                        "reserved": user_held,
                        "requested": nbytes,
                    },
                )
        if bucket_policy["max_bytes"] != -1 and bucket_policy["enforced"]:
            headroom = bucket_policy["max_bytes"] - bucket_bytes - bucket_held
            if nbytes > headroom:
                raise QuotaExceededError(
                    "bucket storage quota exceeded",
                    details={
                        "limit": "bucket.max_bytes",
                        "max": bucket_policy["max_bytes"],
                        "used": bucket_bytes,
                        "reserved": bucket_held,
                        "requested": nbytes,
                    },
                )

        reservation_id = _new_uuid()
        self._db.execute(
            """
            INSERT INTO quota_reservations
                (id, user_id, bucket_id, bytes, object_key, expires_at)
            VALUES (%s, %s, %s, %s, %s, now() + (%s * interval '1 second'))
            """,
            (
                reservation_id,
                user_id,
                bucket_id,
                nbytes,
                object_key,
                RESERVATION_TTL_S,
            ),
        )
        return reservation_id

    def commit(self, reservation_id: str) -> None:
        if reservation_id is None:
            return
        self._db.execute(
            """
            UPDATE quota_reservations SET state = 'committed', settled_at = now()
            WHERE id = %s AND state = 'held'
            """,
            (reservation_id,),
        )

    def release(self, reservation_id: str) -> None:
        if reservation_id is None:
            return
        self._db.execute(
            """
            UPDATE quota_reservations SET state = 'released', settled_at = now()
            WHERE id = %s AND state = 'held'
            """,
            (reservation_id,),
        )

    def expire_stale(self) -> int:
        return self._db.execute(
            """
            UPDATE quota_reservations SET state = 'expired', settled_at = now()
            WHERE state = 'held' AND expires_at <= now()
            """
        )

    # -- reporting -----------------------------------------------------------
    def summarise(self, user_id: str, bucket_id: str | None = None) -> dict[str, Any]:
        user_policy = self.user_policy(user_id)
        user_used, user_objects = self.usage_for_user(user_id)
        summary: dict[str, Any] = {
            "user": {
                "max_bytes": user_policy["max_bytes"],
                "used_bytes": user_used,
                "reserved_bytes": self.held_bytes(user_id),
                "object_count": user_objects,
                "enforced": user_policy["enforced"],
            }
        }
        if bucket_id:
            bucket_policy = self.bucket_policy(bucket_id)
            bucket_used, bucket_objects = self.usage_for_bucket(bucket_id)
            summary["bucket"] = {
                "max_bytes": bucket_policy["max_bytes"],
                "used_bytes": bucket_used,
                "reserved_bytes": self.held_bytes(user_id, bucket_id),
                "object_count": bucket_objects,
                "enforced": bucket_policy["enforced"],
            }
        return summary


def _validate_limit(name: str, value: int) -> None:
    if value != -1 and value <= 0:
        raise ValueError(f"{name} must be -1 (unlimited) or a positive integer")


def _new_uuid() -> str:
    import uuid

    return str(uuid.uuid4())


def _aware(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return dt.datetime.min.replace(tzinfo=dt.UTC)
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)