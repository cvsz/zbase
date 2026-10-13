"""Storage adapters for S3Vault."""

from .base import (
    BucketInfo,
    Capabilities,
    EngineUsage,
    Feature,
    MultipartUpload,
    ObjectInfo,
    ObjectListPage,
    PresignedTarget,
    StorageAdapter,
    UploadedPart,
)

__all__ = [
    "BucketInfo",
    "Capabilities",
    "EngineUsage",
    "Feature",
    "MultipartUpload",
    "ObjectInfo",
    "ObjectListPage",
    "PresignedTarget",
    "StorageAdapter",
    "UploadedPart",
    "build_adapter",
]


def build_adapter(config) -> StorageAdapter:  # noqa: ANN001 - avoids import cycle
    """Instantiate the adapter selected by ``STORAGE_ENGINE``.

    Keeping the choice behind a factory means the application layer never
    imports a concrete engine module directly.
    """
    import os

    engine = os.environ.get("STORAGE_ENGINE", "seaweedfs").strip().lower()
    if engine == "seaweedfs":
        from .seaweedfs import SeaweedFSAdapter

        return SeaweedFSAdapter(config)
    if engine == "memory":
        from .memory import MemoryAdapter

        return MemoryAdapter(config)
    raise ValueError(
        f"unknown STORAGE_ENGINE={engine!r}; supported: seaweedfs, memory"
    )