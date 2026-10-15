"""Unit tests for configuration validation and secret handling."""

from __future__ import annotations

import base64
import os

import pytest

from s3vault.config import (
    ENV_PROD,
    ConfigError,
    generate_kek,
    load_config,
)
from s3vault.logging_setup import redact_processor, redact_text

BASE_ENV = {
    "DATABASE_URL": "postgresql://s3vault:s3vault@127.0.0.1:5432/s3vault",
    "STORAGE_ENGINE_ENDPOINT": "https://s3.internal:8333",
    "STORAGE_ROOT_ACCESS_KEY": "AKIAEXAMPLE",
    "STORAGE_ROOT_SECRET_KEY": "super-secret-root-key",
    "PUBLIC_HOSTNAME": "s3.example.com",
    "KEK_BASE64": base64.b64encode(b"\x01" * 32).decode(),
    "COOKIE_SECURE": "true",
    "TRUSTED_PROXY_HOPS": "1",
}


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for key in list(os.environ):
        if key.startswith(("S3VAULT_", "DATABASE_", "STORAGE_", "SESSION_", "COOKIE_", "CSRF_", "KEK_")):
            monkeypatch.delenv(key, raising=False)
    for key, value in {**BASE_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)


class TestProductionFailClosed:
    def test_minimal_production_config_is_accepted(self, monkeypatch):
        _env(monkeypatch)
        cfg = load_config(ENV_PROD)
        assert cfg.is_production
        assert cfg.security.cookie_secure is True

    def test_missing_database_url_fails(self, monkeypatch):
        _env(monkeypatch, DATABASE_URL="")
        with pytest.raises(ConfigError) as exc:
            load_config(ENV_PROD)
        assert "DATABASE_URL" in str(exc.value)

    def test_missing_kek_fails(self, monkeypatch):
        _env(monkeypatch, KEK_BASE64="")
        with pytest.raises(ConfigError) as exc:
            load_config(ENV_PROD)
        assert "KEK_BASE64" in str(exc.value)

    def test_short_kek_fails(self, monkeypatch):
        _env(monkeypatch, KEK_BASE64=base64.b64encode(b"tooshort").decode())
        with pytest.raises(ConfigError, match="32 bytes"):
            load_config(ENV_PROD)

    def test_non_base64_kek_fails(self, monkeypatch):
        _env(monkeypatch, KEK_BASE64="not!base64!")
        with pytest.raises(ConfigError, match="base64"):
            load_config(ENV_PROD)

    def test_placeholder_secret_is_rejected(self, monkeypatch):
        _env(monkeypatch, SESSION_SECRET_UNUSED="x")
        _env(monkeypatch, STORAGE_ROOT_SECRET_KEY="changeme")
        with pytest.raises(ConfigError):
            load_config(ENV_PROD)

    def test_plaintext_engine_endpoint_rejected_in_prod(self, monkeypatch):
        _env(monkeypatch, STORAGE_ENGINE_ENDPOINT="http://s3.internal:8333")
        with pytest.raises(ConfigError) as exc:
            load_config(ENV_PROD)
        assert "plaintext" in str(exc.value)

    def test_plaintext_internal_engine_allowed_when_opted_in(self, monkeypatch):
        """A compose deployment reaches the engine over an internal network.

        Plaintext is acceptable only when the operator states the engine is
        not exposed, which is opt-in rather than inferred.
        """
        _env(
            monkeypatch,
            STORAGE_ENGINE_ENDPOINT="http://seaweedfs:8333",
            STORAGE_PLAINTEXT_ALLOWED="true",
        )
        cfg = load_config(ENV_PROD)
        assert cfg.storage.endpoint_url == "http://seaweedfs:8333"

    def test_opt_in_is_still_refused_without_the_flag(self, monkeypatch):
        _env(monkeypatch, STORAGE_ENGINE_ENDPOINT="http://seaweedfs:8333")
        with pytest.raises(ConfigError):
            load_config(ENV_PROD)

    def test_insecure_cookie_rejected_in_prod(self, monkeypatch):
        _env(monkeypatch, COOKIE_SECURE="false")
        with pytest.raises(ConfigError, match="COOKIE_SECURE"):
            load_config(ENV_PROD)

    def test_placeholder_hostname_rejected_in_prod(self, monkeypatch):
        _env(monkeypatch, PUBLIC_HOSTNAME="example.com")
        with pytest.raises(ConfigError, match="PUBLIC_HOSTNAME"):
            load_config(ENV_PROD)

    def test_production_change_guard_rejected_in_prod(self, monkeypatch):
        _env(monkeypatch, S3VAULT_ALLOW_PRODUCTION_CHANGES="true")
        with pytest.raises(ConfigError, match="production mutation guard"):
            load_config(ENV_PROD)

    def test_dry_run_conflicts_with_production_changes(self, monkeypatch):
        _env(
            monkeypatch,
            S3VAULT_REQUIRE_DRY_RUN="1",
            S3VAULT_ALLOW_PRODUCTION_CHANGES="1",
        )
        with pytest.raises(ConfigError, match="conflicts"):
            load_config("development")

    def test_require_backup_without_dir_fails(self, monkeypatch):
        _env(monkeypatch, S3VAULT_REQUIRE_BACKUP="1")
        with pytest.raises(ConfigError, match="S3VAULT_BACKUP_DIR"):
            load_config("development")

    def test_samesite_none_requires_secure_cookie(self, monkeypatch):
        _env(monkeypatch, COOKIE_SAMESITE="none", COOKIE_SECURE="false")
        with pytest.raises(ConfigError, match="COOKIE_SECURE"):
            load_config("development")


class TestStructuralValidation:
    def test_pool_min_greater_than_max_fails(self, monkeypatch):
        _env(monkeypatch, DATABASE_POOL_MIN="10", DATABASE_POOL_MAX="2")
        with pytest.raises(ConfigError, match="DATABASE_POOL_MIN"):
            load_config("development")

    def test_idle_ttl_above_absolute_ttl_fails(self, monkeypatch):
        _env(monkeypatch, SESSION_IDLE_TTL_S="7200", SESSION_TTL_S="3600")
        with pytest.raises(ConfigError, match="SESSION_IDLE_TTL_S"):
            load_config("development")

    def test_part_size_ordering_enforced(self, monkeypatch):
        _env(monkeypatch, MAX_PART_BYTES="1048576", MIN_PART_BYTES="5242880")
        with pytest.raises(ConfigError, match="MAX_PART_BYTES"):
            load_config("development")

    def test_presign_max_below_default_fails(self, monkeypatch):
        _env(monkeypatch, PRESIGN_TTL_S="600", PRESIGN_MAX_TTL_S="300")
        with pytest.raises(ConfigError, match="PRESIGN_MAX_TTL_S"):
            load_config("development")

    def test_quota_zero_is_rejected(self, monkeypatch):
        _env(monkeypatch, DEFAULT_USER_QUOTA_BYTES="0")
        with pytest.raises(ConfigError, match="DEFAULT_USER_QUOTA_BYTES"):
            load_config("development")

    def test_partial_bootstrap_admin_rejected(self, monkeypatch):
        _env(monkeypatch, BOOTSTRAP_ADMIN_USER="admin")
        with pytest.raises(ConfigError, match="must be provided together"):
            load_config("development")

    def test_non_integer_numeric_env_fails(self, monkeypatch):
        _env(monkeypatch, SESSION_TTL_S="soon")
        with pytest.raises(ConfigError, match="SESSION_TTL_S"):
            load_config("development")

    def test_unknown_env_rejected(self, monkeypatch):
        _env(monkeypatch)
        with pytest.raises(ConfigError, match="S3VAULT_ENV"):
            load_config("staging")

    def test_identical_root_credentials_rejected(self, monkeypatch):
        _env(
            monkeypatch,
            STORAGE_ROOT_ACCESS_KEY="identical-value",
            STORAGE_ROOT_SECRET_KEY="identical-value",
        )
        with pytest.raises(ConfigError, match="must differ"):
            load_config("development")


class TestIssueCollection:
    def test_request_limit_above_max_object_is_reported(self, monkeypatch):
        _env(monkeypatch, MAX_OBJECT_BYTES="1024", REQUEST_BODY_LIMIT_BYTES="2048")
        cfg = load_config("development")
        assert any("REQUEST_BODY_LIMIT_BYTES" in issue for issue in cfg.issues)

    def test_issue_names_only_never_values(self, monkeypatch):
        _env(monkeypatch, MAX_OBJECT_BYTES="1024", REQUEST_BODY_LIMIT_BYTES="2048")
        cfg = load_config("development")
        joined = " ".join(cfg.issues)
        assert BASE_ENV["STORAGE_ROOT_SECRET_KEY"] not in joined
        assert BASE_ENV["KEK_BASE64"] not in joined


def test_generate_kek_produces_distinct_32_byte_keys():
    a = base64.b64decode(generate_kek())
    b = base64.b64decode(generate_kek())
    assert len(a) == 32 and len(b) == 32
    assert a != b


class TestRedaction:
    def test_signed_query_parameters_are_masked(self):
        text = "https://s3/x?X-Amz-Signature=deadbeefcafe&X-Amz-Credential=AKIA123"
        redacted = redact_text(text)
        assert "deadbeefcafe" not in redacted
        assert "AKIA123" not in redacted
        assert "X-Amz-Signature=***" in redacted

    def test_jwt_is_masked(self):
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijkl"
        assert "eyJhbGciOiJIUzI1NiJ9" not in redact_text(f"auth {token}")

    def test_bearer_header_is_masked(self):
        assert "supersecrettoken" not in redact_text("Authorization: Bearer supersecrettoken")

    def test_sensitive_keys_masked_recursively(self):
        event = redact_processor(None, "", {"password": "hunter2", "nested": {"secret": "s3cr3t"}})
        assert event["password"] == "***"
        assert event["nested"]["secret"] == "***"

    def test_non_sensitive_values_survive(self):
        event = redact_processor(None, "", {"bucket": "documents", "count": 3})
        assert event["bucket"] == "documents"
        assert event["count"] == 3