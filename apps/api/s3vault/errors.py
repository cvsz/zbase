"""Typed application errors with stable machine-readable codes.

API responses use ``{"error": {"code": ..., "message": ..., "details": ...}}``.
Codes are part of the public contract and must stay stable.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for expected, client-visible failures."""

    code = "internal_error"
    status = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class ValidationError(AppError):
    code = "validation_error"
    status = 400


class AuthError(AppError):
    code = "unauthenticated"
    status = 401


class PermissionError_(AppError):
    code = "forbidden"
    status = 403


class NotFoundError(AppError):
    code = "not_found"
    status = 404


class ConflictError(AppError):
    code = "conflict"
    status = 409


class QuotaExceededError(AppError):
    code = "quota_exceeded"
    status = 409


class RateLimitedError(AppError):
    code = "rate_limited"
    status = 429


class StorageError(AppError):
    """The storage engine refused or failed an operation."""

    code = "storage_error"
    status = 502


class SafetyGateError(AppError):
    """A required operational safety gate refused the operation."""

    code = "safety_gate_blocked"
    status = 423


class UnsupportedFeatureError(AppError):
    """The engine does not implement a feature the caller asked for."""

    code = "unsupported_feature"
    status = 501