"""Fail-closed configuration loading and validation.

Design rules (see docs/CONFIGURATION.md):

* Configuration is read once at startup into an immutable, typed object.
* Missing or insecure production settings raise :class:`ConfigError`.
  The process must not fall back to a weak default in production.
* Validation never prints secret material. It reports *names* and *reasons*
  only, so startup logs are safe to ship.
* ``S3VAULT_ENV`` selects the profile. ``test`` disables external side effects.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Final

ENV_DEV: Final = "development"
ENV_TEST: Final = "test"
ENV_PROD: Final = "production"
VALID_ENVS: Final = frozenset({ENV_DEV, ENV_TEST, ENV_PROD})

#: Placeholders that must never reach production.
FORBIDDEN_SECRET_VALUES: Final = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "change-me-in-production",
        "REPLACE_ME",
        "secret",
        "password",
        "admin",
        "REPLACE_ME_AT_LEAST_32_BYTES",
    }
)


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed, or insecure."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got a non-numeric value") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean-like value (true/false/1/0/yes/no)")


def _require(name: str) -> str:
    value = _env(name)
    if value is None:
        raise ConfigError(f"{name} is required but was not set")
    if value.lower() in FORBIDDEN_SECRET_VALUES:
        # Report the variable name only. Never echo the offending value.
        raise ConfigError(f"{name} is set to a placeholder/weak value; supply a real secret")
    return value


def _assert_min_bytes(name: str, value: str, minimum: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) < minimum:
        raise ConfigError(f"{name} must be at least {minimum} bytes, got {len(raw)}")
    return value


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """PostgreSQL connection settings for application metadata."""

    dsn: str
    pool_min: int
    pool_max: int
    connect_timeout_s: int
    statement_timeout_ms: int

    @property
    def safe_dsn(self) -> str:
        """DSN with the password replaced, safe for logs and health output."""
        return re.sub(r"://([^:/@]+):([^@]*)@", r"://\1:***@", self.dsn)


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Settings for the S3 storage engine (SeaweedFS S3 gateway)."""

    endpoint_url: str
    region: str
    root_access_key: str
    root_secret_key: str
    addressing_style: str  # "path" or "virtual"
    verify_tls: bool
    connect_timeout_s: int
    read_timeout_s: int
    max_attempts: int

    @property
    def public_endpoint(self) -> str:
        return self.endpoint_url


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    """Session, CSRF, rate-limit and key-encryption settings."""

    kek_base64: str
    session_ttl_s: int
    session_idle_ttl_s: int
    cookie_name: str
    cookie_secure: bool
    cookie_samesite: str
    csrf_cookie_name: str
    csrf_header_name: str
    login_rate_limit_per_min: int
    api_rate_limit_per_min: int
    bootstrap_admin_user: str | None
    bootstrap_admin_email: str | None
    bootstrap_admin_password: str | None
    password_min_length: int

    @property
    def is_production(self) -> bool:
        return self.cookie_secure


@dataclass(frozen=True, slots=True)
class UploadConfig:
    """Upload limits. Values are validated against engine limits in tests."""

    max_object_bytes: int
    max_part_bytes: int
    min_part_bytes: int
    multipart_threshold_bytes: int
    presign_ttl_s: int
    presign_max_ttl_s: int
    request_body_limit_bytes: int


@dataclass(frozen=True, slots=True)
class QuotaConfig:
    """Default quota policy applied when a user/bucket has no explicit policy.

    ``-1`` means unlimited. Quotas are enforced by the application against
    engine-reported usage; see docs/ARCHITECTURE.md#quotas.
    """

    default_user_bytes: int
    default_bucket_bytes: int
    default_max_object_bytes: int
    max_buckets_per_user: int


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    """Operational safety gates (docs/OPERATIONS.md#safety-gates).

    These are enforced in code paths that mutate persistent state. Missing or
    non-conforming values must not enable destructive behaviour.
    """

    require_dry_run: bool
    require_backup: bool
    allow_production_changes: bool
    backup_dir: str | None
    backup_retention_days: int


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Fully validated application configuration."""

    env: str
    public_hostname: str
    console_url: str
    s3_public_endpoint: str
    log_level: str
    log_json: bool
    trust_proxy_hops: int
    metrics_enabled: bool
    db: DatabaseConfig
    storage: StorageConfig
    security: SecurityConfig
    uploads: UploadConfig
    quotas: QuotaConfig
    safety: SafetyConfig
    issues: tuple[str, ...] = field(default=())

    @property
    def is_production(self) -> bool:
        return self.env == ENV_PROD

    @property
    def is_test(self) -> bool:
        return self.env == ENV_TEST

    @property
    def has_external_side_effects(self) -> bool:
        """False in tests so suites cannot reach real engines or SMTP."""
        return self.env != ENV_TEST


def _validate_engine_endpoint(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise ConfigError("STORAGE_ENGINE_ENDPOINT must start with http:// or https://")
    if " " in url:
        raise ConfigError("STORAGE_ENGINE_ENDPOINT must not contain whitespace")


def load_config(env: str | None = None) -> AppConfig:
    """Build and validate an :class:`AppConfig` from the process environment.

    Raises:
        ConfigError: on any missing, malformed, or insecure setting.
    """
    resolved_env = (env or _env("S3VAULT_ENV", ENV_DEV) or ENV_DEV).lower()
    if resolved_env not in VALID_ENVS:
        raise ConfigError(
            f"S3VAULT_ENV must be one of {sorted(VALID_ENVS)}; got an unknown value"
        )
    is_prod = resolved_env == ENV_PROD
    is_test = resolved_env == ENV_TEST
    issues: list[str] = []

    # --- database -----------------------------------------------------
    db_dsn = _require("DATABASE_URL")
    if is_prod and db_dsn.startswith("postgres://"):
        issues.append("DATABASE_URL should use the postgresql:// scheme in production")
    db = DatabaseConfig(
        dsn=db_dsn,
        pool_min=_env_int("DATABASE_POOL_MIN", 1, minimum=0),
        pool_max=_env_int("DATABASE_POOL_MAX", 10, minimum=1),
        connect_timeout_s=_env_int("DATABASE_CONNECT_TIMEOUT_S", 5, minimum=1),
        statement_timeout_ms=_env_int("DATABASE_STATEMENT_TIMEOUT_MS", 15_000, minimum=0),
    )
    if db.pool_min > db.pool_max:
        raise ConfigError("DATABASE_POOL_MIN must not exceed DATABASE_POOL_MAX")

    # --- storage engine ----------------------------------------------
    endpoint = _require("STORAGE_ENGINE_ENDPOINT").rstrip("/")
    _validate_engine_endpoint(endpoint)
    plaintext_allowed = _env_bool("STORAGE_PLAINTEXT_ALLOWED", False)
    if is_prod and endpoint.startswith("http://") and not plaintext_allowed:
        issues.append(
            "STORAGE_ENGINE_ENDPOINT is plaintext HTTP in production; either terminate "
            "TLS to the engine or set STORAGE_PLAINTEXT_ALLOWED=true when the engine "
            "is only reachable over an isolated internal network"
        )
    addressing = (_env("STORAGE_ADDRESSING_STYLE", "path") or "path").lower()
    if addressing not in {"path", "virtual"}:
        raise ConfigError("STORAGE_ADDRESSING_STYLE must be 'path' or 'virtual'")
    storage = StorageConfig(
        endpoint_url=endpoint,
        region=_env("STORAGE_REGION", "us-east-1") or "us-east-1",
        root_access_key=_require("STORAGE_ROOT_ACCESS_KEY"),
        root_secret_key=_assert_min_bytes(
            "STORAGE_ROOT_SECRET_KEY", _require("STORAGE_ROOT_SECRET_KEY"), 8
        ),
        addressing_style=addressing,
        verify_tls=_env_bool("STORAGE_VERIFY_TLS", True),
        connect_timeout_s=_env_int("STORAGE_CONNECT_TIMEOUT_S", 5, minimum=1),
        read_timeout_s=_env_int("STORAGE_READ_TIMEOUT_S", 60, minimum=1),
        max_attempts=_env_int("STORAGE_MAX_ATTEMPTS", 3, minimum=1),
    )
    if not is_prod and storage.root_access_key == storage.root_secret_key:
        raise ConfigError("STORAGE_ROOT_ACCESS_KEY and STORAGE_ROOT_SECRET_KEY must differ")

    # --- security ------------------------------------------------------
    kek = _require("KEK_BASE64")
    try:
        import base64

        kek_bytes = base64.b64decode(kek, validate=True)
    except Exception as exc:  # noqa: BLE001 - surfaced as ConfigError
        raise ConfigError("KEK_BASE64 must be valid standard base64") from exc
    if len(kek_bytes) != 32:
        raise ConfigError(
            f"KEK_BASE64 must decode to exactly 32 bytes, got {len(kek_bytes)}"
        )

    cookie_secure = _env_bool("COOKIE_SECURE", True)
    if is_prod and not cookie_secure:
        issues.append("COOKIE_SECURE must be true in production")
    samesite = (_env("COOKIE_SAMESITE", "lax") or "lax").lower()
    if samesite not in {"lax", "strict", "none"}:
        raise ConfigError("COOKIE_SAMESITE must be one of lax, strict, none")
    if samesite == "none" and not cookie_secure:
        raise ConfigError("COOKIE_SAMESITE=none requires COOKIE_SECURE=true")

    session_ttl = _env_int("SESSION_TTL_S", 3600, minimum=60)
    idle_ttl = _env_int("SESSION_IDLE_TTL_S", 900, minimum=60)

    security = SecurityConfig(
        kek_base64=kek,
        session_ttl_s=session_ttl,
        session_idle_ttl_s=idle_ttl,
        cookie_name=_env("SESSION_COOKIE_NAME", "s3vault_session") or "s3vault_session",
        cookie_secure=cookie_secure,
        cookie_samesite=samesite,
        csrf_cookie_name=_env("CSRF_COOKIE_NAME", "s3vault_csrf")
        or "s3vault_csrf",
        csrf_header_name=_env("CSRF_HEADER_NAME", "X-CSRF-Token") or "X-CSRF-Token",
        login_rate_limit_per_min=_env_int("LOGIN_RATE_LIMIT_PER_MIN", 5, minimum=1),
        api_rate_limit_per_min=_env_int("API_RATE_LIMIT_PER_MIN", 600, minimum=1),
        bootstrap_admin_user=_env("BOOTSTRAP_ADMIN_USER"),
        bootstrap_admin_email=_env("BOOTSTRAP_ADMIN_EMAIL"),
        bootstrap_admin_password=_env("BOOTSTRAP_ADMIN_PASSWORD"),
        password_min_length=_env_int("PASSWORD_MIN_LENGTH", 12, minimum=8),
    )
    if security.session_idle_ttl_s > security.session_ttl_s:
        raise ConfigError("SESSION_IDLE_TTL_S must not exceed SESSION_TTL_S")

    bootstrap_fields = (
        security.bootstrap_admin_user,
        security.bootstrap_admin_email,
        security.bootstrap_admin_password,
    )
    if any(bootstrap_fields) and not all(bootstrap_fields):
        raise ConfigError(
            "BOOTSTRAP_ADMIN_USER, BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD "
            "must be provided together"
        )
    if security.bootstrap_admin_password is not None:
        _assert_min_bytes(
            "BOOTSTRAP_ADMIN_PASSWORD",
            security.bootstrap_admin_password,
            security.password_min_length,
        )

    # --- uploads --------------------------------------------------------
    max_object = _env_int("MAX_OBJECT_BYTES", 5 * 1024**3, minimum=1)
    max_part = _env_int("MAX_PART_BYTES", 100 * 1024**2, minimum=5 * 1024**2)
    min_part = _env_int("MIN_PART_BYTES", 5 * 1024**2, minimum=1)
    if max_part < min_part:
        raise ConfigError("MAX_PART_BYTES must be >= MIN_PART_BYTES")
    threshold = _env_int("MULTIPART_THRESHOLD_BYTES", max_part, minimum=1)
    presign_ttl = _env_int("PRESIGN_TTL_S", 900, minimum=60)
    presign_max = _env_int("PRESIGN_MAX_TTL_S", 3600, minimum=presign_ttl)
    uploads = UploadConfig(
        max_object_bytes=max_object,
        max_part_bytes=max_part,
        min_part_bytes=min_part,
        multipart_threshold_bytes=threshold,
        presign_ttl_s=presign_ttl,
        presign_max_ttl_s=presign_max,
        request_body_limit_bytes=_env_int("REQUEST_BODY_LIMIT_BYTES", 8 * 1024**2, minimum=1),
    )
    if uploads.request_body_limit_bytes > uploads.max_object_bytes:
        issues.append(
            "REQUEST_BODY_LIMIT_BYTES exceeds MAX_OBJECT_BYTES; large uploads must use "
            "multipart or presigned direct upload"
        )

    # --- quotas ---------------------------------------------------------
    quotas = QuotaConfig(
        default_user_bytes=_env_int("DEFAULT_USER_QUOTA_BYTES", -1),
        default_bucket_bytes=_env_int("DEFAULT_BUCKET_QUOTA_BYTES", -1),
        default_max_object_bytes=_env_int("DEFAULT_MAX_OBJECT_BYTES", max_object, minimum=1),
        max_buckets_per_user=_env_int("MAX_BUCKETS_PER_USER", 100, minimum=1),
    )
    for env_name, value in (
        ("DEFAULT_USER_QUOTA_BYTES", quotas.default_user_bytes),
        ("DEFAULT_BUCKET_QUOTA_BYTES", quotas.default_bucket_bytes),
    ):
        if value != -1 and value <= 0:
            raise ConfigError(f"{env_name} must be -1 (unlimited) or a positive byte count")

    # --- safety gates ---------------------------------------------------
    safety = SafetyConfig(
        require_dry_run=_env_bool("S3VAULT_REQUIRE_DRY_RUN", False),
        require_backup=_env_bool("S3VAULT_REQUIRE_BACKUP", False),
        allow_production_changes=_env_bool("S3VAULT_ALLOW_PRODUCTION_CHANGES", False),
        backup_dir=_env("S3VAULT_BACKUP_DIR"),
        backup_retention_days=_env_int("S3VAULT_BACKUP_RETENTION_DAYS", 14, minimum=1),
    )
    if safety.require_backup and not safety.backup_dir:
        raise ConfigError(
            "S3VAULT_REQUIRE_BACKUP=true requires S3VAULT_BACKUP_DIR to be set"
        )
    if is_prod and safety.allow_production_changes:
        issues.append(
            "S3VAULT_ALLOW_PRODUCTION_CHANGES=true disables the production mutation guard"
        )
    if safety.require_dry_run and safety.allow_production_changes:
        raise ConfigError(
            "S3VAULT_REQUIRE_DRY_RUN=true conflicts with "
            "S3VAULT_ALLOW_PRODUCTION_CHANGES=true"
        )

    # --- public surface -------------------------------------------------
    public_hostname = _require("PUBLIC_HOSTNAME")
    if is_prod and public_hostname in {"localhost", "127.0.0.1", "example.com"}:
        issues.append("PUBLIC_HOSTNAME is still a placeholder value")

    trust_hops = _env_int("TRUSTED_PROXY_HOPS", 0, minimum=0)
    if is_prod and trust_hops < 1:
        issues.append(
            "TRUSTED_PROXY_HOPS should be >= 1 in production so client IPs are "
            "attributed to the real proxy hop"
        )

    config = AppConfig(
        env=resolved_env,
        public_hostname=public_hostname,
        console_url=_env("CONSOLE_URL", f"https://{public_hostname}")
        or f"https://{public_hostname}",
        s3_public_endpoint=_env("S3_PUBLIC_ENDPOINT", f"https://{public_hostname}")
        or f"https://{public_hostname}",
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        log_json=_env_bool("LOG_JSON", True),
        trust_proxy_hops=trust_hops,
        metrics_enabled=_env_bool("METRICS_ENABLED", True),
        db=db,
        storage=storage,
        security=security,
        uploads=uploads,
        quotas=quotas,
        safety=safety,
        issues=tuple(issues),
    )

    if is_prod and config.issues:
        # Fail closed: refuse to start rather than run insecurely.
        joined = "\n  - ".join(config.issues)
        raise ConfigError(f"insecure production configuration:\n  - {joined}")

    if not is_test and config.storage.root_secret_key == "test-secret-key":
        issues.append("STORAGE_ROOT_SECRET_KEY looks like a test value")
        config = AppConfig(
            **{
                **{f.name: getattr(config, f.name) for f in _dataclass_fields(config)},
                "issues": tuple(issues),
            }
        )

    return config


def _dataclass_fields(obj: AppConfig):
    import dataclasses

    return dataclasses.fields(obj)


def generate_kek() -> str:
    """Return a fresh 32-byte key-encryption key as base64 (operator utility)."""
    import base64

    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def random_secret(nbytes: int = 32) -> str:
    """Return a URL-safe random secret string."""
    return secrets.token_urlsafe(nbytes)