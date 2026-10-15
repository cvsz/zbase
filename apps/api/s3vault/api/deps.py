"""Request dependencies: session auth, CSRF, rate limiting, capability checks."""

from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict, deque
from typing import Annotated, Any

from fastapi import Depends, Request, Response

from ..config import AppConfig
from ..errors import AuthError, PermissionError_, RateLimitedError, ValidationError
from ..security.rbac import Capability, has_capability
from ..security.tokens import verify_token_hash
from ..services.users import Actor
from ..storage.base import Feature

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


# ---------------------------------------------------------------------------
# services access
# ---------------------------------------------------------------------------
def get_services(request: Request):  # noqa: ANN201
    return request.app.state.services


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


ServicesDep = Annotated[Any, Depends(get_services)]
ConfigDep = Annotated[AppConfig, Depends(get_config)]


# ---------------------------------------------------------------------------
# client identity
# ---------------------------------------------------------------------------
def client_ip(request: Request) -> str | None:
    """Return the client address as a valid IP string, or ``None``.

    ``X-Forwarded-For`` is only consulted when the deployment declares trusted
    proxy hops, so a direct client cannot spoof its address. Values that are
    not IP addresses (some test clients, some proxies) are discarded rather
    than written into ``INET`` columns.
    """
    import ipaddress

    cfg: AppConfig = request.app.state.config
    candidate: str | None = None
    if cfg.trust_proxy_hops > 0:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            candidate = forwarded.split(",")[0].strip() or None
    if candidate is None:
        peer = request.client
        candidate = peer.host if peer else None
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
def current_actor(request: Request) -> Actor | None:
    token = request.cookies.get(request.app.state.config.security.cookie_name)
    if not token:
        return None
    services = get_services(request)
    row = services.users.load_session(token)
    if row is None:
        return None
    services.users.touch_session(
        str(row["session_id"]), request.app.state.config.security.session_idle_ttl_s
    )
    actor = Actor(
        user_id=str(row["user_id"]),
        username=str(row["username"]),
        role=str(row["role"]),
        session_id=str(row["session_id"]),
    )
    request.state.actor = actor
    request.state.session_token = token
    request.state.csrf_hash = str(row["csrf_token_hash"])
    return actor


def require_actor(request: Request) -> Actor:
    actor = current_actor(request)
    if actor is None:
        raise AuthError("authentication is required")
    return actor


def require_csrf(request: Request) -> None:
    """Double-submit CSRF check for state-changing requests.

    Session cookies are sent automatically by the browser, so every mutating
    request must also present the CSRF token that was issued with the session.

    This resolves the session itself rather than relying on ``require_actor``
    having run first, so the guard does not depend on dependency ordering.
    """
    cfg: AppConfig = request.app.state.config
    if request.method in SAFE_METHODS:
        return

    expected_hash = getattr(request.state, "csrf_hash", None)
    if expected_hash is None:
        token = request.cookies.get(cfg.security.cookie_name)
        if not token:
            raise AuthError("authentication is required")
        services = get_services(request)
        row = services.users.load_session(token)
        if row is None:
            raise AuthError("authentication is required")
        expected_hash = str(row["csrf_token_hash"])

    presented = request.headers.get(cfg.security.csrf_header_name)
    if not presented:
        raise PermissionError_(f"{cfg.security.csrf_header_name} header is required")
    if not verify_token_hash(presented, str(expected_hash)):
        raise PermissionError_("CSRF token is invalid")


def set_session_cookies(
    response: Response,
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    max_age: int,
) -> None:
    common = {
        "max_age": max_age,
        "path": "/",
        "secure": config.security.cookie_secure,
        "samesite": config.security.cookie_samesite,  # type: ignore[arg-type]
    }
    response.set_cookie(
        config.security.cookie_name,
        session_token,
        httponly=True,  # never readable from JavaScript
        **common,
    )
    # Readable by the console so it can echo the token in the CSRF header.
    response.set_cookie(
        config.security.csrf_cookie_name,
        csrf_token,
        httponly=False,
        **common,
    )


def clear_session_cookies(response: Response, config: AppConfig) -> None:
    response.delete_cookie(config.security.cookie_name, path="/")
    response.delete_cookie(config.security.csrf_cookie_name, path="/")


# ---------------------------------------------------------------------------
# authorisation
# ---------------------------------------------------------------------------
def require_capability(capability: Capability):
    """Dependency factory requiring a capability."""

    def dependency(request: Request) -> Actor:
        actor = require_actor(request)
        if not has_capability(actor.role, capability):
            raise PermissionError_(
                f"role {actor.role!r} lacks the {capability.value!r} capability"
            )
        return actor

    return dependency


def require_admin(request: Request) -> Actor:
    actor = require_actor(request)
    if not actor.is_admin:
        raise PermissionError_("this operation requires the administrator role")
    return actor


def require_feature(feature: Feature):
    """Dependency factory failing with 501 when the engine lacks a feature."""

    def dependency(request: Request) -> Actor:
        services = get_services(request)
        services.capabilities.require(feature)
        return require_actor(request)

    return dependency


# ---------------------------------------------------------------------------
# rate limiting (in-process, per instance)
# ---------------------------------------------------------------------------
class SlidingWindowLimiter:
    """Fixed-memory sliding-window limiter.

    Deliberately per-process: it protects a single instance from credential
    stuffing without adding Redis to the deployment. Multiple replicas each
    enforce their own window. docs/SECURITY.md records this limitation.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = __import__("threading").Lock()

    def check(self, key: str, limit: int, window_s: int = 60) -> None:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            cutoff = now - window_s
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(window_s - (now - bucket[0])))
                raise RateLimitedError(
                    "too many attempts; retry later",
                    details={"retry_after_seconds": retry_after},
                )
            bucket.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_limiter = SlidingWindowLimiter()


def limiter() -> SlidingWindowLimiter:
    return _limiter


def rate_limit(limit: int, *, scope: str):
    """Dependency factory applying a per-IP rate limit."""

    def dependency(request: Request) -> None:
        ip = client_ip(request) or "unknown"
        _limiter.check(f"{scope}:{ip}", limit)

    return dependency


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("value must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValidationError(f"value must be between {minimum} and {maximum}")
    return parsed


def parse_datetime(value: str | None, field: str) -> dt.datetime | None:
    if not value:
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)