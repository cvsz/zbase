"""Usage measurement and snapshots.

SeaweedFS does not expose storage totals over the S3 API, so S3Vault measures
usage by listing objects. Results are therefore **measured by listing**, not
read from an engine counter, and the console labels them accordingly.

Capacity is reported separately and only when the engine can supply it, so an
absent measurement is never shown as zero.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..db import Database
from ..storage.base import Feature, StorageAdapter

_LIST_PAGE = 1000


class UsageService:
    def __init__(self, db: Database, adapter: StorageAdapter) -> None:
        self._db = db
        self._adapter = adapter

    # -- measurement ------------------------------------------------------
    def measure_bucket(self, bucket: str) -> tuple[int, int]:
        """Return ``(bytes, object_count)`` for a bucket by listing its objects."""
        total_bytes = 0
        total_objects = 0
        marker: str | None = None
        while True:
            page = self._adapter.list_objects(
                bucket, max_keys=_LIST_PAGE, marker=marker
            )
            for obj in page.objects:
                total_bytes += obj.size
                total_objects += 1
            for common in page.common_prefixes:
                # Common prefixes carry no size; count them as prefixes only.
                pass
            if not page.is_truncated or not page.next_marker:
                break
            marker = page.next_marker
        return total_bytes, total_objects

    def measure_all(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for info in self._adapter.list_buckets():
            result[info.name] = dict(
                zip(("bytes", "objects"), self.measure_bucket(info.name))
            )
        return result

    # -- snapshots ---------------------------------------------------------
    def snapshot_global(self, totals: dict[str, dict[str, int]]) -> dict[str, Any]:
        measured_at = _now()
        bytes_used = sum(v["bytes"] for v in totals.values())
        object_count = sum(v["objects"] for v in totals.values())
        self._db.execute(
            """
            INSERT INTO usage_snapshots
                (scope, measured_at, storage_bytes, object_count, measured_by)
            VALUES ('global', %s, %s, %s, 'engine-listing')
            """,
            (measured_at, bytes_used, object_count),
        )
        return {
            "measured_at": measured_at.isoformat(),
            "storage_bytes": bytes_used,
            "object_count": object_count,
            "bucket_count": len(totals),
            "measurement_method": "engine-listing",
        }

    def snapshot_bucket(self, bucket_name: str, bytes_used: int, object_count: int) -> None:
        row = self._db.fetch_one("SELECT id FROM buckets WHERE name = %s", (bucket_name,))
        if row is None:
            return
        self._db.execute(
            """
            INSERT INTO usage_snapshots
                (scope, bucket_id, measured_at, storage_bytes, object_count, measured_by)
            VALUES ('bucket', %s, %s, %s, %s, 'engine-listing')
            """,
            (row["id"], _now(), bytes_used, object_count),
        )

    def snapshot_user(self, user_id: str, bytes_used: int, object_count: int) -> None:
        self._db.execute(
            """
            INSERT INTO usage_snapshots
                (scope, user_id, measured_at, storage_bytes, object_count, measured_by)
            VALUES ('user', %s, %s, %s, %s, 'engine-listing')
            """,
            (user_id, _now(), bytes_used, object_count),
        )

    # -- reporting ----------------------------------------------------------
    def latest_global(self) -> dict[str, Any] | None:
        return self._db.fetch_one(
            """
            SELECT measured_at, storage_bytes, object_count, measured_by
            FROM usage_snapshots WHERE scope = 'global'
            ORDER BY measured_at DESC LIMIT 1
            """
        )

    def latest_per_bucket(self) -> list[dict[str, Any]]:
        return self._db.fetch_all(
            """
            SELECT b.name AS bucket, s.storage_bytes, s.object_count, s.measured_at
            FROM usage_snapshots s
            JOIN buckets b ON b.id = s.bucket_id
            WHERE s.scope = 'bucket'
              AND s.measured_at = (
                    SELECT max(s2.measured_at) FROM usage_snapshots s2
                    WHERE s2.scope = 'bucket' AND s2.bucket_id = s.bucket_id
              )
            ORDER BY s.storage_bytes DESC
            """
        )

    def engine_capacity(self) -> dict[str, Any]:
        """Capacity from the engine when available, otherwise explicitly unknown."""
        caps = getattr(self._adapter, "_capabilities", None)
        if caps is not None and not caps.supports(Feature.ENGINE_USAGE_METRICS):
            return {
                "available": False,
                "reason": caps.notes.get(
                    Feature.ENGINE_USAGE_METRICS.value, "engine does not expose usage metrics"
                ),
            }
        usage = self._adapter.usage()
        if not usage.measured or usage.capacity_bytes is None:
            return {"available": False, "reason": usage.source}
        return {
            "available": True,
            "capacity_bytes": usage.capacity_bytes,
            "used_bytes": usage.bytes_used,
            "source": usage.source,
        }

    def activity(self, *, hours: int = 24) -> dict[str, int]:
        """Count audit-derived object operations in the window."""
        since = _now() - dt.timedelta(hours=hours)
        rows = self._db.fetch_all(
            """
            SELECT action, count(*) AS n
            FROM audit_events
            WHERE occurred_at >= %s AND action LIKE 'object.%%'
            GROUP BY action
            """,
            (since,),
        )
        return {str(r["action"]): int(r["n"]) for r in rows}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)