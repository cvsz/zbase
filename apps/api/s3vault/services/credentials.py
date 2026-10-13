"""S3 API credential lifecycle.

Secret access keys are stored only as AES-256-GCM envelopes under the
key-encryption key. The plaintext is returned exactly once, in the response to
the create/rotate call, and is never readable afterwards.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..db import Database
from ..errors import ConflictError, NotFoundError, ValidationError
from ..security.envelope import DecryptionError, load_kek, open_sealed, seal
from ..security.tokens import (
    is_valid_bucket_name,
    key_matches_prefix,
    new_access_key_id,
    new_secret_access_key,
    utcnow,
)

VALID_PERMISSIONS = frozenset({"read", "write", "readwrite"})


class CredentialService:
    def __init__(self, db: Database, *, kek: bytes | None = None) -> None:
        self._db = db
        self._kek = kek

    @property
    def kek(self) -> bytes:
        return self._kek if self._kek is not None else load_kek()

    # -- helpers ------------------------------------------------------
    @staticmethod
    def _normalise_permissions(permissions: list[str]) -> list[str]:
        cleaned = sorted({p.strip().lower() for p in permissions if p and p.strip()})
        bad = set(cleaned) - VALID_PERMISSIONS
        if bad:
            raise ValidationError(
                "unsupported permission(s): " + ", ".join(sorted(bad)),
                details={"allowed": sorted(VALID_PERMISSIONS)},
            )
        if not cleaned:
            raise ValidationError("at least one permission is required")
        if "readwrite" in cleaned:
            cleaned = sorted(VALID_PERMISSIONS)
        return cleaned

    @staticmethod
    def _normalise_scope(buckets: list[str] | None, prefix: str | None) -> list[str]:
        if not buckets:
            return []
        cleaned = sorted({b.strip() for b in buckets if b and b.strip()})
        for bucket in cleaned:
            if not is_valid_bucket_name(bucket):
                raise ValidationError(f"invalid bucket name in scope: {bucket!r}")
        return cleaned

    # -- create --------------------------------------------------------
    def create(
        self,
        *,
        user_id: str,
        permissions: list[str],
        bucket_scope: list[str] | None = None,
        prefix_scope: str | None = None,
        description: str = "",
        expires_at: dt.datetime | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        perms = self._normalise_permissions(permissions)
        scope = self._normalise_scope(bucket_scope, prefix_scope)
        if prefix_scope and not scope:
            raise ValidationError("a prefix scope requires a bucket scope")
        if expires_at is not None:
            expires = _aware(expires_at)
            if expires <= utcnow():
                raise ValidationError("expires_at must be in the future")

        access_key_id = new_access_key_id()
        secret_access_key = new_secret_access_key()
        credential_id = _new_uuid()

        sealed = seal(
            secret_access_key,
            credential_id=credential_id,
            access_key_id=access_key_id,
            kek=self.kek,
        )

        try:
            self._db.execute(
                """
                INSERT INTO credentials
                    (id, user_id, access_key_id, secret_ciphertext, description,
                     bucket_scope, prefix_scope, permissions, expires_at, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    credential_id,
                    user_id,
                    access_key_id,
                    sealed.blob,
                    description[:200],
                    scope,
                    prefix_scope or None,
                    perms,
                    expires_at,
                    created_by or user_id,
                ),
            )
        except Exception as exc:  # unique violation on a (astronomically unlikely) key clash
            raise ConflictError("could not allocate a unique access key id; retry") from exc

        # Plaintext is returned here and never again.
        return {
            "id": credential_id,
            "user_id": user_id,
            "access_key_id": access_key_id,
            "secret_access_key": secret_access_key,
            "permissions": perms,
            "bucket_scope": scope,
            "prefix_scope": prefix_scope or None,
            "description": description[:200],
            "expires_at": expires_at,
        }

    # -- read -----------------------------------------------------------
    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return self._db.fetch_all(
            """
            SELECT id, user_id, access_key_id, description, bucket_scope,
                   prefix_scope, permissions, expires_at, revoked_at,
                   last_used_at, created_at
            FROM credentials
            WHERE user_id = %s AND revoked_at IS NULL
            ORDER BY created_at DESC
            """,
            (user_id,),
        )

    def list_all(self, *, limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        total_row = self._db.fetch_one(
            "SELECT count(*) AS n FROM credentials WHERE revoked_at IS NULL"
        )
        rows = self._db.fetch_all(
            """
            SELECT c.id, c.user_id, c.access_key_id, c.description, c.bucket_scope,
                   c.prefix_scope, c.permissions, c.expires_at, c.revoked_at,
                   c.last_used_at, c.created_at, u.username AS username
            FROM credentials c JOIN users u ON u.id = c.user_id
            WHERE c.revoked_at IS NULL
            ORDER BY c.created_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        return rows, int(total_row["n"]) if total_row else 0

    def get(self, credential_id: str) -> dict[str, Any]:
        row = self._db.fetch_one(
            """
            SELECT id, user_id, access_key_id, description, bucket_scope,
                   prefix_scope, permissions, expires_at, revoked_at,
                   last_used_at, created_at
            FROM credentials WHERE id = %s
            """,
            (credential_id,),
        )
        if row is None:
            raise NotFoundError("credential not found")
        return row

    # -- lifecycle -------------------------------------------------------
    def revoke(self, credential_id: str) -> None:
        row = self.get(credential_id)
        if row["revoked_at"] is not None:
            return
        self._db.execute(
            "UPDATE credentials SET revoked_at = now() WHERE id = %s", (credential_id,)
        )

    def rotate(
        self, credential_id: str, *, user_id: str, created_by: str | None = None
    ) -> dict[str, Any]:
        """Revoke a credential and issue its replacement, preserving scope."""
        original = self.get(credential_id)
        if original["revoked_at"] is not None:
            raise ConflictError("credential is already revoked")

        replacement = self.create(
            user_id=user_id,
            permissions=list(original["permissions"]),
            bucket_scope=list(original["bucket_scope"] or []),
            prefix_scope=original["prefix_scope"],
            description=original["description"],
            expires_at=original["expires_at"],
            created_by=created_by or user_id,
        )
        self._db.execute(
            "UPDATE credentials SET revoked_at = now(), rotated_from = %s WHERE id = %s",
            (replacement["id"], credential_id),
        )
        return replacement

    def touch(self, access_key_id: str) -> None:
        self._db.execute(
            "UPDATE credentials SET last_used_at = now() WHERE access_key_id = %s",
            (access_key_id,),
        )

    def revoke_for_user(self, user_id: str) -> int:
        return self._db.execute(
            "UPDATE credentials SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        )

    # -- resolution for the signing proxy ---------------------------------
    def resolve_secret(self, access_key_id: str) -> dict[str, Any]:
        """Return the full credential row including the decrypted secret.

        Raises:
            NotFoundError: unknown, revoked, or expired credential.
        """
        row = self._db.fetch_one(
            """
            SELECT id, user_id, access_key_id, secret_ciphertext, bucket_scope,
                   prefix_scope, permissions, expires_at, revoked_at
            FROM credentials WHERE access_key_id = %s
            """,
            (access_key_id,),
        )
        if row is None:
            raise NotFoundError("unknown access key")
        if row["revoked_at"] is not None:
            raise NotFoundError("access key has been revoked")
        if row["expires_at"] is not None and _aware(row["expires_at"]) <= utcnow():
            raise NotFoundError("access key has expired")

        try:
            secret = open_sealed(
                row["secret_ciphertext"],
                credential_id=str(row["id"]),
                access_key_id=str(row["access_key_id"]),
                kek=self.kek,
            )
        except DecryptionError as exc:
            # Never leak engine/DB detail; the operator needs the KEK intact.
            raise ConflictError(
                "stored credential secret cannot be decrypted; verify KEK_BASE64/KEK_FILE"
            ) from exc

        self.touch(access_key_id)
        return {
            "id": str(row["id"]),
            "user_id": str(row["user_id"]),
            "access_key_id": str(row["access_key_id"]),
            "secret_access_key": secret,
            "bucket_scope": list(row["bucket_scope"] or []),
            "prefix_scope": row["prefix_scope"],
            "permissions": list(row["permissions"]),
        }

    # -- authorization ------------------------------------------------------
    @staticmethod
    def is_allowed(
        credential: dict[str, Any], *, action: str, bucket: str, key: str | None = None
    ) -> bool:
        """Check a resolved credential against its scope and permissions.

        ``action`` is ``"read"`` or ``"write"``. ``readwrite`` implies both.
        """
        granted = set(credential["permissions"])
        if "readwrite" in granted:
            granted |= {"read", "write"}
        if action not in granted:
            return False

        scope = credential.get("bucket_scope") or []
        if scope and bucket not in scope:
            return False
        prefix = credential.get("prefix_scope")
        if prefix and key is not None and not key_matches_prefix(key, prefix):
            return False
        if prefix and key is None:
            # Bucket-level listing must still be constrained to the prefix scope.
            return False
        return True


def _new_uuid() -> str:
    import uuid

    return str(uuid.uuid4())


def _aware(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return dt.datetime.min.replace(tzinfo=dt.UTC)
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)