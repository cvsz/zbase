"""Bucket metadata, ownership and the object operations that depend on it.

The storage engine remains authoritative for object bytes and object existence.
This service owns the application-side metadata: who owns a bucket, what
versioning/CORS/lifecycle configuration is intended, and usage snapshots.
"""

from __future__ import annotations

from typing import Any

from ..db import Database
from ..errors import (
    ConflictError,
    NotFoundError,
    PermissionError_,
    ValidationError,
)
from ..security.tokens import is_valid_bucket_name
from ..storage.base import Capabilities, Feature, StorageAdapter

BUCKET_NAME_MAX = 63


class BucketService:
    def __init__(
        self,
        db: Database,
        adapter: StorageAdapter,
        capabilities: Capabilities,
    ) -> None:
        self._db = db
        self._adapter = adapter
        self._caps = capabilities

    # -- queries ---------------------------------------------------------
    def get(self, name: str) -> dict[str, Any]:
        row = self._db.fetch_one("SELECT * FROM buckets WHERE name = %s", (name,))
        if row is None:
            raise NotFoundError(f"bucket not found: {name}")
        return row

    def exists_in_metadata(self, name: str) -> bool:
        return self._db.fetch_one("SELECT 1 FROM buckets WHERE name = %s", (name,)) is not None

    def list_for_owner(self, owner_id: str | None, *, is_admin: bool = False) -> list[dict[str, Any]]:
        if is_admin or owner_id is None:
            rows = self._db.fetch_all(
                """
                SELECT b.*, u.username AS owner_username,
                       (lc.bucket_id IS NOT NULL) AS has_lifecycle_rules
                FROM buckets b
                JOIN users u ON u.id = b.owner_id
                LEFT JOIN bucket_lifecycle lc ON lc.bucket_id = b.id
                ORDER BY b.name ASC
                """
            )
        else:
            rows = self._db.fetch_all(
                """
                SELECT b.*, u.username AS owner_username,
                       (lc.bucket_id IS NOT NULL) AS has_lifecycle_rules
                FROM buckets b
                JOIN users u ON u.id = b.owner_id
                LEFT JOIN bucket_lifecycle lc ON lc.bucket_id = b.id
                WHERE b.owner_id = %s
                ORDER BY b.name ASC
                """,
                (owner_id,),
            )
        return rows

    def assert_can_read(self, name: str, actor) -> None:  # noqa: ANN001
        row = self.get(name)
        if actor.is_admin or str(row["owner_id"]) == actor.user_id:
            return
        raise PermissionError_(f"no access to bucket {name!r}")

    def assert_can_write(self, name: str, actor) -> None:  # noqa: ANN001
        row = self.get(name)
        if actor.is_admin or str(row["owner_id"]) == actor.user_id:
            return
        raise PermissionError_(f"no write access to bucket {name!r}")

    def assert_owner_or_admin(self, name: str, actor) -> None:  # noqa: ANN001
        row = self.get(name)
        if actor.is_admin or str(row["owner_id"]) == actor.user_id:
            return
        raise PermissionError_(f"only the owner or an administrator may modify {name!r}")

    # -- lifecycle --------------------------------------------------------
    def create(self, *, name: str, owner_id: str, actor) -> dict[str, Any]:  # noqa: ANN001
        requested = name.strip()
        # Reject rather than silently normalise: a control panel that quietly
        # renames "Documents" to "documents" causes support tickets and can
        # point clients at a different bucket than the operator intended.
        if requested != requested.lower():
            raise ValidationError(
                "bucket names must be lowercase; S3 does not accept uppercase characters",
                details={"submitted": requested, "suggested": requested.lower()},
            )
        name = requested
        if not is_valid_bucket_name(name):
            raise ValidationError(
                "bucket name must be 3-63 characters, lowercase, and may contain "
                "letters, digits, dots and hyphens (not starting or ending with a "
                "hyphen or dot, and without consecutive dots)",
                details={"bucket": name},
            )
        if self.exists_in_metadata(name):
            raise ConflictError(f"bucket already exists: {name}")
        if self._adapter.bucket_exists(name):
            # Engine has it but we do not: refuse to silently adopt it.
            raise ConflictError(
                f"a bucket named {name!r} already exists in the storage engine but is "
                "not managed by S3Vault; remove it manually or choose another name"
            )

        count_row = self._db.fetch_one(
            "SELECT count(*) AS n FROM buckets WHERE owner_id = %s", (owner_id,)
        )
        from ..config import load_config  # local import to avoid a cycle

        limit = load_config().quotas.max_buckets_per_user
        if count_row and limit != -1 and int(count_row["n"]) >= limit:
            raise ConflictError(
                f"bucket limit reached for this account ({limit})",
                details={"limit": limit},
            )

        self._adapter.create_bucket(name)
        row = self._db.fetch_one(
            """
            INSERT INTO buckets (name, owner_id, created_by, engine_confirmed_at)
            VALUES (%s, %s, %s, now())
            RETURNING *
            """,
            (name, owner_id, actor.user_id),
        )
        assert row is not None
        self._db.execute(
            "INSERT INTO bucket_lifecycle (bucket_id, rules) VALUES (%s, '[]'::jsonb)",
            (row["id"],),
        )
        return row

    def delete(self, *, name: str, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """Delete a bucket. Refuses non-empty buckets unless ``force``."""
        row = self.get(name)
        page = self._adapter.list_objects(name, max_keys=1)
        non_empty = bool(page.objects or page.common_prefixes)

        if non_empty and not force:
            raise ConflictError(
                f"bucket {name!r} is not empty",
                details={
                    "bucket": name,
                    "objects_present": True,
                    "remediation": "delete the objects, or repeat with force=true",
                },
            )

        plan = {
            "bucket": row["name"],
            "existed": True,
            "non_empty": non_empty,
            "forced": force,
            "dry_run": dry_run,
        }
        if dry_run:
            return plan

        self._adapter.delete_bucket(name, force=force)
        self._db.execute("DELETE FROM buckets WHERE id = %s", (row["id"],))
        return plan

    # -- configuration ------------------------------------------------------
    def get_versioning(self, name: str) -> dict[str, Any]:
        self.get(name)
        self._caps.require(Feature.VERSIONING)
        return {"status": self._adapter.get_versioning(name)}

    def set_versioning(self, *, name: str, status: str) -> dict[str, Any]:
        row = self.get(name)
        self._caps.require(Feature.VERSIONING)
        self._adapter.set_versioning(name, status)
        self._db.execute(
            "UPDATE buckets SET versioning_enabled = %s, updated_at = now() WHERE id = %s",
            (status == "Enabled", row["id"]),
        )
        return {"status": status}

    def get_cors(self, name: str) -> dict[str, Any]:
        self.get(name)
        if not self._caps.supports(Feature.CORS):
            return {
                "supported": False,
                "origins": [],
                "reason": self._caps.notes.get(Feature.CORS.value, "engine does not support CORS"),
            }
        return {"supported": True, "origins": self._adapter.get_cors(name)}

    def set_cors(self, *, name: str, origins: list[str]) -> dict[str, Any]:
        row = self.get(name)
        self._caps.require(Feature.CORS)
        cleaned = sorted({o.strip() for o in origins if o and o.strip()})
        for origin in cleaned:
            if not (origin == "*" or origin.startswith(("http://", "https://"))):
                raise ValidationError(
                    f"CORS origin must be '*' or an absolute http(s) URL: {origin!r}"
                )
        self._adapter.set_cors(name, cleaned)
        self._db.execute(
            "UPDATE buckets SET cors_origins = %s, updated_at = now() WHERE id = %s",
            (cleaned, row["id"]),
        )
        return {"supported": True, "origins": cleaned}

    def get_lifecycle(self, name: str) -> dict[str, Any]:
        row = self.get(name)
        stored = self._db.fetch_one(
            "SELECT rules, updated_at FROM bucket_lifecycle WHERE bucket_id = %s",
            (row["id"],),
        )
        engine_rules: list[dict] = []
        if self._caps.supports(Feature.LIFECYCLE):
            engine_rules = self._adapter.get_lifecycle(name)
        return {
            "supported": self._caps.supports(Feature.LIFECYCLE),
            "stored_rules": (stored or {}).get("rules") or [],
            "engine_rules": engine_rules,
            "reason": self._caps.notes.get(Feature.LIFECYCLE.value)
            if not self._caps.supports(Feature.LIFECYCLE)
            else None,
        }

    def set_lifecycle(self, *, name: str, rules: list[dict]) -> dict[str, Any]:
        row = self.get(name)
        self._caps.require(Feature.LIFECYCLE)
        self._adapter.set_lifecycle(name, rules)
        import json

        self._db.execute(
            """
            INSERT INTO bucket_lifecycle (bucket_id, rules, updated_at)
            VALUES (%s, %s::jsonb, now())
            ON CONFLICT (bucket_id) DO UPDATE SET rules = EXCLUDED.rules, updated_at = now()
            """,
            (row["id"], json.dumps(rules)),
        )
        return {"supported": True, "rule_count": len(rules)}

    def get_policy(self, name: str) -> dict[str, Any]:
        self.get(name)
        if not self._caps.supports(Feature.POLICIES):
            return {
                "supported": False,
                "policy": None,
                "reason": self._caps.notes.get(
                    Feature.POLICIES.value, "engine does not support bucket policies"
                ),
            }
        return {"supported": True, "policy": self._adapter.get_policy(name)}

    def set_policy(self, *, name: str, policy: dict) -> dict[str, Any]:
        self.get(name)
        self._caps.require(Feature.POLICIES)
        _validate_policy(policy)
        self._adapter.set_policy(name, policy)
        import json

        self._db.execute(
            """
            INSERT INTO bucket_policies (bucket_id, policy, updated_at)
            VALUES (%s, %s::jsonb, now())
            ON CONFLICT (bucket_id) DO UPDATE SET policy = EXCLUDED.policy, updated_at = now()
            """,
            (name and self.get(name)["id"], json.dumps(policy)),
        )
        return {"supported": True}

    # -- reconciliation ------------------------------------------------------
    def reconcile(self) -> dict[str, Any]:
        """Compare engine buckets with S3Vault metadata.

        Reports drift without changing state; safe to run from the worker.
        """
        engine = {b.name for b in self._adapter.list_buckets()}
        managed = {str(r["name"]) for r in self.list_for_owner(None)}
        return {
            "engine_bucket_count": len(engine),
            "managed_bucket_count": len(managed),
            "unmanaged_in_engine": sorted(engine - managed),
            "missing_from_engine": sorted(managed - engine),
            "checked_at": _now_iso(),
        }


def _validate_policy(policy: dict[str, Any]) -> None:
    if not isinstance(policy, dict):
        raise ValidationError("policy must be an object")
    statements = policy.get("Statement")
    if not isinstance(statements, list) or not statements:
        raise ValidationError("policy must contain a non-empty Statement list")
    for statement in statements:
        if not isinstance(statement, dict):
            raise ValidationError("each policy statement must be an object")
        if statement.get("Effect") not in {"Allow", "Deny"}:
            raise ValidationError("policy statement Effect must be 'Allow' or 'Deny'")
        for field in ("Action", "Resource"):
            value = statement.get(field)
            if isinstance(value, str):
                continue
            if not isinstance(value, list) or not value:
                raise ValidationError(f"policy statement {field} must be a string or non-empty list")


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).isoformat()