"""Bucket and object endpoints."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from ..errors import NotFoundError, PermissionError_, ValidationError
from ..security.rbac import Capability
from ..services.audit import (
    BUCKET_CONFIG_CHANGED,
    BUCKET_CREATED,
    BUCKET_DELETED,
    OBJECT_DELETED,
    OBJECT_DELETED_BULK,
)
from ..services.users import Actor
from ..storage.base import Feature
from .deps import (
    ConfigDep,
    ServicesDep,
    client_ip,
    parse_bool,
    parse_int,
    require_actor,
    require_capability,
    require_csrf,
    require_feature,
)

router = APIRouter(prefix="/api/v1/buckets", tags=["buckets"])


class BucketCreateBody(BaseModel):
    name: str = Field(min_length=3, max_length=63)


class BucketDeleteBody(BaseModel):
    force: bool = False
    confirm_name: str | None = None
    dry_run: bool = False


class VersioningBody(BaseModel):
    status: str = Field(pattern=r"^(Enabled|Suspended)$")


class CorsBody(BaseModel):
    origins: list[str] = Field(default_factory=list)


class LifecycleBody(BaseModel):
    rules: list[dict[str, Any]] = Field(default_factory=list)


class PolicyBody(BaseModel):
    policy: dict[str, Any]


class ObjectDeleteBody(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=1000)
    dry_run: bool = False


class PresignBody(BaseModel):
    key: str = Field(min_length=1, max_length=1024)
    expires_in: int | None = None
    content_type: str | None = None
    filename: str | None = None


class MultipartCompleteBody(BaseModel):
    parts: list[dict[str, Any]] = Field(min_length=1)


# ---------------------------------------------------------------------------
# buckets
# ---------------------------------------------------------------------------
@router.get("")
def list_buckets(
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_actor)],
) -> dict[str, Any]:
    bucket_service = services.buckets()
    rows = bucket_service.list_for_owner(actor.user_id, is_admin=actor.is_admin)
    return {
        "buckets": [
            {
                "name": r["name"],
                "owner": r.get("owner_username"),
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "versioning_enabled": r.get("versioning_enabled"),
            }
            for r in rows
        ]
    }


@router.post("", status_code=201)
def create_bucket(
    body: BucketCreateBody,
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_capability(Capability.BUCKET_WRITE))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    bucket_service = services.buckets()
    row = bucket_service.create(name=body.name, owner_id=actor.user_id, actor=actor)
    services.audit.record(
        action=BUCKET_CREATED,
        resource_type="bucket",
        resource_id=str(row["name"]),
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"bucket": row["name"]},
    )
    return {"bucket": {"name": row["name"], "created_at": row["created_at"].isoformat()}}


@router.get("/{bucket}")
def get_bucket(
    bucket: str,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_actor)],
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_can_read(bucket, actor)
    row = bucket_service.get(bucket)
    capabilities = services.capabilities
    return {
        "bucket": {
            "name": row["name"],
            "owner_id": str(row["owner_id"]),
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            "versioning_enabled": row.get("versioning_enabled"),
            "cors_origins": list(row.get("cors_origins") or []),
        },
        "engine_features": {
            "versioning": capabilities.supports(Feature.VERSIONING),
            "cors": capabilities.supports(Feature.CORS),
            "lifecycle": capabilities.supports(Feature.LIFECYCLE),
            "policies": capabilities.supports(Feature.POLICIES),
            "multipart": capabilities.supports(Feature.MULTIPART),
        },
        "usage": services.usage.measure_bucket(bucket),
        "quota": services.quotas.summarise(actor.user_id, str(row["id"])),
    }


@router.delete("/{bucket}")
def delete_bucket(
    bucket: str,
    request: Request,
    response: Response,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_capability(Capability.BUCKET_DELETE))],
    body: Annotated[BucketDeleteBody | None, None] = None,
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Delete a bucket.

    Requires an explicit name confirmation, refuses non-empty buckets unless
    ``force`` is set, and honours the configured dry-run safety gate.
    """
    payload = body or BucketDeleteBody()
    bucket_service = services.buckets()
    bucket_service.assert_owner_or_admin(bucket, actor)

    if payload.confirm_name != bucket:
        raise ValidationError(
            "confirm_name must exactly match the bucket being deleted",
            details={"expected": bucket},
        )

    services.safety.assert_mutation_allowed(
        operation="bucket.delete",
        actor_id=actor.user_id,
        request_id=getattr(request.state, "request_id", ""),
        ip=client_ip(request),
    )
    dry_run = payload.dry_run or services.safety.dry_run_required
    plan = bucket_service.delete(name=bucket, force=payload.force, dry_run=dry_run)

    if dry_run:
        services.safety.record_dry_run(
            operation="bucket.delete",
            actor_id=actor.user_id,
            plan=plan,
            request_id=getattr(request.state, "request_id", ""),
            ip=client_ip(request),
        )
        return {"dry_run": True, "plan": plan}

    services.audit.record(
        action=BUCKET_DELETED,
        resource_type="bucket",
        resource_id=bucket,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"forced": payload.force, "non_empty": plan.get("non_empty")},
    )
    return {"deleted": bucket, "forced": payload.force}


@router.put("/{bucket}/versioning")
def set_versioning(
    bucket: str,
    body: VersioningBody,
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_feature(Feature.VERSIONING))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_owner_or_admin(bucket, actor)
    result = bucket_service.set_versioning(name=bucket, status=body.status)
    services.audit.record(
        action=BUCKET_CONFIG_CHANGED,
        resource_type="bucket",
        resource_id=bucket,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"field": "versioning", "status": body.status},
    )
    return result


@router.get("/{bucket}/versioning")
def get_versioning(
    bucket: str,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_feature(Feature.VERSIONING))],
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_can_read(bucket, actor)
    return bucket_service.get_versioning(bucket)


@router.put("/{bucket}/cors")
def set_cors(
    bucket: str,
    body: CorsBody,
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_feature(Feature.CORS))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_owner_or_admin(bucket, actor)
    result = bucket_service.set_cors(name=bucket, origins=body.origins)
    services.audit.record(
        action=BUCKET_CONFIG_CHANGED,
        resource_type="bucket",
        resource_id=bucket,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"field": "cors", "origin_count": len(body.origins)},
    )
    return result


@router.get("/{bucket}/cors")
def get_cors(
    bucket: str,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_actor)],
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_can_read(bucket, actor)
    return bucket_service.get_cors(bucket)


@router.put("/{bucket}/lifecycle")
def set_lifecycle(
    bucket: str,
    body: LifecycleBody,
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_feature(Feature.LIFECYCLE))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_owner_or_admin(bucket, actor)
    result = bucket_service.set_lifecycle(name=bucket, rules=body.rules)
    services.audit.record(
        action=BUCKET_CONFIG_CHANGED,
        resource_type="bucket",
        resource_id=bucket,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"field": "lifecycle", "rule_count": len(body.rules)},
    )
    return result


@router.get("/{bucket}/lifecycle")
def get_lifecycle(
    bucket: str,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_actor)],
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_can_read(bucket, actor)
    return bucket_service.get_lifecycle(bucket)


@router.put("/{bucket}/policy")
def set_policy(
    bucket: str,
    body: PolicyBody,
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_feature(Feature.POLICIES))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_owner_or_admin(bucket, actor)
    result = bucket_service.set_policy(name=bucket, policy=body.policy)
    services.audit.record(
        action=BUCKET_CONFIG_CHANGED,
        resource_type="bucket",
        resource_id=bucket,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"field": "policy"},
    )
    return result


@router.get("/{bucket}/policy")
def get_policy(
    bucket: str,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_actor)],
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_can_read(bucket, actor)
    return bucket_service.get_policy(bucket)


# ---------------------------------------------------------------------------
# objects
# ---------------------------------------------------------------------------
@router.get("/{bucket}/objects")
def list_objects(
    bucket: str,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_actor)],
    prefix: Annotated[str, Query(max_length=1024)] = "",
    delimiter: Annotated[str | None, Query(max_length=1)] = "/",
    max_keys: Annotated[int, Query(ge=1, le=1000)] = 1000,
    marker: Annotated[str | None, Query(max_length=1024)] = None,
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_can_read(bucket, actor)
    page = services.adapter.list_objects(
        bucket,
        prefix=prefix,
        delimiter=delimiter,
        max_keys=max_keys,
        marker=marker,
    )
    return {
        "objects": [
            {
                "key": o.key,
                "size": o.size,
                "last_modified": o.last_modified,
                "etag": o.etag,
                "content_type": o.content_type,
                "storage_class": o.storage_class,
            }
            for o in page.objects
        ],
        "common_prefixes": list(page.common_prefixes),
        "is_truncated": page.is_truncated,
        "next_marker": page.next_marker,
        "key_count": page.key_count,
    }


@router.get("/{bucket}/objects/stat")
def bucket_stat(
    bucket: str,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_actor)],
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_can_read(bucket, actor)
    storage_bytes, object_count = services.usage.measure_bucket(bucket)
    return {"storage_bytes": storage_bytes, "object_count": object_count}


@router.get("/{bucket}/objects/detail")
def object_detail(
    bucket: str,
    key: str,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_actor)],
) -> dict[str, Any]:
    from ..security.tokens import normalise_object_key

    bucket_service = services.buckets()
    bucket_service.assert_can_read(bucket, actor)
    try:
        safe_key = normalise_object_key(key)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    info = services.adapter.head_object(bucket, safe_key)
    if info is None:
        raise NotFoundError("object not found")
    return {
        "object": {
            "key": info.key,
            "size": info.size,
            "last_modified": info.last_modified,
            "etag": info.etag,
            "content_type": info.content_type,
            "storage_class": info.storage_class,
            "version_id": info.version_id,
            "metadata": info.metadata,
        }
    }


@router.delete("/{bucket}/objects")
def delete_objects(
    bucket: str,
    body: ObjectDeleteBody,
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_capability(Capability.OBJECT_DELETE))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    from ..security.tokens import normalise_object_key

    bucket_service = services.buckets()
    bucket_service.assert_can_write(bucket, actor)

    keys: list[str] = []
    for raw in body.keys:
        try:
            keys.append(normalise_object_key(raw))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    services.safety.assert_mutation_allowed(
        operation="object.delete",
        actor_id=actor.user_id,
        request_id=getattr(request.state, "request_id", ""),
        ip=client_ip(request),
    )
    dry_run = body.dry_run or services.safety.dry_run_required
    if dry_run:
        services.safety.record_dry_run(
            operation="object.delete",
            actor_id=actor.user_id,
            plan={"bucket": bucket, "keys": keys, "count": len(keys)},
            request_id=getattr(request.state, "request_id", ""),
            ip=client_ip(request),
        )
        return {"dry_run": True, "plan": {"bucket": bucket, "keys": keys, "count": len(keys)}}

    result = services.adapter.delete_objects(bucket, keys)
    services.audit.record(
        action=OBJECT_DELETED_BULK if len(keys) > 1 else OBJECT_DELETED,
        resource_type="object",
        resource_id=bucket,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"bucket": bucket, "requested": len(keys), "deleted": result["deleted"]},
    )
    return {"deleted": result["deleted"], "errors": result["errors"], "requested": len(keys)}


@router.post("/{bucket}/presigned-download")
def presigned_download(
    bucket: str,
    body: PresignBody,
    request: Request,
    config: ConfigDep,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_capability(Capability.OBJECT_READ))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Issue a short-lived download URL.

    The URL is returned only to the authenticated console over TLS and is never
    written to logs or audit details.
    """
    from ..security.tokens import normalise_object_key

    bucket_service = services.buckets()
    bucket_service.assert_can_read(bucket, actor)
    try:
        safe_key = normalise_object_key(body.key)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    ttl = parse_int(body.expires_in, config.uploads.presign_ttl_s, minimum=60, maximum=config.uploads.presign_max_ttl_s)
    target = services.adapter.presign_get(
        bucket, safe_key, expires_in=ttl, filename=body.filename
    )
    services.audit.record(
        action="object.presign_download",
        resource_type="object",
        resource_id=safe_key,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"bucket": bucket, "expires_in": ttl},
    )
    return {"url": target.url, "method": target.method, "expires_at": target.expires_at}


@router.post("/{bucket}/presigned-upload")
def presigned_upload(
    bucket: str,
    body: PresignBody,
    request: Request,
    config: ConfigDep,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_capability(Capability.OBJECT_WRITE))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Issue a short-lived direct-to-engine upload URL."""
    from ..security.tokens import normalise_object_key

    bucket_service = services.buckets()
    bucket_service.assert_can_write(bucket, actor)
    try:
        safe_key = normalise_object_key(body.key)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    if services.adapter.head_object(bucket, safe_key) is None and not bucket_service.get(bucket):
        raise NotFoundError("bucket not found")

    ttl = parse_int(body.expires_in, config.uploads.presign_ttl_s, minimum=60, maximum=config.uploads.presign_max_ttl_s)
    target = services.adapter.presign_put(
        bucket, safe_key, expires_in=ttl, content_type=body.content_type
    )
    services.audit.record(
        action="object.presign_upload",
        resource_type="object",
        resource_id=safe_key,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"bucket": bucket, "expires_in": ttl},
    )
    return {
        "url": target.url,
        "method": target.method,
        "expires_at": target.expires_at,
        "headers": target.headers,
    }


@router.get("/{bucket}/uploads")
def list_uploads(
    bucket: str,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_feature(Feature.MULTIPART))],
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_can_read(bucket, actor)
    uploads = services.adapter.list_multipart_uploads(bucket)
    return {
        "uploads": [
            {
                "upload_id": u.upload_id,
                "key": u.key,
                "initiated_at": u.initiated_at,
            }
            for u in uploads
        ]
    }


@router.delete("/{bucket}/uploads")
def abort_upload(
    bucket: str,
    upload_id: str,
    key: str,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_feature(Feature.ENGINE_ABORT_MULTIPART))],
) -> dict[str, Any]:
    bucket_service = services.buckets()
    bucket_service.assert_can_write(bucket, actor)
    services.adapter.abort_multipart_upload(bucket, key, upload_id)
    return {"aborted": upload_id}