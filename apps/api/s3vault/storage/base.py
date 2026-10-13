"""Storage-engine abstraction.

S3Vault does not implement the S3 protocol. The engine owns object bytes,
durability and the wire protocol; the application layer above this package owns
identity, policy and metadata.

Everything engine-specific lives in a concrete adapter. Callers depend only on
:class:`StorageAdapter` and on :class:`Capabilities`, so an unsupported engine
feature is reported as unsupported rather than silently emulated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class Feature(StrEnum):
    BUCKET_CREATE = "bucket.create"
    BUCKET_DELETE = "bucket.delete"
    BUCKET_LIST = "bucket.list"
    OBJECT_PUT = "object.put"
    OBJECT_GET = "object.get"
    OBJECT_DELETE = "object.delete"
    OBJECT_LIST = "object.list"
    OBJECT_COPY = "object.copy"
    RANGE_READ = "object.range_read"
    MULTIPART = "multipart"
    PRESIGN_PUT = "presign.put"
    PRESIGN_GET = "presign.get"
    VERSIONING = "bucket.versioning"
    CORS = "bucket.cors"
    LIFECYCLE = "bucket.lifecycle"
    POLICIES = "bucket.policies"
    CONDITIONAL_READ = "object.conditional_read"
    ENGINE_USAGE_METRICS = "engine.usage_metrics"
    ENGINE_LIST_PARTS = "multipart.list_parts"
    ENGINE_ABORT_MULTIPART = "multipart.abort"


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What the configured engine actually supports, as measured at runtime."""

    engine_name: str
    engine_version: str | None
    supported: frozenset[Feature]
    notes: dict[str, str] = field(default_factory=dict)

    def supports(self, feature: Feature) -> bool:
        return feature in self.supported

    def require(self, feature: Feature) -> None:
        """Raise :class:`UnsupportedFeatureError` unless ``feature`` is available."""
        from ..errors import UnsupportedFeatureError

        if feature not in self.supported:
            raise UnsupportedFeatureError(
                f"storage engine '{self.engine_name}' does not support {feature.value}",
                details={"feature": feature.value, "engine": self.engine_name},
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "engine": self.engine_name,
            "version": self.engine_version,
            "supported": sorted(f.value for f in self.supported),
            "notes": dict(self.notes),
        }


@dataclass(frozen=True, slots=True)
class BucketInfo:
    name: str
    created_at: str | None = None
    versioning: str | None = None


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    size: int
    last_modified: str | None = None
    etag: str | None = None
    content_type: str | None = None
    storage_class: str | None = None
    version_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ObjectListPage:
    objects: tuple[ObjectInfo, ...]
    common_prefixes: tuple[str, ...]
    is_truncated: bool
    next_marker: str | None = None
    key_count: int = 0


@dataclass(frozen=True, slots=True)
class MultipartUpload:
    upload_id: str
    key: str
    bucket: str
    initiated_at: str | None = None


@dataclass(frozen=True, slots=True)
class UploadedPart:
    part_number: int
    etag: str
    size: int


@dataclass(frozen=True, slots=True)
class EngineUsage:
    """Usage as reported by the engine, when it exposes it."""

    measured: bool
    bytes_used: int | None = None
    object_count: int | None = None
    capacity_bytes: int | None = None
    source: str = "unavailable"


@dataclass(frozen=True, slots=True)
class PresignedTarget:
    url: str
    method: str
    expires_at: str
    headers: dict[str, str] = field(default_factory=dict)


class StorageAdapter(Protocol):
    """Engine-facing operations used by the application layer."""

    def probe(self) -> Capabilities:
        """Measure engine capabilities. Must not raise for a reachable engine."""

    def healthy(self) -> bool: ...

    # buckets
    def create_bucket(self, bucket: str) -> None: ...
    def delete_bucket(self, bucket: str, *, force: bool = False) -> None: ...
    def bucket_exists(self, bucket: str) -> bool: ...
    def list_buckets(self) -> list[BucketInfo]: ...

    # objects
    def put_object(
        self,
        bucket: str,
        key: str,
        body: bytes | Iterator[bytes],
        *,
        content_type: str = "application/octet-stream",
        content_length: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str: ...

    def get_object(
        self, bucket: str, key: str, *, byte_range: tuple[int, int] | None = None
    ) -> tuple[dict[str, str], Iterator[bytes]]: ...

    def head_object(self, bucket: str, key: str) -> ObjectInfo | None: ...
    def delete_object(self, bucket: str, key: str) -> None: ...
    def delete_objects(self, bucket: str, keys: Sequence[str]) -> dict[str, int]: ...
    def list_objects(
        self,
        bucket: str,
        *,
        prefix: str = "",
        delimiter: str | None = None,
        max_keys: int = 1000,
        marker: str | None = None,
    ) -> ObjectListPage: ...
    def copy_object(
        self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str
    ) -> ObjectInfo: ...

    # multipart
    def create_multipart_upload(self, bucket: str, key: str) -> str: ...
    def upload_part(
        self, bucket: str, key: str, upload_id: str, part_number: int, body: bytes
    ) -> UploadedPart: ...
    def list_parts(self, bucket: str, key: str, upload_id: str) -> list[UploadedPart]: ...
    def list_multipart_uploads(self, bucket: str) -> list[MultipartUpload]: ...
    def complete_multipart_upload(
        self, bucket: str, key: str, upload_id: str, parts: Sequence[UploadedPart]
    ) -> ObjectInfo: ...
    def abort_multipart_upload(self, bucket: str, key: str, upload_id: str) -> None: ...

    # presigned
    def presign_put(
        self,
        bucket: str,
        key: str,
        *,
        expires_in: int,
        content_type: str | None = None,
        content_length: int | None = None,
    ) -> PresignedTarget: ...
    def presign_get(
        self, bucket: str, key: str, *, expires_in: int, filename: str | None = None
    ) -> PresignedTarget: ...

    # bucket configuration
    def get_versioning(self, bucket: str) -> str | None: ...
    def set_versioning(self, bucket: str, status: str) -> None: ...
    def get_cors(self, bucket: str) -> list[str]: ...
    def set_cors(self, bucket: str, origins: Sequence[str]) -> None: ...
    def get_lifecycle(self, bucket: str) -> list[dict]: ...
    def set_lifecycle(self, bucket: str, rules: Sequence[dict]) -> None: ...
    def get_policy(self, bucket: str) -> dict | None: ...
    def set_policy(self, bucket: str, policy: dict) -> None: ...

    # usage
    def usage(self) -> EngineUsage: ...


async def aiter_from_sync(iterator: Iterator[bytes], chunk: int = 65536) -> AsyncIterator[bytes]:
    """Adapt a synchronous byte iterator to an async iterator."""
    import asyncio

    while True:
        part = await asyncio.to_thread(lambda: next(iterator, None))
        if part is None:
            return
        yield part