"""Shared pytest fixtures for the whole suite.

Unit tests need no external services. Integration, security and
S3-compatibility tests use the ``client``/``auth_client`` fixtures, which skip
when the PostgreSQL + SeaweedFS stack is not running (see
``scripts/test-stack.sh``).

The stack is only ever contacted on loopback against the containers that
script creates. Nothing here can reach a production endpoint: ``S3VAULT_ENV``
is pinned to ``test`` and cookie security is disabled deliberately.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = REPO_ROOT / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))


TEST_PG_DSN = os.environ.get(
    "S3VAULT_TEST_DATABASE_URL", "postgresql://s3vault:s3vault@127.0.0.1:55432/s3vault"
)
TEST_S3_ENDPOINT = os.environ.get("S3VAULT_TEST_S3_ENDPOINT", "http://127.0.0.1:8333")
TEST_S3_ACCESS_KEY = os.environ.get("S3VAULT_TEST_S3_ACCESS_KEY", "s3vaulttest")
TEST_S3_SECRET_KEY = os.environ.get("S3VAULT_TEST_S3_SECRET_KEY", "s3vaulttestsecret")

#: Test-only key-encryption key: 32 bytes of 0x00. Never used outside the test env.
TEST_KEK = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def repo_migrations() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "migrations"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("could not locate the migrations directory")


def base_env(**overrides: str) -> dict[str, str]:
    """The baseline environment for tests. Never talks to production."""
    env = {
        "S3VAULT_ENV": "test",
        "DATABASE_URL": TEST_PG_DSN,
        "STORAGE_ENGINE": "seaweedfs",
        "STORAGE_ENGINE_ENDPOINT": TEST_S3_ENDPOINT,
        "STORAGE_ROOT_ACCESS_KEY": TEST_S3_ACCESS_KEY,
        "STORAGE_ROOT_SECRET_KEY": TEST_S3_SECRET_KEY,
        "STORAGE_VERIFY_TLS": "false",
        "PUBLIC_HOSTNAME": "s3vault.test",
        "COOKIE_SECURE": "false",
        "COOKIE_SAMESITE": "lax",
        "TRUSTED_PROXY_HOPS": "0",
        "S3VAULT_DISABLE_ARGON2": "1",
        "S3VAULT_MIGRATIONS_DIR": str(repo_migrations()),
        "KEK_BASE64": TEST_KEK,
        "LOG_LEVEL": "WARNING",
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


_SAFETY_KEYS = (
    "S3VAULT_REQUIRE_DRY_RUN",
    "S3VAULT_ALLOW_PRODUCTION_CHANGES",
    "S3VAULT_REQUIRE_BACKUP",
    "S3VAULT_BACKUP_DIR",
    "S3VAULT_ENV",
    "COOKIE_SECURE",
)


@pytest.fixture(scope="session", autouse=True)
def test_environment() -> Iterator[None]:
    """Baseline test environment for every test in the run."""
    for key, value in base_env().items():
        os.environ.setdefault(key, value)
    yield


@pytest.fixture(scope="session")
def config():
    from s3vault.config import load_config

    return load_config()


@pytest.fixture
def db(config):
    """Connected :class:`Database`, skipped when PostgreSQL is unavailable."""
    from s3vault.db import Database

    database = Database(config)
    if not database.ping():
        database.close()
        pytest.skip("PostgreSQL is not reachable; start scripts/test-stack.sh")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# stack-backed fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def stack_available() -> bool:
    """True when PostgreSQL and the S3 gateway both accept connections."""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(TEST_PG_DSN)
    try:
        with socket.create_connection(
            (parsed.hostname or "127.0.0.1", parsed.port or 5432), timeout=3
        ):
            pass
    except OSError:
        return False

    s3 = urlparse(TEST_S3_ENDPOINT)
    try:
        with socket.create_connection((s3.hostname or "127.0.0.1", s3.port or 8333), timeout=3):
            pass
    except OSError:
        return False
    return True


@pytest.fixture
def app_env() -> Iterator[dict[str, str]]:
    """Apply a test environment, restoring previous values afterwards."""
    keys = set(base_env()) | set(_SAFETY_KEYS)
    saved = {key: os.environ.get(key) for key in keys}
    env = base_env()
    for key, value in env.items():
        os.environ[key] = value
    try:
        yield env
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def client(app_env, stack_available) -> Iterator[Any]:
    """A TestClient bound to a migrated database, with a known admin account."""
    if not stack_available:
        pytest.skip("integration stack not running; start scripts/test-stack.sh")

    from fastapi.testclient import TestClient

    from s3vault.api.deps import limiter as get_limiter
    from s3vault.app import create_app
    from s3vault.config import load_config
    from s3vault.db import Migrator

    config = load_config()
    application = create_app(config, run_migrations=False)
    services = application.state.services

    # The rate limiter is a deliberate process-wide singleton (it guards one
    # instance without adding Redis). Each test builds a new app, so reset it
    # to stop one test's logins from exhausting the next test's window.
    get_limiter().reset()

    Migrator(services.db).apply()

    username = f"admin-{uuid.uuid4().hex[:8]}"
    password = "IntegrationAdminPass1!"
    services.users.create_user(
        username=username, email=f"{username}@s3vault.test", password=password, role="admin"
    )

    with TestClient(application) as test_client:
        test_client.s3vault_test_credentials = {  # type: ignore[attr-defined]
            "username": username,
            "password": password,
        }
        yield test_client


@pytest.fixture
def auth_client(client) -> Any:
    """Client with an authenticated admin session and the CSRF header set."""
    creds = client.s3vault_test_credentials
    response = client.post(
        "/api/v1/auth/login",
        json={"username": creds["username"], "password": creds["password"]},
    )
    assert response.status_code == 200, response.text
    csrf = client.cookies.get("s3vault_csrf")
    assert csrf, "a CSRF cookie must be issued with the session"
    client.headers.update({"X-CSRF-Token": csrf})
    return client


@pytest.fixture
def unique_bucket() -> str:
    """A bucket name unique to this test."""
    return f"s3vault-test-{uuid.uuid4().hex[:12]}"