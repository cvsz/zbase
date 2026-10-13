"""In-memory storage adapter.

Used by the unit test suite and by ``S3VAULT_ENV=test`` runs so that application
logic (policy, quota accounting, request shaping) can be exercised without a
live engine. It is **not** a production storage engine and is never selected
outside tests.

Implements the same contract as the SeaweedFS adapter, including the
capability surface, so tests exercise realistic behaviour.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import threading
import uuid
from collections.abc import Iterator, Sequence

from ..errors import ConflictError, NotFoundError, ValidationError
from .base import (
    BucketInfo,
    Capabilities,
    EngineUsage,
    Feature,
    MultipartUpload,
    ObjectInfo,
    ObjectListPage,
    PresignedTarget,
    UploadedPart,
)

_UNSUPPORTED_BY_DEFAULT = frozenset(
    {
        Feature.ENGINE_USAGE_METRICS,
        Feature.LIFECYCLE,
        Feature.POLICIES,
        Feature.CONDITIONAL_READ,
    }
)


class _Entry:
    __slots__ = ("data", "content_type", "etag", "last_modified", "metadata")

    def __init__(
        self,
        data: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        self.data = data
        self.content_type = content_type
        self.metadata = metadata
        self.etag = uuid.uuid5(uuid.NAMESPACE_OID, data).hex[:32]
        self.last_modified = dt.datetime.now(dt.UTC).isoformat()


class MemoryAdapter:
    """Thread-safe, process-local object store."""

    engine_name = "memory"

    def __init__(self, config=None, *, unsupported: frozenset[Feature] = _UNSUPPORTED_BY_DEFAULT) -> None:  # noqa: ANN001
        self._lock = threading.RLock()
        self._buckets: dict[str, dict[str, _Entry]] = {}
        self._meta: dict[str, dict] = {}
        self._uploads: dict[str, dict] = {}
        self._unsupported = unsupported
        self._capabilities: Capabilities | None = None
        self.reachable = True

    # -- capabilities ------------------------------------------------
    def probe(self) -> Capabilities:
        if self._capabilities is None:
            supported = frozenset(Feature) - self._unsupported
            self._capabilities = Capabilities(
                engine_name=self.engine_name,
                engine_version="test",
                supported=supported,
                notes={f.value: "disabled in the in-memory adapter" for f in self._unsupported},
            )
        return self._capabilities

    def healthy(self) -> bool:
        return self.reachable

    def usage(self) -> EngineUsage:
        with self._lock:
            total = sum(len(e.data) for store in self._buckets.values() for e in store.values())
            count = sum(len(store) for store in self._buckets.values())
        return EngineUsage(
            measured=True, bytes_used=total, object_count=count, source="memory"
        )

    # -- buckets -----------------------------------------------------
    def create_bucket(self, bucket: str) -> None:
        with self._lock:
            if bucket in self._buckets:
                raise ConflictError(f"bucket already exists: {bucket}")
            self._buckets[bucket] = {}
            self._meta[bucket] = {"versioning": None, "cors": [], "lifecycle": [], "policy": None}

    def bucket_exists(self, bucket: str) -> bool:
        with self._lock:
            return bucket in self._buckets

    def delete_bucket(self, bucket: str, *, force: bool = False) -> None:
        with self._lock:
            if bucket not in self._buckets:
                raise NotFoundError(f"bucket does not exist: {bucket}")
            if not force and self._buckets[bucket]:
                raise ConflictError("bucket is not empty")
            self._buckets.pop(bucket, None)
            self._meta.pop(bucket, None)

    def list_buckets(self) -> list[BucketInfo]:
        with self._lock:
            return [
                BucketInfo(name=name, created_at=self._meta[name].get("created_at"))
                for name in sorted(self._buckets)
            ]

    # -- objects -----------------------------------------------------
    def _store(self, bucket: str) -> dict[str, _Entry]:
        store = self._buckets.get(bucket)
        if store is None:
            raise NotFoundError(f"bucket does not exist: {bucket}")
        return store

    def put_object(
        self,
        bucket: str,
        key: str,
        body: bytes | Iterator[bytes],
        *,
        content_type: str = "application/octet-stream",
        content_length: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        if not isinstance(body, (bytes, bytearray)):
            body = b"".join(body)
        data = bytes(body)
        if content_length is not None and len(data) != content_length:
            raise ValidationError("content-length does not match the supplied body")
        with self._lock:
            entry = _Entry(data, content_type, dict(metadata or {}))
            self._store(bucket)[key] = entry
            return entry.etag

    def get_object(
        self, bucket: str, key: str, *, byte_range: tuple[int, int] | None = None
    ) -> tuple[dict[str, str], Iterator[bytes]]:
        with self._lock:
            entry = self._store(bucket).get(key)
            if entry is None:
                raise NotFoundError(f"object does not exist: {key}")
            payload = entry.data
            headers = {
                "content-type": entry.content_type,
                "content-length": str(len(payload)),
                "etag": entry.etag,
                "last-modified": entry.last_modified,
            }
        if byte_range is not None:
            start, end = byte_range
            if start >= len(payload):
                raise ValidationError("range start is beyond the end of the object")
            payload = payload[start : end + 1]
            headers["content-length"] = str(len(payload))
            headers["content-range"] = f"bytes {start}-{start + len(payload) - 1}"
        return headers, iter([payload])

    def head_object(self, bucket: str, key: str) -> ObjectInfo | None:
        with self._lock:
            entry = self._store(bucket).get(key)
            if entry is None:
                return None
            return ObjectInfo(
                key=key,
                size=len(entry.data),
                last_modified=entry.last_modified,
                etag=entry.etag,
                content_type=entry.content_type,
                metadata=dict(entry.metadata),
            )

    def delete_object(self, bucket: str, key: str) -> None:
        with self._lock:
            self._store(bucket).pop(key, None)

    def delete_objects(self, bucket: str, keys: Sequence[str]) -> dict[str, int]:
        deleted = 0
        with self._lock:
            store = self._store(bucket)
            for key in keys:
                if store.pop(key, None) is not None:
                    deleted += 1
        return {"deleted": deleted, "errors": 0}

    def list_objects(
        self,
        bucket: str,
        *,
        prefix: str = "",
        delimiter: str | None = None,
        max_keys: int = 1000,
        marker: str | None = None,
    ) -> ObjectListPage:
        with self._lock:
            store = self._store(bucket)
            keys = sorted(k for k in store if k.startswith(prefix or ""))

        start = 0
        if marker:
            start = next((i for i, k in enumerate(keys) if k > marker), len(keys))
        window = keys[start : start + max_keys]
        truncated = (start + len(window)) < len(keys)

        objects: list[ObjectInfo] = []
        prefixes: list[str] = []
        seen_prefixes: set[str] = set()
        for key in window:
            info = self.head_object(bucket, key)
            assert info is not None
            if delimiter:
                remainder = key[len(prefix) :] if prefix else key
                idx = remainder.find(delimiter)
                if idx != -1:
                    common = (prefix or "") + remainder[: idx + 1]
                    if common not in seen_prefixes:
                        seen_prefixes.add(common)
                        prefixes.append(common.rstrip("/"))
                    continue
            objects.append(info)

        return ObjectListPage(
            objects=tuple(objects),
            common_prefixes=tuple(prefixes),
            is_truncated=truncated,
            next_marker=window[-1] if truncated and window else None,
            key_count=len(objects) + len(prefixes),
        )

    def copy_object(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> ObjectInfo:
        with self._lock:
            source = self._buckets.get(src_bucket, {}).get(src_key)
            if source is None:
                raise NotFoundError(f"object does not exist: {src_key}")
            entry = _Entry(source.data, source.content_type, dict(source.metadata))
            self._store(dst_bucket)[dst_key] = entry
            return ObjectInfo(
                key=dst_key,
                size=len(entry.data),
                last_modified=entry.last_modified,
                etag=entry.etag,
                content_type=entry.content_type,
            )

    # -- multipart ---------------------------------------------------
    def create_multipart_upload(self, bucket: str, key: str) -> str:
        with self._lock:
            self._store(bucket)
            upload_id = uuid.uuid4().hex
            self._uploads[upload_id] = {
                "bucket": bucket,
                "key": key,
                "parts": {},
                "created": dt.datetime.now(dt.UTC).isoformat(),
            }
            return upload_id

    def _upload(self, upload_id: str) -> dict:
        state = self._uploads.get(upload_id)
        if state is None:
            raise NotFoundError(f"multipart upload does not exist: {upload_id}")
        return state

    def upload_part(
        self, bucket: str, key: str, upload_id: str, part_number: int, body: bytes
    ) -> UploadedPart:
        with self._lock:
            state = self._upload(upload_id)
            if state["bucket"] != bucket or state["key"] != key:
                raise ValidationError("multipart upload does not match bucket/key")
            if not 1 <= part_number <= 10_000:
                raise ValidationError("part number must be between 1 and 10000")
            etag = uuid.uuid5(uuid.NAMESPACE_OID, body).hex[:32]
            state["parts"][part_number] = (body, etag)
            return UploadedPart(part_number=part_number, etag=etag, size=len(body))

    def list_parts(self, bucket: str, key: str, upload_id: str) -> list[UploadedPart]:
        with self._lock:
            state = self._upload(upload_id)
            return [
                UploadedPart(part_number=n, etag=etag, size=len(body))
                for n, (body, etag) in sorted(state["parts"].items())
            ]

    def list_multipart_uploads(self, bucket: str) -> list[MultipartUpload]:
        with self._lock:
            return [
                MultipartUpload(
                    upload_id=uid,
                    key=state["key"],
                    bucket=state["bucket"],
                    initiated_at=state["created"],
                )
                for uid, state in self._uploads.items()
                if state["bucket"] == bucket
            ]

    def complete_multipart_upload(
        self, bucket: str, key: str, upload_id: str, parts: Sequence[UploadedPart]
    ) -> ObjectInfo:
        with self._lock:
            state = self._upload(upload_id)
            numbers = [p.part_number for p in parts]
            if numbers != sorted(numbers):
                raise ValidationError("parts must be listed in ascending part order")
            if numbers != list(range(1, len(numbers) + 1)):
                raise ValidationError("parts must be contiguous starting at 1")
            chunks: list[bytes] = []
            for part in sorted(parts, key=lambda p: p.part_number):
                stored = state["parts"].get(part.part_number)
                if stored is None:
                    raise ValidationError(f"part {part.part_number} was never uploaded")
                if stored[1] != part.etag:
                    raise ValidationError(f"etag mismatch for part {part.part_number}")
                chunks.append(stored[0])
            payload = b"".join(chunks)
            entry = _Entry(payload, "application/octet-stream", {})
            self._store(bucket)[key] = entry
            self._uploads.pop(upload_id, None)
            return ObjectInfo(
                key=key,
                size=len(payload),
                etag=entry.etag,
                last_modified=entry.last_modified,
            )

    def abort_multipart_upload(self, bucket: str, key: str, upload_id: str) -> None:
        with self._lock:
            self._upload(upload_id)
            self._uploads.pop(upload_id, None)

    # -- presigned ----------------------------------------------------
    def presign_put(
        self,
        bucket: str,
        key: str,
        *,
        expires_in: int,
        content_type: str | None = None,
        content_length: int | None = None,
    ) -> PresignedTarget:
        if not self.bucket_exists(bucket):
            raise NotFoundError(f"bucket does not exist: {bucket}")
        return PresignedTarget(
            url=f"memory://{bucket}/{key}?op=put&expires={expires_in}",
            method="PUT",
            expires_at=(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=expires_in)).isoformat(),
            headers={"Content-Type": content_type} if content_type else {},
        )

    def presign_get(
        self, bucket: str, key: str, *, expires_in: int, filename: str | None = None
    ) -> PresignedTarget:
        if not self.bucket_exists(bucket):
            raise NotFoundError(f"bucket does not exist: {bucket}")
        url = f"memory://{bucket}/{key}?op=get&expires={expires_in}"
        if filename:
            url += f"&name={filename}"
        return PresignedTarget(
            url=url,
            method="GET",
            expires_at=(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=expires_in)).isoformat(),
        )

    # -- configuration -------------------------------------------------
    def get_versioning(self, bucket: str) -> str | None:
        with self._lock:
            return self._meta.get(bucket, {}).get("versioning")

    def set_versioning(self, bucket: str, status: str) -> None:
        if status not in {"Enabled", "Suspended"}:
            raise ValidationError("versioning status must be 'Enabled' or 'Suspended'")
        with self._lock:
            if bucket not in self._meta:
                raise NotFoundError(f"bucket does not exist: {bucket}")
            self._meta[bucket]["versioning"] = status

    def get_cors(self, bucket: str) -> list[str]:
        with self._lock:
            return list(self._meta.get(bucket, {}).get("cors") or [])

    def set_cors(self, bucket: str, origins: Sequence[str]) -> None:
        with self._lock:
            if bucket not in self._meta:
                raise NotFoundError(f"bucket does not exist: {bucket}")
            self._meta[bucket]["cors"] = list(origins)

    def get_lifecycle(self, bucket: str) -> list[dict]:
        if Feature.LIFECYCLE in self._unsupported:
            return []
        with self._lock:
            return list(self._meta.get(bucket, {}).get("lifecycle") or [])

    def set_lifecycle(self, bucket: str, rules: Sequence[dict]) -> None:
        self.probe().require(Feature.LIFECYCLE)
        with self._lock:
            if bucket not in self._meta:
                raise NotFoundError(f"bucket does not exist: {bucket}")
            self._meta[bucket]["lifecycle"] = [dict(r) for r in rules]

    def get_policy(self, bucket: str) -> dict | None:
        if Feature.POLICIES in self._unsupported:
            return None
        with self._lock:
            return self._meta.get(bucket, {}).get("policy")

    def set_policy(self, bucket: str, policy: dict) -> None:
        self.probe().require(Feature.POLICIES)
        with self._lock:
            if bucket not in self._meta:
                raise NotFoundError(f"bucket does not exist: {bucket}")
            self._meta[bucket]["policy"] = policy

    # -- test helpers -------------------------------------------------
    def glob(self, bucket: str, pattern: str) -> list[str]:
        """Return keys in ``bucket`` matching a shell-style pattern (test helper)."""
        with self._lock:
            return sorted(k for k in self._buckets.get(bucket, {}) if fnmatch.fnmatch(k, pattern))