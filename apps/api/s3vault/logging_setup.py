"""Structured logging with mandatory secret redaction.

Every log record passes through :func:`_redact` so that accidentally logged
secrets, bearer tokens, presigned URLs and signed query strings are masked
before they reach a handler.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from .config import AppConfig

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "password",
        "passwd",
        "secret",
        "secret_access_key",
        "secretkey",
        "access_key",
        "access_key_id",
        "session_token",
        "token",
        "csrf_token",
        "api_key",
        "kek",
        "kek_base64",
        "cookie",
        "set-cookie",
        "x-amz-security-token",
        "presigned_url",
        "x_amz_credential",
        "x_amz_signature",
        "private_key",
    }
)

_QUERY_SECRET_RE = re.compile(
    r"(?i)\b(X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token|"
    r"Signature|AWSAccessKeyId|token|password)=[^&\s]+"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_BASIC_RE = re.compile(r"(?i)\b(Basic|Bearer)\s+[A-Za-z0-9._~+/=-]{8,}")


def redact_text(value: str) -> str:
    """Mask credential-looking substrings inside free text."""
    value = _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}=***", value)
    value = _JWT_RE.sub("***", value)
    value = _BASIC_RE.sub(lambda m: f"{m.group(1)} ***", value)
    return value


def _redact(value: Any, _depth: int = 0) -> Any:
    if _depth > 6:
        return "..."
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
                out[key] = "***"
            else:
                out[key] = _redact(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor that redacts the whole event."""
    return _redact(event_dict)  # type: ignore[no-any-return]


def configure_logging(config: AppConfig) -> None:
    """Install structlog + stdlib logging configuration for the process.

    Uses the stdlib ``BoundLogger``/``LoggerFactory`` pair with
    ``render_to_log_kwargs``. structlog's ``WriteLogger.msg()`` accepts only a
    single positional message, so ``logger.info("event", key=value)`` would
    raise ``TypeError`` with the print/write factories; ``render_to_log_kwargs``
    moves the event dictionary into stdlib logging keyword arguments instead.
    """
    level = getattr(logging, config.log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.render_to_log_kwargs,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stderr,
        force=True,
    )
    # Uvicorn's own loggers must not bypass redaction.
    for noisy in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        std_logger = logging.getLogger(noisy)
        std_logger.handlers.clear()
        std_logger.propagate = True