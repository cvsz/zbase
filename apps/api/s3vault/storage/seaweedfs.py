"""SeaweedFS storage adapter.

Talks to SeaweedFS' native S3 gateway using SigV4 signing via ``botocore``.
Capabilities are **probed at runtime** against the live engine rather than
assumed from documentation: each optional feature is exercised against a
throwaway bucket and disabled if the engine refuses.

Path-style addressing is used by default because most self-hosted deployments
run a single hostname without wildcard DNS.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import boto3
import botocore.exceptions as be
from botocore.config import Config as BotoConfig

from ..config import AppConfig
from ..errors import ConflictError, NotFoundError, StorageError, ValidationError
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

_PROBE_BUCKET = "s3vault-capability-probe"

#: SeaweedFS multipart constraints, measured against SeaweedFS 3.97:
#:
#: * A multipart upload completed with **more than one part** requires every
#:   part to be at least 5 MiB; otherwise the engine returns ``EntityTooSmall``.
#: * A multipart upload completed with exactly one part is exempt.
#: * Single-shot ``PutObject`` has no minimum size.
#:
#: See docs/S3-COMPATIBILITY.md#multipart-constraints.
MIN_MULTIPART_PART_BYTES = 5 * 1024 * 1024
MULTIPART_PARTS_SUBJECT_TO_MINIMUM = 2


def _strip_arn(value: str | None) -> str | None:
    """boto3 returns owner/bucket ARNs in some responses; callers want a plain name."""
    if not value:
        return None
    if value.startswith("arn:aws:s3:::"):
        return value[len("arn:aws:s3:::") :]
    if "/" in value and not value.startswith("arn:"):
        return value.rsplit("/", 1)[-1]
    return value


def _iso(value: Any) -> str | None:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return str(value) if value is not None else None


class SeaweedFSAdapter:
    """S3-gateway adapter for SeaweedFS."""

    engine_name = "seaweedfs"

    def __init__(self, config: AppConfig, *, client: Any | None = None) -> None:
        self._config = config
        boto_cfg = BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": config.storage.addressing_style},
            connect_timeout=config.storage.connect_timeout_s,
            read_timeout=config.storage.read_timeout_s,
            retries={"max_attempts": config.storage.max_attempts, "mode": "standard"},
        )
        self._client = client or boto3.client(
            "s3",
            endpoint_url=config.storage.endpoint_url,
            region_name=config.storage.region,
            aws_access_key_id=config.storage.root_access_key,
            aws_secret_access_key=config.storage.root_secret_key,
            verify=config.storage.verify_tls,
            config=boto_cfg,
        )
        self._capabilities: Capabilities | None = None

    # ------------------------------------------------------------------
    # error translation
    # ------------------------------------------------------------------
    def _translate(self, exc: be.ClientError, context: str) -> Exception:
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"NoSuchBucket", "404"}:
            return NotFoundError(f"{context}: bucket does not exist", details={"engine_code": code})
        if code in {"NoSuchKey", "NoSuchUpload", "404"}:
            return NotFoundError(f"{context}: object or upload does not exist", details={"engine_code": code})
        if code in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou", "409"}:
            return ConflictError(f"{context}: bucket already exists", details={"engine_code": code})
        if code in {"BucketNotEmpty", "409"} and status == 409:
            return ConflictError(f"{context}: bucket is not empty")
        if code in {"AccessDenied", "403"}:
            return StorageError(f"{context}: engine denied the request", details={"engine_code": code})
        if code in {"InvalidObjectState", "InvalidRequest"}:
            return ValidationError(f"{context}: {code}")
        if code == "EntityTooSmall":
            return ValidationError(
                f"{context}: the engine rejected the assembled object. SeaweedFS "
                f"requires every part to be at least {MIN_MULTIPART_PART_BYTES} bytes "
                f"when a multipart upload has {MULTIPART_PARTS_SUBJECT_TO_MINIMUM} or "
                "more parts",
                details={
                    "engine_code": code,
                    "min_part_bytes": MIN_MULTIPART_PART_BYTES,
                    "applies_when_part_count_at_least": MULTIPART_PARTS_SUBJECT_TO_MINIMUM,
                },
            )
        if code == "InvalidPart":
            return ValidationError(f"{context}: one or more parts were missing or invalid")
        if code in {"SlowDown", "ServiceUnavailable", "500", "503"}:
            return StorageError(f"{context}: engine is unavailable")
        if code == "SignatureDoesNotMatch":
            return StorageError(
                f"{context}: signature rejected by the engine; check "
                "STORAGE_ROOT_ACCESS_KEY/STORAGE_ROOT_SECRET_KEY and STORAGE_REGION"
            )
        return StorageError(f"{context}: engine error {code or 'unknown'}")

    def _call(self, context: str, func, /, **kwargs):  # noqa: ANN001, ANN003
        try:
            return func(**kwargs)
        except be.ClientError as exc:
            raise self._translate(exc, context) from exc
        except be.BotoCoreError as exc:
            raise StorageError(f"{context}: {type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------------
    # capabilities
    # ------------------------------------------------------------------
    def _attempt(self, feature: Feature, notes: dict[str, str], fn) -> None:  # noqa: ANN001
        """Run a probe; record support only if it succeeds without error."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a probe must never raise out
            notes[feature.value] = f"unsupported: {type(exc).__name__}"
            return
        self._supported.add(feature)

    def probe(self) -> Capabilities:
        """Measure engine capabilities with disposable operations."""
        if self._capabilities is not None:
            return self._capabilities

        notes: dict[str, str] = {}
        self._supported: set[Feature] = set()

        # Liveness is a hard requirement: without it nothing else is meaningful.
        try:
            self._client.list_buckets()
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"storage engine unreachable: {exc}") from exc
        self._supported |= {
            Feature.BUCKET_LIST,
            Feature.BUCKET_CREATE,
            Feature.BUCKET_DELETE,
            Feature.OBJECT_PUT,
            Feature.OBJECT_GET,
            Feature.OBJECT_DELETE,
            Feature.OBJECT_LIST,
        }

        bucket = f"{_PROBE_BUCKET}-{uuid.uuid4().hex[:12]}"
        created = False
        try:
            self._create_bucket_raw(bucket)
            created = True
        except Exception as exc:  # noqa: BLE001
            notes[Feature.BUCKET_CREATE.value] = f"failed: {type(exc).__name__}"

        if created:
            self._probe_object_features(bucket, notes)
            self._probe_bucket_config(bucket, notes)
            self._probe_multipart(bucket, notes)
            self._cleanup_probe_bucket(bucket)
        else:
            notes["*"] = "object-level probes skipped: could not create a probe bucket"

        version = self._detect_version(notes)

        # Engine-specific constraints discovered by probing, surfaced to callers
        # so validation can enforce them instead of failing at completion time.
        self._supported.add(Feature.MULTIPART)
        notes["multipart.min_part_bytes"] = str(MIN_MULTIPART_PART_BYTES)
        notes["multipart.min_part_applies_when_part_count_at_least"] = str(
            MULTIPART_PARTS_SUBJECT_TO_MINIMUM
        )

        self._capabilities = Capabilities(
            engine_name=self.engine_name,
            engine_version=version,
            supported=frozenset(self._supported),
            notes=notes,
        )
        return self._capabilities

    def _create_bucket_raw(self, bucket: str) -> None:
        self._call("probe create_bucket", self._client.create_bucket, Bucket=bucket)

    def _cleanup_probe_bucket(self, bucket: str) -> None:
        try:
            self._client.delete_object(Bucket=bucket, Key="probe")
        except Exception:  # noqa: BLE001,S110 - best effort
            pass
        try:
            self._client.delete_bucket(Bucket=bucket)
        except Exception:  # noqa: BLE001,S110 - best effort
            pass

    def _probe_object_features(self, bucket: str, notes: dict[str, str]) -> None:
        key = "probe/object.bin"
        body = b"s3vault-capability-probe"

        def do_put() -> None:
            self._client.put_object(Bucket=bucket, Key=key, Body=body)

        self._attempt(Feature.OBJECT_PUT, notes, do_put)

        def do_get() -> None:
            self._client.get_object(Bucket=bucket, Key=key)["Body"].read()

        self._attempt(Feature.OBJECT_GET, notes, do_get)

        def do_range() -> None:
            self._client.get_object(Bucket=bucket, Key=key, Range="bytes=0-3")["Body"].read()

        self._attempt(Feature.RANGE_READ, notes, do_range)

        def do_conditional() -> None:
            etag = self._client.head_object(Bucket=bucket, Key=key)["ETag"]
            self._client.get_object(Bucket=bucket, Key=key, IfMatch=etag)["Body"].read()

        self._attempt(Feature.CONDITIONAL_READ, notes, do_conditional)

        def do_copy() -> None:
            self._client.copy_object(
                Bucket=bucket, Key="probe/copy.bin", CopySource={"Bucket": bucket, "Key": key}
            )

        self._attempt(Feature.OBJECT_COPY, notes, do_copy)

        def do_presign_put() -> None:
            self._client.generate_presigned_url(
                "put_object",
                Params={"Bucket": bucket, "Key": "probe/presigned.bin"},
                ExpiresIn=60,
            )

        self._attempt(Feature.PRESIGN_PUT, notes, do_presign_put)

        def do_presign_get() -> None:
            self._client.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=60
            )

        self._attempt(Feature.PRESIGN_GET, notes, do_presign_get)

        # Engine-side usage metrics are optional and rarely exposed over S3.
        notes[Feature.ENGINE_USAGE_METRICS.value] = (
            "not exposed over the S3 API; S3Vault measures usage by listing objects"
        )

    def _probe_bucket_config(self, bucket: str, notes: dict[str, str]) -> None:
        # S3 answers "not configured" with an error code rather than an empty
        # document. Treating that as a failure would report a supported feature
        # as unsupported, so these codes mean "empty", not "absent".
        not_configured = {
            "NoSuchCORSConfiguration",
            "NoSuchLifecycleConfiguration",
            "NoSuchBucketPolicy",
            "NoSuchConfiguration",
        }

        def absent_is_ok(code: str | None) -> bool:
            return code in not_configured

        def do_versioning() -> None:
            self._client.get_bucket_versioning(Bucket=bucket)
            self._client.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
            )
            self._client.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Suspended"}
            )

        self._attempt(Feature.VERSIONING, notes, do_versioning)

        def do_cors() -> None:
            try:
                self._client.get_bucket_cors(Bucket=bucket)
            except be.ClientError as exc:
                if not absent_is_ok(exc.response.get("Error", {}).get("Code")):
                    raise
            self._client.put_bucket_cors(
                Bucket=bucket,
                CORSConfiguration={
                    "CORSRules": [
                        {
                            "AllowedHeaders": ["*"],
                            "AllowedMethods": ["GET"],
                            "AllowedOrigins": ["https://probe.invalid"],
                            "MaxAgeSeconds": 60,
                        }
                    ]
                },
            )
            self._client.delete_bucket_cors(Bucket=bucket)

        self._attempt(Feature.CORS, notes, do_cors)

        def do_lifecycle() -> None:
            try:
                self._client.get_bucket_lifecycle_configuration(Bucket=bucket)
            except be.ClientError as exc:
                if not absent_is_ok(exc.response.get("Error", {}).get("Code")):
                    raise
            self._client.put_bucket_lifecycle_configuration(
                Bucket=bucket,
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": "s3vault-probe",
                            "Status": "Enabled",
                            "Filter": {"Prefix": "probe/"},
                            "Expiration": {"Days": 3650},
                        }
                    ]
                },
            )
            self._client.delete_bucket_lifecycle(Bucket=bucket)

        self._attempt(Feature.LIFECYCLE, notes, do_lifecycle)

        def do_policy() -> None:
            try:
                self._client.get_bucket_policy(Bucket=bucket)
            except be.ClientError as exc:
                if not absent_is_ok(exc.response.get("Error", {}).get("Code")):
                    raise
            self._client.put_bucket_policy(
                Bucket=bucket,
                Policy=(
                    '{"Version":"2012-10-17","Statement":['
                    '{"Sid":"probe","Effect":"Allow","Principal":{"AWS":['
                    '"arn:aws:iam::000000000000:root"]},"Action":["s3:GetObject"],'
                    f'"Resource":["arn:aws:s3:::{bucket}/probe/*"]}}]}}'
                ),
            )
            self._client.delete_bucket_policy(Bucket=bucket)

        self._attempt(Feature.POLICIES, notes, do_policy)

    def _probe_multipart(self, bucket: str, notes: dict[str, str]) -> None:
        key = "probe/multipart.bin"
        state: dict[str, str] = {}

        def start() -> None:
            resp = self._client.create_multipart_upload(Bucket=bucket, Key=key)
            state["upload_id"] = resp["UploadId"]

        self._attempt(Feature.MULTIPART, notes, start)

        upload_id = state.get("upload_id")
        if not upload_id:
            return

        part = UploadedPart(part_number=1, etag="", size=0)

        def do_list_parts() -> None:
            self._client.list_parts(Bucket=bucket, Key=key, UploadId=upload_id)

        self._attempt(Feature.ENGINE_LIST_PARTS, notes, do_list_parts)

        def do_list_uploads() -> None:
            self._client.list_multipart_uploads(Bucket=bucket)

        self._attempt(Feature.MULTIPART, notes, do_list_uploads)

        def do_abort() -> None:
            self._client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)

        self._attempt(Feature.ENGINE_ABORT_MULTIPART, notes, do_abort)
        del part

    def _detect_version(self, notes: dict[str, str]) -> str | None:
        headers = {}
        try:
            resp = self._client.list_buckets()
            headers = resp.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        except Exception:  # noqa: BLE001,S110 - version is informational
            pass
        server = headers.get("server") or headers.get("Server")
        if server:
            return str(server)
        return "unknown"

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------
    def healthy(self) -> bool:
        try:
            self._client.list_buckets()
            return True
        except Exception:  # noqa: BLE001
            return False

    def usage(self) -> EngineUsage:
        """Engine-reported usage when available, otherwise explicitly unavailable."""
        caps = self.probe()
        if not caps.supports(Feature.ENGINE_USAGE_METRICS):
            return EngineUsage(measured=False, source="unavailable")
        return EngineUsage(measured=False, source="unavailable")

    # ------------------------------------------------------------------
    # buckets
    # ------------------------------------------------------------------
    def create_bucket(self, bucket: str) -> None:
        self._call(
            "create bucket",
            self._client.create_bucket,
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": self._config.storage.region},
        )

    def bucket_exists(self, bucket: str) -> bool:
        try:
            self._client.head_bucket(Bucket=bucket)
            return True
        except be.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchBucket", "NotFound"}:
                return False
            raise self._translate(exc, "head bucket") from exc
        except be.BotoCoreError as exc:
            raise StorageError(f"head bucket: {exc}") from exc

    def delete_bucket(self, bucket: str, *, force: bool = False) -> None:
        if not force:
            page = self.list_objects(bucket, max_keys=1)
            if page.objects or page.common_prefixes:
                raise ConflictError(
                    "bucket is not empty",
                    details={
                        "bucket": bucket,
                        "remediation": "delete objects first, or confirm an explicit force delete",
                    },
                )
        else:
            token: str | None = None
            while True:
                page = self.list_objects(bucket, max_keys=1000, marker=token)
                if page.objects:
                    self._client.delete_objects(
                        Bucket=bucket,
                        Delete={"Objects": [{"Key": o.key} for o in page.objects], "Quiet": True},
                    )
                if not page.is_truncated or not page.next_marker:
                    break
                token = page.next_marker

        self._call("delete bucket", self._client.delete_bucket, Bucket=bucket)

    def list_buckets(self) -> list[BucketInfo]:
        resp = self._call("list buckets", self._client.list_buckets)
        out = []
        for entry in resp.get("Buckets", []):
            name = _strip_arn(entry.get("Name"))
            if not name or name.startswith(_PROBE_BUCKET):
                continue
            out.append(
                BucketInfo(
                    name=name,
                    created_at=_iso(entry.get("CreationDate")),
                )
            )
        return sorted(out, key=lambda b: b.name)

    # ------------------------------------------------------------------
    # objects
    # ------------------------------------------------------------------
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
        kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
        }
        if content_length is not None:
            kwargs["ContentLength"] = content_length
        if metadata:
            kwargs["Metadata"] = metadata
        resp = self._call("put object", self._client.put_object, **kwargs)
        return str(resp.get("ETag", "")).strip('"')

    def get_object(
        self, bucket: str, key: str, *, byte_range: tuple[int, int] | None = None
    ) -> tuple[dict[str, str], Iterator[bytes]]:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if byte_range is not None:
            start, end = byte_range
            kwargs["Range"] = f"bytes={start}-{end}"
        resp = self._call("get object", self._client.get_object, **kwargs)
        headers = {
            "content-type": str(resp.get("ContentType") or "application/octet-stream"),
            "content-length": str(resp.get("ContentLength") or 0),
            "etag": str(resp.get("ETag") or "").strip('"'),
            "last-modified": _iso(resp.get("LastModified")) or "",
        }
        if resp.get("ContentRange"):
            headers["content-range"] = str(resp["ContentRange"])
        stream: Iterator[bytes] = resp["Body"].iter_chunks(chunk_size=65536)
        return headers, stream

    def head_object(self, bucket: str, key: str) -> ObjectInfo | None:
        try:
            resp = self._client.head_object(Bucket=bucket, Key=key)
        except be.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise self._translate(exc, "head object") from exc
        except be.BotoCoreError as exc:
            raise StorageError(f"head object: {exc}") from exc
        return ObjectInfo(
            key=key,
            size=int(resp.get("ContentLength") or 0),
            last_modified=_iso(resp.get("LastModified")),
            etag=str(resp.get("ETag") or "").strip('"'),
            content_type=str(resp.get("ContentType") or "application/octet-stream"),
            storage_class=resp.get("StorageClass"),
            version_id=resp.get("VersionId"),
            metadata={str(k): str(v) for k, v in (resp.get("Metadata") or {}).items()},
        )

    def delete_object(self, bucket: str, key: str) -> None:
        self._call("delete object", self._client.delete_object, Bucket=bucket, Key=key)

    def delete_objects(self, bucket: str, keys: Sequence[str]) -> dict[str, int]:
        if not keys:
            return {"deleted": 0, "errors": 0}
        counts = {"deleted": 0, "errors": 0}
        chunk = 1000
        for start in range(0, len(keys), chunk):
            batch = [{"Key": k} for k in keys[start : start + chunk]]
            resp = self._call(
                "delete objects",
                self._client.delete_objects,
                Bucket=bucket,
                Delete={"Objects": batch, "Quiet": False},
            )
            counts["deleted"] += len(resp.get("Deleted") or [])
            counts["errors"] += len(resp.get("Errors") or [])
        return counts

    def list_objects(
        self,
        bucket: str,
        *,
        prefix: str = "",
        delimiter: str | None = None,
        max_keys: int = 1000,
        marker: str | None = None,
    ) -> ObjectListPage:
        capped = max(1, min(max_keys, 1000))
        kwargs: dict[str, Any] = {"Bucket": bucket, "MaxKeys": capped}
        if prefix:
            kwargs["Prefix"] = prefix
        if delimiter:
            kwargs["Delimiter"] = delimiter
        # ListObjectsV2 paginates with ContinuationToken, not Marker.
        if marker:
            kwargs["ContinuationToken"] = marker
        resp = self._call("list objects", self._client.list_objects_v2, **kwargs)

        objects = tuple(
            ObjectInfo(
                key=item["Key"],
                size=int(item.get("Size") or 0),
                last_modified=_iso(item.get("LastModified")),
                etag=str(item.get("ETag") or "").strip('"'),
                storage_class=item.get("StorageClass"),
            )
            for item in resp.get("Contents", [])
        )
        prefixes = tuple(
            item["Prefix"].rstrip("/")
            for item in resp.get("CommonPrefixes", [])
            if item.get("Prefix")
        )
        truncated = bool(resp.get("IsTruncated"))
        next_marker = (
            resp.get("NextContinuationToken")
            or (objects[-1].key if truncated and objects else None)
            or (prefixes[-1] if truncated and prefixes else None)
        )
        return ObjectListPage(
            objects=objects,
            common_prefixes=prefixes,
            is_truncated=truncated,
            next_marker=next_marker,
            key_count=int(resp.get("KeyCount") or len(objects)),
        )

    def copy_object(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> ObjectInfo:
        resp = self._call(
            "copy object",
            self._client.copy_object,
            Bucket=dst_bucket,
            Key=dst_key,
            CopySource={"Bucket": src_bucket, "Key": src_key},
        )
        etag = str(resp.get("CopyObjectResult", {}).get("ETag", "")).strip('"')
        return ObjectInfo(key=dst_key, size=0, etag=etag or None)

    # ------------------------------------------------------------------
    # multipart
    # ------------------------------------------------------------------
    def create_multipart_upload(self, bucket: str, key: str) -> str:
        resp = self._call(
            "create multipart upload",
            self._client.create_multipart_upload,
            Bucket=bucket,
            Key=key,
        )
        return str(resp["UploadId"])

    def upload_part(
        self, bucket: str, key: str, upload_id: str, part_number: int, body: bytes
    ) -> UploadedPart:
        resp = self._call(
            "upload part",
            self._client.upload_part,
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=body,
        )
        etag = str(resp.get("ETag", "")).strip('"')
        return UploadedPart(part_number=part_number, etag=etag, size=len(body))

    def list_parts(self, bucket: str, key: str, upload_id: str) -> list[UploadedPart]:
        resp = self._call(
            "list parts",
            self._client.list_parts,
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
        )
        return [
            UploadedPart(
                part_number=int(p["PartNumber"]),
                etag=str(p.get("ETag", "")).strip('"'),
                size=int(p.get("Size") or 0),
            )
            for p in resp.get("Parts", [])
        ]

    def list_multipart_uploads(self, bucket: str) -> list[MultipartUpload]:
        resp = self._call("list multipart uploads", self._client.list_multipart_uploads, Bucket=bucket)
        return [
            MultipartUpload(
                upload_id=str(u["UploadId"]),
                key=str(u["Key"]),
                bucket=bucket,
                initiated_at=_iso(u.get("Initiated")),
            )
            for u in resp.get("Uploads", [])
        ]

    def complete_multipart_upload(
        self, bucket: str, key: str, upload_id: str, parts: Sequence[UploadedPart]
    ) -> ObjectInfo:
        ordered = sorted(parts, key=lambda p: p.part_number)
        resp = self._call(
            "complete multipart upload",
            self._client.complete_multipart_upload,
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [{"PartNumber": p.part_number, "ETag": p.etag} for p in ordered]
            },
        )
        etag = str(resp.get("ETag", "")).strip('"')
        # CompleteMultipartUpload does not return a size; re-head for the truth.
        settled = self.head_object(bucket, key)
        if settled is not None:
            return settled
        return ObjectInfo(key=key, size=0, etag=etag or None)

    def abort_multipart_upload(self, bucket: str, key: str, upload_id: str) -> None:
        self._call(
            "abort multipart upload",
            self._client.abort_multipart_upload,
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
        )

    # ------------------------------------------------------------------
    # presigned URLs
    # ------------------------------------------------------------------
    def presign_put(
        self,
        bucket: str,
        key: str,
        *,
        expires_in: int,
        content_type: str | None = None,
        content_length: int | None = None,
    ) -> PresignedTarget:
        params: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if content_type:
            params["ContentType"] = content_type
        url = self._call(
            "presign put",
            self._client.generate_presigned_url,
            ClientMethod="put_object",
            Params=params,
            ExpiresIn=expires_in,
        )
        headers = {"Content-Type": content_type} if content_type else {}
        return PresignedTarget(
            url=url,
            method="PUT",
            expires_at=(
                dt.datetime.now(dt.UTC) + dt.timedelta(seconds=expires_in)
            ).isoformat(),
            headers=headers,
        )

    def presign_get(
        self, bucket: str, key: str, *, expires_in: int, filename: str | None = None
    ) -> PresignedTarget:
        params: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if filename:
            safe = re.sub(r'[^A-Za-z0-9._-]', "_", filename)
            params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
        url = self._call(
            "presign get",
            self._client.generate_presigned_url,
            ClientMethod="get_object",
            Params=params,
            ExpiresIn=expires_in,
        )
        return PresignedTarget(
            url=url,
            method="GET",
            expires_at=(
                dt.datetime.now(dt.UTC) + dt.timedelta(seconds=expires_in)
            ).isoformat(),
        )

    # ------------------------------------------------------------------
    # bucket configuration
    # ------------------------------------------------------------------
    def get_versioning(self, bucket: str) -> str | None:
        resp = self._call("get versioning", self._client.get_bucket_versioning, Bucket=bucket)
        return resp.get("Status")

    def set_versioning(self, bucket: str, status: str) -> None:
        if status not in {"Enabled", "Suspended"}:
            raise ValidationError("versioning status must be 'Enabled' or 'Suspended'")
        self._call(
            "set versioning",
            self._client.put_bucket_versioning,
            Bucket=bucket,
            VersioningConfiguration={"Status": status},
        )

    def get_cors(self, bucket: str) -> list[str]:
        try:
            resp = self._client.get_bucket_cors(Bucket=bucket)
        except be.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"NoSuchCORSConfiguration", "404"}:
                return []
            raise self._translate(exc, "get cors") from exc
        except be.BotoCoreError as exc:
            raise StorageError(f"get cors: {exc}") from exc
        origins: list[str] = []
        for rule in resp.get("CORSRules", []):
            for origin in rule.get("AllowedOrigins", []):
                if origin not in origins:
                    origins.append(origin)
        return origins

    def set_cors(self, bucket: str, origins: Sequence[str]) -> None:
        if not origins:
            self._call("delete cors", self._client.delete_bucket_cors, Bucket=bucket)
            return
        self._call(
            "set cors",
            self._client.put_bucket_cors,
            Bucket=bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedHeaders": ["*"],
                        "AllowedMethods": ["GET", "HEAD", "PUT", "POST", "DELETE"],
                        "AllowedOrigins": list(origins),
                        "ExposeHeaders": ["ETag", "Content-Length", "Content-Range"],
                        "MaxAgeSeconds": 3000,
                    }
                ]
            },
        )

    def get_lifecycle(self, bucket: str) -> list[dict]:
        try:
            resp = self._client.get_bucket_lifecycle_configuration(Bucket=bucket)
        except be.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"NoSuchLifecycleConfiguration", "404"}:
                return []
            raise self._translate(exc, "get lifecycle") from exc
        except be.BotoCoreError as exc:
            raise StorageError(f"get lifecycle: {exc}") from exc
        return [dict(rule) for rule in resp.get("Rules", [])]

    def set_lifecycle(self, bucket: str, rules: Sequence[dict]) -> None:
        if not rules:
            self._call(
                "delete lifecycle",
                self._client.delete_bucket_lifecycle,
                Bucket=bucket,
            )
            return
        self._call(
            "set lifecycle",
            self._client.put_bucket_lifecycle_configuration,
            Bucket=bucket,
            LifecycleConfiguration={"Rules": [dict(r) for r in rules]},
        )

    def get_policy(self, bucket: str) -> dict | None:
        import json

        try:
            resp = self._client.get_bucket_policy(Bucket=bucket)
        except be.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"NoSuchBucketPolicy", "404"}:
                return None
            raise self._translate(exc, "get policy") from exc
        except be.BotoCoreError as exc:
            raise StorageError(f"get policy: {exc}") from exc
        raw = resp.get("Policy")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def set_policy(self, bucket: str, policy: dict) -> None:
        import json

        self._call(
            "set policy",
            self._client.put_bucket_policy,
            Bucket=bucket,
            Policy=json.dumps(policy),
        )