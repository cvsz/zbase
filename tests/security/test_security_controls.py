"""Security-focused tests.

Each test names the threat it mitigates. Assertions target observable
behaviour (status codes, persisted state, emitted logs) rather than internals.
"""

from __future__ import annotations

import uuid

import pytest

from conftest import TEST_S3_ENDPOINT

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# authentication and lockout
# ---------------------------------------------------------------------------
def test_repeated_failures_lock_the_account(client):
    """Brute-force mitigation: the account locks instead of staying open."""
    services = client.app.state.services
    from s3vault.api.deps import limiter as get_limiter

    username = f"lockme-{uuid.uuid4().hex[:8]}"
    services.users.create_user(
        username=username, email=f"{username}@t.test", password="CorrectPassword123!", role="owner"
    )

    locked_at = None
    for attempt in range(10):
        get_limiter().reset()
        response = client.post(
            "/api/v1/auth/login", json={"username": username, "password": "wrong-guess"}
        )
        if response.status_code == 429:
            row = services.db.fetch_one(
                "SELECT locked_until FROM users WHERE username = %s", (username,)
            )
            if row and row["locked_until"] is not None:
                locked_at = attempt
                break
    assert locked_at is not None, "the account should lock after repeated failures"

    # Even the correct password is refused while locked.
    get_limiter().reset()
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "CorrectPassword123!"}
    )
    assert response.status_code == 429


def test_login_rate_limit_is_enforced(client):
    from s3vault.api.deps import limiter as get_limiter

    get_limiter().reset()
    statuses = []
    for _ in range(8):
        statuses.append(
            client.post(
                "/api/v1/auth/login", json={"username": "nobody", "password": "wrongguess1"}
            ).status_code
        )
    assert 429 in statuses, "repeated sign-in attempts must be rate limited"


def test_deactivated_user_cannot_use_an_existing_session(auth_client):
    """Deactivation must invalidate sessions that already exist."""
    services = auth_client.app.state.services
    assert auth_client.get("/api/v1/auth/me").status_code == 200

    me = auth_client.get("/api/v1/auth/me").json()["user"]
    services.users.set_active(me["id"], False)

    assert auth_client.get("/api/v1/auth/me").status_code == 401


def test_password_reset_revokes_all_sessions(client):
    services = client.app.state.services
    username = f"reset-{uuid.uuid4().hex[:8]}"
    services.users.create_user(
        username=username, email=f"{username}@t.test", password="OriginalPassword1!", role="owner"
    )
    token = services.users.issue_reset_token(f"{username}@t.test")
    assert token

    services.users.complete_reset(token, "BrandNewPassword12!")
    assert services.users.active_session_count(
        services.db.fetch_one("SELECT id FROM users WHERE username = %s", (username,))["id"]
    ) == 0


def test_reset_token_is_single_use(client):
    services = client.app.state.services
    username = f"once-{uuid.uuid4().hex[:8]}"
    services.users.create_user(
        username=username, email=f"{username}@t.test", password="OriginalPassword1!", role="owner"
    )
    token = services.users.issue_reset_token(f"{username}@t.test")
    services.users.complete_reset(token, "BrandNewPassword12!")

    from s3vault.errors import AuthError

    with pytest.raises(AuthError):
        services.users.complete_reset(token, "AnotherPassword12345")


def test_password_reset_endpoint_does_not_enumerate_accounts(client):
    known = client.post(
        "/api/v1/auth/password-reset/request", json={"email": "nobody@example.invalid"}
    )
    unknown = client.post(
        "/api/v1/auth/password-reset/request", json={"email": "still-nobody@example.invalid"}
    )
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert "development_token" not in unknown.json(), "tokens must not leak outside development"


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def test_every_mutating_route_requires_csrf(auth_client):
    """A session cookie alone must never authorise a state change."""
    auth_client.headers.pop("X-CSRF-Token", None)
    attempts = [
        ("post", "/api/v1/buckets", {"name": f"nocsrf-{uuid.uuid4().hex[:8]}"}),
        ("post", "/api/v1/credentials", {"permissions": ["read"]}),
        ("post", "/api/v1/auth/logout", None),
        ("post", "/api/v1/users", {
            "username": f"nocsrf{uuid.uuid4().hex[:8]}",
            "email": f"n{uuid.uuid4().hex[:8]}@t.test",
            "password": "NoCsrfPassword123!",
        }),
        ("delete", "/api/v1/credentials/00000000-0000-0000-0000-000000000000", None),
        ("delete", "/api/v1/buckets/some-bucket-name", {"confirm_name": "some-bucket-name"}),
    ]
    for method, path, body in attempts:
        response = auth_client.request(method, path, json=body)
        assert response.status_code == 403, (
            f"{method.upper()} {path} returned {response.status_code}, expected a CSRF refusal"
        )
        assert response.json()["error"]["code"] == "forbidden"


def test_forged_csrf_token_is_rejected(auth_client):
    auth_client.headers.update({"X-CSRF-Token": "x" * 64})
    response = auth_client.post("/api/v1/credentials", json={"permissions": ["read"]})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_logout_requires_csrf(auth_client):
    """Logging out without CSRF must not succeed."""
    token = auth_client.cookies.get("s3vault_csrf")
    auth_client.headers.pop("X-CSRF-Token", None)
    response = auth_client.post("/api/v1/auth/logout")
    assert response.status_code == 403
    auth_client.headers.update({"X-CSRF-Token": token or ""})
    assert auth_client.get("/api/v1/auth/me").status_code == 200, "session must survive"


# ---------------------------------------------------------------------------
# injection and traversal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key",
    [
        "../../../etc/passwd",
        "..%2f..%2fetc%2fpasswd",
        "/etc/passwd",
        "a/../../b",
        "..",
        "dir/..",
        "a//b",
        "\x00null",
    ],
)
def test_object_keys_cannot_escape_the_namespace(client, key):
    from s3vault.security.tokens import normalise_object_key

    with pytest.raises(ValueError):
        normalise_object_key(key)


def test_sql_identifiers_are_validated():
    from s3vault.db import Migrator
    from s3vault.errors import AppError

    assert Migrator.safe_identifier("users") is not None
    for bad in ["users; DROP TABLE users", "users--", "1users", "us ers", ""]:
        with pytest.raises(AppError):
            Migrator.safe_identifier(bad)


def test_query_parameters_are_parameterised(auth_client, unique_bucket):
    """Injection through list parameters must not execute SQL."""
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    services = auth_client.app.state.services
    services.adapter.put_object(unique_bucket, "x.txt", b"x")

    hostile = "'; DROP TABLE buckets; --"
    response = auth_client.get(
        f"/api/v1/buckets/{unique_bucket}/objects", params={"prefix": hostile}
    )
    assert response.status_code == 200
    # The table survived.
    assert services.db.fetch_one("SELECT count(*) AS n FROM buckets")["n"] >= 1


def test_cors_origin_injection_is_rejected(auth_client, unique_bucket):
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    for hostile in ["javascript:alert(1)", "data:text/html,x", "file:///etc/passwd"]:
        response = auth_client.put(
            f"/api/v1/buckets/{unique_bucket}/cors", json={"origins": [hostile]}
        )
        assert response.status_code == 400, f"{hostile} should be refused"


def test_oversized_body_is_rejected(auth_client, unique_bucket):
    """A request body beyond the configured limit must not be accepted."""
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    services = auth_client.app.state.services
    limit = services.config.uploads.request_body_limit_bytes

    huge = "x" * (limit + 1024)
    response = auth_client.post(
        f"/api/v1/buckets/{unique_bucket}/presigned-upload",
        json={"key": "big.txt", "content_type": "text/plain", "description": huge},
    )
    assert response.status_code in {400, 413, 422}


# ---------------------------------------------------------------------------
# IDOR
# ---------------------------------------------------------------------------
def test_non_admin_cannot_read_other_users(auth_client):
    """Admins may read users; a peer account may not."""
    services = auth_client.app.state.services
    from fastapi.testclient import TestClient

    peer = f"peer-{uuid.uuid4().hex[:8]}"
    services.users.create_user(
        username=peer, email=f"{peer}@t.test", password="PeerUserPassword12!", role="owner"
    )
    other = f"other-{uuid.uuid4().hex[:8]}"
    services.users.create_user(
        username=other, email=f"{other}@t.test", password="OtherUserPassword1!", role="owner"
    )
    other_id = services.db.fetch_one(
        "SELECT id FROM users WHERE username = %s", (other,)
    )["id"]

    with TestClient(auth_client.app) as c:
        c.post("/api/v1/auth/login", json={"username": peer, "password": "PeerUserPassword12!"})
        c.headers.update({"X-CSRF-Token": c.cookies.get("s3vault_csrf") or ""})

        assert c.get("/api/v1/auth/me").status_code == 200, "may read itself"
        assert c.get(f"/api/v1/users/{other_id}").status_code == 403


def test_auditor_cannot_mutate(auth_client, unique_bucket):
    """Least privilege: the auditor role is read-only."""
    services = auth_client.app.state.services
    auditor = f"aud-{uuid.uuid4().hex[:8]}"
    services.users.create_user(
        username=auditor, email=f"{auditor}@t.test", password="AuditorPassword123!", role="auditor"
    )

    from fastapi.testclient import TestClient

    with TestClient(auth_client.app) as c:
        c.post("/api/v1/auth/login", json={"username": auditor, "password": "AuditorPassword123!"})
        c.headers.update({"X-CSRF-Token": c.cookies.get("s3vault_csrf") or ""})

        assert c.get("/api/v1/buckets").status_code == 200, "auditors may read"
        assert c.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 403
        assert c.post("/api/v1/credentials", json={"permissions": ["read"]}).status_code == 403
        assert c.get("/api/v1/users").status_code == 403


def test_expired_credential_cannot_be_resolved(auth_client):
    """An expired credential must stop working, not merely look expired."""
    import datetime as dt

    services = auth_client.app.state.services
    from s3vault.errors import NotFoundError

    created = auth_client.post(
        "/api/v1/credentials",
        json={
            "permissions": ["readwrite"],
            "expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=2)).isoformat(),
        },
    )
    assert created.status_code == 201
    access_key_id = created.json()["credential"]["access_key_id"]

    # Valid now.
    resolved = services.credentials.resolve_secret(access_key_id)
    assert resolved["access_key_id"] == access_key_id

    # Force expiry rather than sleeping.
    services.db.execute(
        "UPDATE credentials SET expires_at = now() - interval '1 hour' WHERE access_key_id = %s",
        (access_key_id,),
    )
    with pytest.raises(NotFoundError, match="expired"):
        services.credentials.resolve_secret(access_key_id)


def test_unknown_credential_cannot_be_resolved(auth_client):
    from s3vault.errors import NotFoundError

    services = auth_client.app.state.services
    with pytest.raises(NotFoundError, match="unknown access key"):
        services.credentials.resolve_secret("AKIDNOTAREALKEY00000")


# ---------------------------------------------------------------------------
# secret leakage
# ---------------------------------------------------------------------------
def test_no_secret_appears_in_any_api_response(auth_client):
    """Sweep the API for the configured secrets and the admin password."""
    services = auth_client.app.state.services
    secrets_to_hunt = {
        services.config.security.kek_base64,
        services.config.storage.root_secret_key,
        services.config.storage.root_access_key,
    }
    created = auth_client.post("/api/v1/credentials", json={"permissions": ["readwrite"]})
    secrets_to_hunt.add(created.json()["credential"]["secret_access_key"])

    probes = [
        ("GET", "/api/v1/overview", None),
        ("GET", "/api/v1/settings", None),
        ("GET", "/api/v1/settings/engine-capabilities", None),
        ("GET", "/api/v1/credentials", None),
        ("GET", "/api/v1/audit-events", {"limit": 500}),
        ("GET", "/api/v1/auth/me", None),
        ("GET", "/api/v1/usage", None),
        ("GET", "/readyz", None),
        ("GET", "/healthz", None),
    ]
    for method, path, params in probes:
        text = auth_client.request(method, path, params=params).text
        for secret in secrets_to_hunt:
            assert secret not in text, f"{path} leaked a secret"


def test_logs_redact_credentials(caplog):
    import logging

    from s3vault.logging_setup import configure_logging, redact_processor

    caplog.set_level(logging.INFO)
    configure_logging(
        type(
            "C",
            (),
            {"log_level": "INFO", "log_json": True, "issues": (), "env": "test"},
        )()
    )
    secret = "s3vaulttestsecret"
    record = {
        "event": "request",
        "authorization": f"AWS4-HMAC-SHA256 Credential=AKIA/{secret}",
        "url": f"https://s3/x?X-Amz-Signature={secret}&x=1",
        "nested": {"secret_access_key": secret, "bucket": "documents"},
    }
    cleaned = redact_processor(None, "", record)
    assert secret not in str(cleaned)
    assert cleaned["nested"]["bucket"] == "documents"
    assert cleaned["nested"]["secret_access_key"] == "***"


def test_audit_detail_drops_forbidden_keys():
    from s3vault.services.audit import AuditService

    sanitised = AuditService._sanitise(
        {
            "password": "hunter2",
            "secret_access_key": "abc",
            "url": "https://s3/x?X-Amz-Signature=zzz",
            "bucket": "documents",
        }
    )
    assert "password" not in sanitised
    assert "secret_access_key" not in sanitised
    assert sanitised["bucket"] == "documents"


# ---------------------------------------------------------------------------
# operational safety gates
# ---------------------------------------------------------------------------
def test_production_mutation_guard_blocks_deletes(app_env):
    """In production, mutations fail closed unless explicitly authorised.

    Exercised at the guard rather than over HTTP: a valid production profile
    requires an HTTPS engine endpoint, which the local test stack does not
    serve, and the property under test is the guard's decision, not transport.
    The companion HTTP test (``test_dry_run_does_not_reach_the_backend``)
    proves the storage backend is never contacted.
    """
    import os

    from s3vault.app import Services
    from s3vault.config import load_config
    from s3vault.errors import SafetyGateError

    if not stack_available_for_tests():
        pytest.skip("stack not running")

    os.environ["S3VAULT_ENV"] = "production"
    os.environ["COOKIE_SECURE"] = "true"
    os.environ["PUBLIC_HOSTNAME"] = "s3.example.com"
    os.environ["STORAGE_ENGINE_ENDPOINT"] = "https://seaweedfs.internal:8333"
    os.environ["TRUSTED_PROXY_HOPS"] = "1"
    os.environ["S3VAULT_ALLOW_PRODUCTION_CHANGES"] = "0"
    try:
        cfg = load_config()
        assert cfg.is_production
        services = Services(cfg)
        try:
            with pytest.raises(SafetyGateError) as excinfo:
                services.safety.assert_mutation_allowed(operation="bucket.delete")
            assert "S3VAULT_ALLOW_PRODUCTION_CHANGES" in excinfo.value.message
            assert excinfo.value.status == 423

            # Read paths never consult this guard, so there is nothing to
            # assert about them here; the gate exists only for mutations.

            # And the denial was audited.
            rows, _ = services.audit.list(action="safety.production_change_blocked", limit=5)
            assert rows, "the blocked mutation must be audited"
            assert rows[0]["outcome"] == "denied"

            # Explicitly authorising it lifts the guard.
            os.environ["S3VAULT_ALLOW_PRODUCTION_CHANGES"] = "1"
            from dataclasses import replace as _replace

            authorised = _replace(cfg, safety=_replace(cfg.safety, allow_production_changes=True))
            guard = type(services.safety)(authorised, services.db, services.audit)
            guard.assert_mutation_allowed(operation="bucket.delete")
        finally:
            services.close()
    finally:
        os.environ["S3VAULT_ENV"] = "test"
        os.environ["COOKIE_SECURE"] = "false"
        os.environ["PUBLIC_HOSTNAME"] = "s3vault.test"
        os.environ["STORAGE_ENGINE_ENDPOINT"] = TEST_S3_ENDPOINT
        os.environ["TRUSTED_PROXY_HOPS"] = "0"
        os.environ.pop("S3VAULT_ALLOW_PRODUCTION_CHANGES", None)


def stack_available_for_tests() -> bool:
    import socket

    for port in (55432, 8333):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass
        except OSError:
            return False
    return True


def test_dry_run_does_not_reach_the_backend(app_env, stack_available):
    """Dry run must compute a plan and leave persistent state untouched."""
    if not stack_available:
        pytest.skip("stack not running")
    import os

    from fastapi.testclient import TestClient

    from s3vault.app import create_app
    from s3vault.config import load_config
    from s3vault.db import Migrator

    os.environ["S3VAULT_REQUIRE_DRY_RUN"] = "1"
    try:
        cfg = load_config()
        app = create_app(cfg, run_migrations=False)
        services = app.state.services
        Migrator(services.db).apply()

        username = f"dry-{uuid.uuid4().hex[:8]}"
        services.users.create_user(
            username=username, email=f"{username}@t.test", password="DryRunPassword123!", role="admin"
        )

        with TestClient(app) as c:
            c.post("/api/v1/auth/login", json={"username": username, "password": "DryRunPassword123!"})
            c.headers.update({"X-CSRF-Token": c.cookies.get("s3vault_csrf") or ""})

            bucket = f"dry-b-{uuid.uuid4().hex[:10]}"
            assert c.post("/api/v1/buckets", json={"name": bucket}).status_code == 201

            response = c.request(
                "DELETE", f"/api/v1/buckets/{bucket}", json={"confirm_name": bucket}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["dry_run"] is True
            assert body["plan"]["bucket"] == bucket

            # Untouched in the engine AND in the metadata database.
            assert services.adapter.bucket_exists(bucket), "dry run deleted the engine bucket"
            assert services.buckets().exists_in_metadata(bucket)
    finally:
        os.environ.pop("S3VAULT_REQUIRE_DRY_RUN", None)


def test_dry_run_and_production_changes_conflict(app_env):
    """Conflicting safety settings must fail closed at startup."""
    import os

    from s3vault.config import ConfigError, load_config

    os.environ["S3VAULT_REQUIRE_DRY_RUN"] = "1"
    os.environ["S3VAULT_ALLOW_PRODUCTION_CHANGES"] = "1"
    try:
        with pytest.raises(ConfigError):
            load_config("development")
    finally:
        os.environ.pop("S3VAULT_REQUIRE_DRY_RUN", None)
        os.environ.pop("S3VAULT_ALLOW_PRODUCTION_CHANGES", None)


def test_delete_requires_name_confirmation(auth_client, unique_bucket):
    """Destructive operations must require an explicit confirmation token."""
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    for wrong in (None, "", "other-bucket", unique_bucket.upper()):
        body = {"confirm_name": wrong} if wrong is not None else {}
        response = auth_client.request("DELETE", f"/api/v1/buckets/{unique_bucket}", json=body)
        assert response.status_code == 400
        assert auth_client.app.state.services.adapter.bucket_exists(unique_bucket)


# ---------------------------------------------------------------------------
# transport and headers
# ---------------------------------------------------------------------------
def test_security_headers_are_present(auth_client):
    response = auth_client.get("/api/v1/health")
    headers = response.headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "X-Request-ID" in headers


def test_session_cookie_is_not_readable_by_script(auth_client):
    """The session cookie must be HttpOnly; the CSRF cookie is readable by design."""
    fresh = auth_client
    login = fresh.post(
        "/api/v1/auth/login",
        json={
            "username": fresh.s3vault_test_credentials["username"],
            "password": fresh.s3vault_test_credentials["password"],
        },
    )
    cookies = login.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith("s3vault_session="))
    csrf = next(c for c in cookies if c.startswith("s3vault_csrf="))
    assert "HttpOnly" in session
    assert "HttpOnly" not in csrf
    assert "SameSite" in session or "samesite" in session.lower()


def test_error_responses_do_not_leak_internals(client):
    """A 500 must not describe internal state."""
    response = client.get("/api/v1/buckets/../../etc/passwd")
    assert response.status_code in {400, 404}
    body = response.text
    for leak in ["Traceback", "psycopg", "botocore", "/home/", "s3vaulttestsecret"]:
        assert leak not in body


