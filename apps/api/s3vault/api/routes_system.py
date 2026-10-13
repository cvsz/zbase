"""Credential, audit, overview and settings endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..errors import ValidationError
from ..security.rbac import Capability
from ..services.audit import CREDENTIAL_CREATED, CREDENTIAL_REVOKED, CREDENTIAL_ROTATED, SETTINGS_CHANGED
from ..services.users import Actor
from .deps import (
    ConfigDep,
    ServicesDep,
    client_ip,
    parse_datetime,
    parse_int,
    require_actor,
    require_capability,
    require_csrf,
)

router = APIRouter(prefix="/api/v1", tags=["credentials"])


class CredentialCreateBody(BaseModel):
    permissions: list[str] = Field(min_length=1)
    bucket_scope: list[str] = Field(default_factory=list)
    prefix_scope: str | None = None
    description: str = Field(default="", max_length=200)
    expires_at: str | None = None


def _public_credential(row: dict[str, Any]) -> dict[str, Any]:
    """Credential representation that never contains secret material."""
    return {
        "id": str(row["id"]),
        "access_key_id": row["access_key_id"],
        "description": row.get("description"),
        "permissions": list(row.get("permissions") or []),
        "bucket_scope": list(row.get("bucket_scope") or []),
        "prefix_scope": row.get("prefix_scope"),
        "expires_at": row["expires_at"].isoformat() if row.get("expires_at") else None,
        "revoked_at": row["revoked_at"].isoformat() if row.get("revoked_at") else None,
        "last_used_at": row["last_used_at"].isoformat() if row.get("last_used_at") else None,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "owner": row.get("username"),
    }


@router.get("/credentials")
def list_credentials(
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_actor)],
) -> dict[str, Any]:
    rows = (
        services.credentials.list_all()
        if actor.is_admin
        else (services.credentials.list_for_user(actor.user_id), None)
    )
    if actor.is_admin:
        items, total = rows
        return {"credentials": [_public_credential(r) for r in items],
                "pagination": {"total": total}}
    return {"credentials": [_public_credential(r) for r in rows[0]]}


@router.post("/credentials", status_code=201)
def create_credential(
    body: CredentialCreateBody,
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_capability(Capability.CREDENTIAL_ISSUE))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Create a scoped S3 credential.

    ``secret_access_key`` is present in this response only. It is stored as an
    AES-256-GCM envelope and cannot be retrieved again.
    """
    expires = parse_datetime(body.expires_at, "expires_at")
    credential = services.credentials.create(
        user_id=actor.user_id,
        permissions=body.permissions,
        bucket_scope=body.bucket_scope,
        prefix_scope=body.prefix_scope,
        description=body.description,
        expires_at=expires,
        created_by=actor.user_id,
    )
    services.audit.record(
        action=CREDENTIAL_CREATED,
        resource_type="credential",
        resource_id=str(credential["id"]),
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={
            "access_key_id": credential["access_key_id"],
            "permissions": credential["permissions"],
            "bucket_scope": credential["bucket_scope"],
        },
    )
    return {
        "credential": {
            "id": credential["id"],
            "access_key_id": credential["access_key_id"],
            "secret_access_key": credential["secret_access_key"],
            "permissions": credential["permissions"],
            "bucket_scope": credential["bucket_scope"],
            "prefix_scope": credential["prefix_scope"],
            "expires_at": credential["expires_at"].isoformat() if credential["expires_at"] else None,
        },
        "warning": "the secret access key is shown once and cannot be retrieved again",
    }


@router.post("/credentials/{credential_id}/rotate")
def rotate_credential(
    credential_id: str,
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_capability(Capability.CREDENTIAL_ISSUE))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    existing = services.credentials.get(credential_id)
    if not actor.is_admin and str(existing["user_id"]) != actor.user_id:
        from ..errors import PermissionError_

        raise PermissionError_("cannot rotate another user's credential")

    replacement = services.credentials.rotate(
        credential_id, user_id=str(existing["user_id"]), created_by=actor.user_id
    )
    services.audit.record(
        action=CREDENTIAL_ROTATED,
        resource_type="credential",
        resource_id=credential_id,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"replacement_id": replacement["id"]},
    )
    return {
        "credential": {
            "id": replacement["id"],
            "access_key_id": replacement["access_key_id"],
            "secret_access_key": replacement["secret_access_key"],
            "permissions": replacement["permissions"],
        },
        "warning": "the secret access key is shown once and cannot be retrieved again",
    }


@router.delete("/credentials/{credential_id}")
def revoke_credential(
    credential_id: str,
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_capability(Capability.CREDENTIAL_ISSUE))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    existing = services.credentials.get(credential_id)
    if not actor.is_admin and str(existing["user_id"]) != actor.user_id:
        from ..errors import PermissionError_

        raise PermissionError_("cannot revoke another user's credential")
    services.credentials.revoke(credential_id)
    services.audit.record(
        action=CREDENTIAL_REVOKED,
        resource_type="credential",
        resource_id=credential_id,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"access_key_id": existing["access_key_id"]},
    )
    return {"revoked": credential_id}


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
audit_router = APIRouter(prefix="/api/v1", tags=["audit"])


@audit_router.get("/audit-events")
def list_audit_events(
    services: ServicesDep,
    _: Annotated[Actor, Depends(require_capability(Capability.AUDIT_READ))],
    action: str | None = None,
    resource_type: str | None = None,
    outcome: str | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    limit = parse_int(limit, 50, minimum=1, maximum=500)
    offset = parse_int(offset, 0, minimum=0, maximum=1_000_000)
    rows, total = services.audit.list(
        action=action,
        resource_type=resource_type,
        outcome=outcome,
        since=since,
        limit=limit,
        offset=offset,
    )
    return {
        "events": [
            {
                "id": int(r["id"]),
                "occurred_at": r["occurred_at"].isoformat(),
                "actor": r.get("actor_label") or (str(r["actor_id"]) if r.get("actor_id") else r["actor_type"]),
                "action": r["action"],
                "resource_type": r["resource_type"],
                "resource_id": r["resource_id"],
                "outcome": r["outcome"],
                "request_id": r["request_id"],
                "ip_address": r.get("ip_address"),
                "detail": r.get("detail") or {},
            }
            for r in rows
        ],
        "pagination": {"total": total, "limit": limit, "offset": offset},
    }


@audit_router.get("/audit-events/export")
def export_audit_events(
    services: ServicesDep,
    config: ConfigDep,
    _: Annotated[Actor, Depends(require_capability(Capability.AUDIT_READ))],
    limit: int = 5000,
) -> Any:
    """Export audit events as JSON Lines for offline retention."""
    import json

    limit = parse_int(limit, 5000, minimum=1, maximum=50_000)
    rows, _ = services.audit.list(limit=limit)
    body = "\n".join(
        json.dumps(
            {
                "occurred_at": r["occurred_at"].isoformat(),
                "action": r["action"],
                "resource_type": r["resource_type"],
                "resource_id": r["resource_id"],
                "outcome": r["outcome"],
                "actor_id": str(r["actor_id"]) if r.get("actor_id") else None,
                "request_id": r["request_id"],
                "detail": r.get("detail") or {},
            }
        )
        for r in rows
    )
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(
        content=body + "\n",
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=s3vault-audit.jsonl"},
    )


# ---------------------------------------------------------------------------
# overview / settings / health
# ---------------------------------------------------------------------------
system_router = APIRouter(prefix="/api/v1", tags=["system"])


@system_router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok"}


@system_router.get("/overview")
def overview(
    services: ServicesDep,
    config: ConfigDep,
    actor: Annotated[Actor, Depends(require_actor)],
) -> dict[str, Any]:
    """Dashboard data.

    Every figure is labelled with how it was obtained so the console can
    distinguish a measurement from a cached value from an unavailable one.
    """
    capabilities = services.capabilities
    db_ok = services.db.ping()
    engine_ok = services.adapter.healthy()
    latest = services.usage.latest_global()

    bucket_count = services.db.fetch_one("SELECT count(*) AS n FROM buckets")
    credential_count = services.db.fetch_one(
        "SELECT count(*) AS n FROM credentials WHERE revoked_at IS NULL"
    )
    user_count = services.db.fetch_one("SELECT count(*) AS n FROM users")
    job_counts = services.db.fetch_all(
        "SELECT state, count(*) AS n FROM background_jobs GROUP BY state"
    )

    return {
        "environment": config.env,
        "hostname": config.public_hostname,
        "s3_endpoint": config.s3_public_endpoint,
        "engine": {
            "name": capabilities.engine_name,
            "version": capabilities.engine_version,
            "healthy": engine_ok,
            "supported_features": sorted(f.value for f in capabilities.supported),
            "unsupported_features": {
                k: v for k, v in capabilities.notes.items() if k.startswith(("bucket.", "engine."))
            },
        },
        "database": {"healthy": db_ok},
        "totals": {
            "buckets": {"value": int(bucket_count["n"]) if bucket_count else 0, "source": "database"},
            "users": {"value": int(user_count["n"]) if user_count else 0, "source": "database"},
            "credentials": {
                "value": int(credential_count["n"]) if credential_count else 0,
                "source": "database",
            },
            "storage_bytes": {
                "value": int(latest["storage_bytes"]) if latest else None,
                "source": "engine-listing" if latest else "unavailable",
                "measured_at": latest["measured_at"].isoformat() if latest else None,
            },
            "object_count": {
                "value": int(latest["object_count"]) if latest else None,
                "source": "engine-listing" if latest else "unavailable",
                "measured_at": latest["measured_at"].isoformat() if latest else None,
            },
        },
        "capacity": services.usage.engine_capacity(),
        "quota": services.quotas.summarise(actor.user_id),
        "activity_24h": services.usage.activity(hours=24),
        "background_jobs": {str(r["state"]): int(r["n"]) for r in job_counts},
        "backup": services.safety.latest_backup(),
    }


@system_router.get("/usage")
def usage(
    services: ServicesDep,
    _: Annotated[Actor, Depends(require_capability(Capability.USAGE_READ))],
) -> dict[str, Any]:
    return {
        "global": services.usage.latest_global(),
        "per_bucket": services.usage.latest_per_bucket(),
        "measurement_method": "engine-listing",
    }


@system_router.get("/settings")
def get_settings(
    services: ServicesDep,
    config: ConfigDep,
    _: Annotated[Actor, Depends(require_capability(Capability.SETTINGS_READ))],
) -> dict[str, Any]:
    """Return non-sensitive settings.

    Secrets are reported as booleans only. No secret value is ever returned
    after configuration.
    """
    return {
        "environment": config.env,
        "public_hostname": config.public_hostname,
        "console_url": config.console_url,
        "s3_public_endpoint": config.s3_public_endpoint,
        "storage_engine_endpoint": config.storage.endpoint_url,
        "addressing_style": config.storage.addressing_style,
        "uploads": {
            "max_object_bytes": config.uploads.max_object_bytes,
            "max_part_bytes": config.uploads.max_part_bytes,
            "min_part_bytes": config.uploads.min_part_bytes,
            "multipart_threshold_bytes": config.uploads.multipart_threshold_bytes,
            "request_body_limit_bytes": config.uploads.request_body_limit_bytes,
        },
        "sessions": {
            "ttl_seconds": config.security.session_ttl_s,
            "idle_ttl_seconds": config.security.session_idle_ttl_s,
            "cookie_secure": config.security.cookie_secure,
            "cookie_samesite": config.security.cookie_samesite,
        },
        "quotas": {
            "default_user_bytes": config.quotas.default_user_bytes,
            "default_bucket_bytes": config.quotas.default_bucket_bytes,
            "max_buckets_per_user": config.quotas.max_buckets_per_user,
        },
        "safety": {
            "require_dry_run": config.safety.require_dry_run,
            "require_backup": config.safety.require_backup,
            "allow_production_changes": config.safety.allow_production_changes,
            "backup_retention_days": config.safety.backup_retention_days,
        },
        "secrets": {
            "session_signing_key": {"configured": True, "value": None},
            "key_encryption_key": {"configured": True, "value": None},
            "storage_root_access_key": {
                "configured": bool(config.storage.root_access_key),
                "value": None,
            },
            "storage_root_secret_key": {
                "configured": bool(config.storage.root_secret_key),
                "value": None,
            },
        },
        "issues": list(config.issues),
    }


@system_router.get("/settings/engine-capabilities")
def engine_capabilities(
    services: ServicesDep,
    _: Annotated[Actor, Depends(require_capability(Capability.SETTINGS_READ))],
) -> dict[str, Any]:
    return services.capabilities.as_dict()


@system_router.post("/settings/backup-record")
def record_backup(
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_capability(Capability.BACKUP_MANAGE))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Start a backup record.

    The backup itself is performed by scripts/backup.sh; this endpoint opens
    the record so the console shows real status rather than a guess.
    """
    from ..config import load_config

    cfg = load_config()
    if not cfg.safety.backup_dir:
        raise ValidationError("S3VAULT_BACKUP_DIR is not configured")
    backup_id = services.safety.begin_backup(cfg.safety.backup_dir)
    return {"backup_id": backup_id, "location": cfg.safety.backup_dir}