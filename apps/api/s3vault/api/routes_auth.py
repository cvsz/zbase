"""Authentication and user-management endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from ..services.audit import (
    LOGIN_FAILURE,
    LOGIN_SUCCESS,
    LOGOUT,
    PASSWORD_RESET_COMPLETED,
    PASSWORD_RESET_REQUESTED,
    SESSION_REVOKED,
    USER_CREATED,
    USER_DEACTIVATED,
    USER_UPDATED,
)
from ..services.users import Actor
from .deps import (
    ConfigDep,
    ServicesDep,
    clear_session_cookies,
    client_ip,
    parse_int,
    require_actor,
    require_capability,
    require_csrf,
    set_session_cookies,
)
from ..security.rbac import Capability

router = APIRouter(prefix="/api/v1", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserCreateBody(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9._-]+$")
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)
    role: str = Field(default="owner", pattern=r"^(owner|operator|auditor|admin)$")


class UserUpdateBody(BaseModel):
    role: str | None = Field(default=None, pattern=r"^(owner|operator|auditor|admin)$")
    is_active: bool | None = None
    new_password: str | None = Field(default=None, max_length=256)


class PasswordChangeBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class ResetRequestBody(BaseModel):
    email: str = Field(min_length=3, max_length=255)


class ResetCompleteBody(BaseModel):
    token: str = Field(min_length=8, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class RevokeSessionsBody(BaseModel):
    all_sessions: bool = False


def _public_user(row: dict[str, Any]) -> dict[str, Any]:
    """Strip every field that must never leave the server."""
    return {
        "id": str(row["id"]),
        "username": row["username"],
        "email": row.get("email"),
        "role": row.get("role"),
        "is_active": row.get("is_active"),
        "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        "last_login_at": row.get("last_login_at").isoformat() if row.get("last_login_at") else None,
    }


@router.post("/auth/login")
def login(
    body: LoginBody,
    request: Request,
    response: Response,
    services: ServicesDep,
    config: ConfigDep,
) -> dict[str, Any]:
    """Exchange credentials for a session cookie pair."""
    from .deps import limiter as get_limiter

    ip = client_ip(request)
    get_limiter().check(f"login:{ip or 'unknown'}", config.security.login_rate_limit_per_min)

    try:
        account = services.users.authenticate(
            username=body.username, password=body.password, ip=ip
        )
    except Exception:
        services.audit.record(
            action=LOGIN_FAILURE,
            resource_type="session",
            resource_id=body.username[:64],
            outcome="failure",
            actor_type="user",
            request_id=getattr(request.state, "request_id", ""),
            ip_address=ip,
            user_agent=request.headers.get("user-agent"),
            # No password is recorded, not even truncated.
            detail={"reason": "invalid_credentials"},
        )
        raise

    session_token, csrf_token, expires_at = services.users.create_session(
        user_id=account["id"],
        ip=ip,
        user_agent=request.headers.get("user-agent"),
        session_ttl_s=config.security.session_ttl_s,
        idle_ttl_s=config.security.session_idle_ttl_s,
    )
    set_session_cookies(
        response,
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        max_age=config.security.session_ttl_s,
    )
    services.audit.record(
        action=LOGIN_SUCCESS,
        resource_type="session",
        resource_id=str(account["id"]),
        outcome="success",
        actor_id=str(account["id"]),
        actor_type="user",
        actor_label=str(account["username"]),
        request_id=getattr(request.state, "request_id", ""),
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )
    return {
        "user": {
            "id": str(account["id"]),
            "username": account["username"],
            "role": account["role"],
        },
        "csrf_header": config.security.csrf_header_name,
        "expires_at": expires_at.isoformat(),
    }


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    services: ServicesDep,
    config: ConfigDep,
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Sign out and revoke the session.

    CSRF-protected like every other state change: an attacker must not be able
    to force a sign-out from another origin.
    """
    token = request.cookies.get(config.security.cookie_name)
    if token:
        services.users.revoke_session(token)
        services.audit.record(
            action=LOGOUT,
            resource_type="session",
            resource_id="current",
            outcome="success",
            request_id=getattr(request.state, "request_id", ""),
            ip_address=client_ip(request),
        )
    clear_session_cookies(response, config)
    return {"status": "signed_out"}


@router.get("/auth/me")
def me(actor: Annotated[Actor, Depends(require_actor)], services: ServicesDep) -> dict[str, Any]:
    row = services.users.get(actor.user_id)
    return {
        "user": _public_user(row),
        "active_sessions": services.users.active_session_count(actor.user_id),
    }


@router.post("/auth/sessions/revoke")
def revoke_sessions(
    body: RevokeSessionsBody,
    request: Request,
    response: Response,
    services: ServicesDep,
    config: ConfigDep,
    actor: Annotated[Actor, Depends(require_actor)],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    if body.all_sessions:
        count = services.users.revoke_all_sessions(actor.user_id, reason="user_revoked_all")
        clear_session_cookies(response, config)
        return {"revoked": count}
    token = request.cookies.get(config.security.cookie_name)
    revoked = services.users.revoke_session(token) if token else False
    clear_session_cookies(response, config)
    services.audit.record(
        action=SESSION_REVOKED,
        resource_type="session",
        resource_id=actor.user_id,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"scope": "current" if not body.all_sessions else "all"},
    )
    return {"revoked": 1 if revoked else 0}


@router.post("/auth/password")
def change_password(
    body: PasswordChangeBody,
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_actor)],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    services.users.authenticate(
        username=actor.username, password=body.current_password, ip=client_ip(request)
    )
    services.users.change_password(actor.user_id, body.new_password)
    services.audit.record(
        action=PASSWORD_RESET_COMPLETED,
        resource_type="user",
        resource_id=actor.user_id,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"method": "self_service"},
    )
    return {"status": "password_changed", "sessions_revoked": True}


@router.post("/auth/password-reset/request")
def request_reset(
    body: ResetRequestBody,
    request: Request,
    services: ServicesDep,
    config: ConfigDep,
) -> dict[str, Any]:
    """Always returns the same shape so accounts cannot be enumerated.

    When SMTP is not configured the token is returned in the response and the
    behaviour is logged, which is development-only by construction: see
    docs/SECURITY.md#password-reset.
    """
    from .deps import limiter as get_limiter

    get_limiter().check(
        f"reset:{client_ip(request) or 'unknown'}", config.security.login_rate_limit_per_min
    )
    token = services.users.issue_reset_token(body.email)
    services.audit.record(
        action=PASSWORD_RESET_REQUESTED,
        resource_type="user",
        resource_id=body.email[:64],
        outcome="success",
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"delivered": token is None, "channel": "response" if token else "none"},
    )
    payload: dict[str, Any] = {
        "status": "accepted",
        "message": "if the address is registered, a reset link has been sent",
    }
    if token is not None and config.env == "development":
        payload["development_token"] = token
    return payload


@router.post("/auth/password-reset/complete")
def complete_reset(
    body: ResetCompleteBody,
    request: Request,
    services: ServicesDep,
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    services.users.complete_reset(body.token, body.new_password)
    services.audit.record(
        action=PASSWORD_RESET_COMPLETED,
        resource_type="user",
        outcome="success",
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"method": "token"},
    )
    return {"status": "password_reset"}


# ---------------------------------------------------------------------------
# user administration
# ---------------------------------------------------------------------------
users_router = APIRouter(prefix="/api/v1/users", tags=["users"])


@users_router.get("")
def list_users(
    services: ServicesDep,
    _: Annotated[Actor, Depends(require_capability(Capability.USER_READ))],
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    limit = parse_int(limit, 50, minimum=1, maximum=200)
    offset = parse_int(offset, 0, minimum=0, maximum=1_000_000)
    rows, total = services.users.list_users(limit=limit, offset=offset)
    return {
        "users": [_public_user(r) for r in rows],
        "pagination": {"total": total, "limit": limit, "offset": offset},
    }


@users_router.post("", status_code=201)
def create_user(
    body: UserCreateBody,
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_capability(Capability.USER_WRITE))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    row = services.users.create_user(
        username=body.username,
        email=body.email,
        password=body.password,
        role=body.role,
    )
    services.audit.record(
        action=USER_CREATED,
        resource_type="user",
        resource_id=str(row["id"]),
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"username": row["username"], "role": body.role},
    )
    return {"user": _public_user(row)}


@users_router.get("/{user_id}")
def get_user(
    user_id: str,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_actor)],
) -> dict[str, Any]:
    if actor.user_id != user_id and not actor.is_admin:
        from ..errors import PermissionError_

        raise PermissionError_("only an administrator may read another user")
    return {"user": _public_user(services.users.get(user_id))}


@users_router.patch("/{user_id}")
def update_user(
    user_id: str,
    body: UserUpdateBody,
    request: Request,
    services: ServicesDep,
    actor: Annotated[Actor, Depends(require_capability(Capability.USER_WRITE))],
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    existing = services.users.get(user_id)
    changed: list[str] = []
    if body.role is not None and body.role != existing.get("role"):
        services.users.change_role(user_id, body.role)
        changed.append("role")
    if body.is_active is not None and body.is_active != existing.get("is_active"):
        services.users.set_active(user_id, body.is_active)
        changed.append("is_active")
    if body.new_password:
        services.users.change_password(user_id, body.new_password)
        changed.append("password")

    services.audit.record(
        action=USER_UPDATED,
        resource_type="user",
        resource_id=user_id,
        outcome="success",
        actor_id=actor.user_id,
        actor_type="user",
        request_id=getattr(request.state, "request_id", ""),
        ip_address=client_ip(request),
        detail={"fields": changed},
    )
    if body.is_active is False:
        services.audit.record(
            action=USER_DEACTIVATED,
            resource_type="user",
            resource_id=user_id,
            outcome="success",
            actor_id=actor.user_id,
            actor_type="user",
            request_id=getattr(request.state, "request_id", ""),
            ip_address=client_ip(request),
        )
    return {"user": _public_user(services.users.get(user_id)), "changed": changed}


def register(auth_router: APIRouter) -> None:  # pragma: no cover - trivial
    auth_router.include_router(users_router)