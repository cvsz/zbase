"""FastAPI application factory for S3Vault.

Wires configuration, database, storage adapter, services and routers, and
installs the middleware that enforces the security posture described in
docs/SECURITY.md.
"""

from __future__ import annotations

import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from .config import AppConfig, ConfigError, load_config
from .db import Database, Migrator
from .errors import AppError
from .logging_setup import configure_logging
from .security.envelope import load_kek
from .services.audit import AuditService
from .services.buckets import BucketService
from .services.credentials import CredentialService
from .services.quotas import QuotaService
from .services.safety import SafetyGuard
from .services.usage import UsageService
from .services.users import UserService
from .storage import build_adapter
from .storage.base import Capabilities, StorageAdapter

log = structlog.get_logger("s3vault")


class Services:
    """Container holding the constructed collaborators for one process."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.db = Database(config)
        self.adapter: StorageAdapter = build_adapter(config)
        self.audit = AuditService(self.db)
        self.users = UserService(
            self.db, password_min_length=config.security.password_min_length
        )
        self.credentials = CredentialService(self.db, kek=load_kek(config.security.kek_base64))
        self.quotas = QuotaService(self.db, config)
        self.usage = UsageService(self.db, self.adapter)
        self.safety = SafetyGuard(config, self.db, self.audit)
        self._capabilities: Capabilities | None = None

    @property
    def capabilities(self) -> Capabilities:
        if self._capabilities is None:
            self._capabilities = self.adapter.probe()
        return self._capabilities

    def buckets(self) -> BucketService:
        return BucketService(self.db, self.adapter, self.capabilities)

    def close(self) -> None:
        self.db.close()


def create_app(
    config: AppConfig | None = None,
    *,
    run_migrations: bool = True,
) -> FastAPI:
    cfg = config or load_config()
    configure_logging(cfg)
    services = Services(cfg)

    if run_migrations:
        Migrator(services.db).apply()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        log.info(
            "s3vault.starting",
            env=cfg.env,
            engine=cfg.storage.endpoint_url,
            hostname=cfg.public_hostname,
            issues=list(cfg.issues),
        )
        try:
            services.capabilities
            log.info(
                "s3vault.engine_ready",
                engine=services.capabilities.engine_name,
                version=services.capabilities.engine_version,
                supported=len(services.capabilities.supported),
            )
        except AppError as exc:
            # Readiness will report the failure; do not crash-loop the API.
            log.error("s3vault.engine_unavailable", error=exc.code)
        yield
        services.close()
        log.info("s3vault.stopped")

    app = FastAPI(
        title="S3Vault",
        version="0.1.0",
        description="Self-hosted S3-compatible object storage control plane",
        lifespan=lifespan,
        docs_url="/api/docs" if not cfg.is_production else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if not cfg.is_production else None,
    )
    app.state.services = services
    app.state.config = cfg

    _install_middleware(app, cfg)
    _install_error_handlers(app)
    # Operational routes are registered before the console's SPA catch-all, or
    # `GET /{full_path:path}` would shadow /healthz and /readyz.
    _install_operational_routes(app, services)
    _install_routes(app, cfg, services)

    return app


def _install_operational_routes(app: FastAPI, services: "Services") -> None:
    @app.get("/healthz", include_in_schema=False)
    async def liveness() -> dict[str, Any]:
        """Liveness: the process is up. No dependency checks, no secrets."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readiness() -> JSONResponse:
        """Readiness: dependencies reachable. Reveals only pass/fail."""
        checks: dict[str, bool] = {"database": services.db.ping()}
        try:
            checks["storage"] = services.adapter.healthy()
        except Exception:  # noqa: BLE001
            checks["storage"] = False
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready", "checks": checks},
        )


# ---------------------------------------------------------------------------
# middleware
# ---------------------------------------------------------------------------
def _install_middleware(app: FastAPI, cfg: AppConfig) -> None:
    # Reject oversized request bodies before they are buffered or parsed.
    # `REQUEST_BODY_LIMIT_BYTES` is therefore a real limit, not a comment.
    limit = cfg.uploads.request_body_limit_bytes

    @app.middleware("http")
    async def enforce_body_limit(request: Request, call_next):  # noqa: ANN202
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "payload_too_large",
                                "message": (
                                    "request body exceeds the configured limit of "
                                    f"{limit} bytes; use multipart or presigned upload"
                                ),
                                "details": {"limit_bytes": limit},
                            }
                        },
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "code": "validation_error",
                            "message": "Content-Length is not a valid integer",
                            "details": {},
                        }
                    },
                )
        # Chunked bodies declare no length; enforce while streaming.
        body = await request.body()
        if len(body) > limit:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "payload_too_large",
                        "message": (
                            f"request body exceeds the configured limit of {limit} bytes"
                        ),
                        "details": {"limit_bytes": limit},
                    }
                },
            )
        return await call_next(request)

    # Trust proxy headers only when the operator has declared trusted hops.
    # `trusted_hosts` is the ASGI-level control; uvicorn additionally needs
    # `forwarded_allow_ips` at its own layer (see main.py).
    if cfg.trust_proxy_hops > 0:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])

    # Baseline security headers. CSP is strict: the console ships no inline script.
    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # noqa: ANN202
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        if cfg.is_production:
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        return response

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # noqa: ANN202
        request_id = request.headers.get("X-Request-ID") or secrets.token_hex(8)
        structlog.contextvars.bind_contextvars(request_id=request_id)
        request.state.request_id = request_id
        request.state.started = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        duration_ms = (time.monotonic() - request.state.started) * 1000
        # Never log the path of a presigned-URL request body or credentials.
        safe_path = request.url.path
        log.info(
            "http.request",
            method=request.method,
            path=safe_path[:200],
            status=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        response.headers["X-Request-ID"] = request_id
        return response

    app.add_middleware(GZipMiddleware, minimum_size=1024)


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):  # noqa: ANN202
        if exc.status >= 500:
            log.error("app.error", code=exc.code, error=exc.message)
        else:
            log.info("app.rejected", code=exc.code, status=exc.status, error=exc.message)
        return JSONResponse(status_code=exc.status, content=exc.to_payload())

    @app.exception_handler(ConfigError)
    async def handle_config_error(request: Request, exc: ConfigError):  # noqa: ANN202
        log.error("config.invalid", error=str(exc))
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "configuration_invalid",
                    "message": "the server configuration is invalid",
                    "details": {"reason": str(exc)},
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):  # noqa: ANN202
        log.error("app.unhandled", error=f"{type(exc).__name__}", detail=str(exc)[:300])
        # Never echo an internal message to an unauthenticated caller.
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "an unexpected error occurred",
                    "details": {},
                }
            },
        )


def _install_routes(app: FastAPI, cfg: AppConfig, services: "Services") -> None:
    """Attach the API routers and the management console.

    Must run after the operational routes are registered: the console mounts a
    ``GET /{full_path:path}`` catch-all that would otherwise shadow the health
    endpoints.
    """
    from .api import register_routes

    register_routes(app, cfg, services)
