"""ASGI entrypoint: ``python -m s3vault`` or ``uvicorn s3vault.main:app``."""

from __future__ import annotations

from .app import create_app
from .config import load_config

app = create_app()


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "s3vault.main:app",
        host=cfg.host if hasattr(cfg, "host") else "0.0.0.0",
        port=3000,
        log_config=None,  # structlog owns formatting and redaction
        access_log=False,
        proxy_headers=cfg.trust_proxy_hops > 0,
        forwarded_allow_ips="*" if cfg.trust_proxy_hops > 0 else None,
    )


if __name__ == "__main__":  # pragma: no cover
    main()