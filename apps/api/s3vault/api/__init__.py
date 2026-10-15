"""HTTP API surface for S3Vault."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import AppConfig

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Services


def register_routes(app: FastAPI, config: AppConfig, services: "Services") -> None:
    """Attach every router plus the console to ``app``."""
    from .routes_auth import router as auth_router
    from .routes_auth import users_router
    from .routes_buckets import router as buckets_router
    from .routes_system import audit_router, router as credentials_router, system_router

    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(buckets_router)
    app.include_router(credentials_router)
    app.include_router(audit_router)
    app.include_router(system_router)

    _mount_console(app)


def _web_root() -> Any:
    from pathlib import Path

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "web"
        if candidate.is_dir():
            return candidate
    return None


def _mount_console(app: FastAPI) -> None:
    """Serve the management console.

    The console is a no-build static bundle (see docs/DEVELOPMENT.md). Assets are
    mounted read-only; unknown non-API paths fall back to ``index.html`` so the
    single-page app owns client routing.
    """
    root = _web_root()
    if root is None or not root.is_dir():  # pragma: no cover - packaging fault
        @app.get("/", include_in_schema=False)
        async def console_missing() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "console_missing", "message": "console assets not found"}},
            )

        return

    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = root / "index.html"

    @app.get("/", include_in_schema=False)
    async def console_index() -> Any:
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> Any:
        return FileResponse(root / "favicon.svg", media_type="image/svg+xml")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def console_spa(full_path: str, request: Request) -> Any:
        # Never shadow the API or the operational endpoints.
        if full_path.startswith(("api/", "healthz", "readyz")):
            return JSONResponse(
                status_code=404, content={"error": {"code": "not_found", "message": "not found"}}
            )
        candidate = root / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-store"})