"""Integration tests against the live PostgreSQL + SeaweedFS stack.

These exercise the real application: real migrations, real engine, real HTTP.
Nothing here is mocked.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# health and readiness
# ---------------------------------------------------------------------------
def test_liveness_needs_no_authentication(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_both_dependencies(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": True, "storage": True}


def test_health_endpoints_expose_no_secrets(client):
    for path in ("/healthz", "/readyz"):
        text = client.get(path).text
        assert "s3vaulttestsecret" not in text
        assert "AKIA" not in text
        assert "KEK" not in text


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------
def test_login_rejects_wrong_password(client):
    creds = client.s3vault_test_credentials
    response = client.post(
        "/api/v1/auth/login",
        json={"username": creds["username"], "password": "definitely-not-the-password"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_login_for_unknown_user_is_indistinguishable(client):
    response = client.post(
        "/api/v1/auth/login", json={"username": "no-such-user", "password": "whatever12345"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "invalid username or password"


def test_login_sets_httponly_session_and_readable_csrf(client):
    creds = client.s3vault_test_credentials
    response = client.post(
        "/api/v1/auth/login",
        json={"username": creds["username"], "password": creds["password"]},
    )
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("s3vault_session="))
    csrf_cookie = next(c for c in cookies if c.startswith("s3vault_csrf="))
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "Path=/" in session_cookie


def test_protected_route_requires_session(client):
    assert client.get("/api/v1/buckets").status_code == 401


def test_logout_invalidates_session(auth_client):
    assert auth_client.get("/api/v1/auth/me").status_code == 200
    assert auth_client.post("/api/v1/auth/logout").status_code == 200
    assert auth_client.get("/api/v1/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def test_state_change_without_csrf_header_is_rejected(auth_client):
    token = auth_client.cookies.get("s3vault_csrf")
    auth_client.headers.pop("X-CSRF-Token", None)
    response = auth_client.post("/api/v1/buckets", json={"name": f"csrf-{uuid.uuid4().hex[:8]}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    auth_client.headers.update({"X-CSRF-Token": token or ""})


def test_state_change_with_wrong_csrf_token_is_rejected(auth_client):
    auth_client.headers.update({"X-CSRF-Token": "not-the-real-csrf-token"})
    response = auth_client.post("/api/v1/buckets", json={"name": f"csrf-{uuid.uuid4().hex[:8]}"})
    assert response.status_code == 403
    auth_client.headers.update({"X-CSRF-Token": auth_client.cookies.get("s3vault_csrf") or ""})


def test_safe_methods_do_not_require_csrf(auth_client):
    auth_client.headers.pop("X-CSRF-Token", None)
    assert auth_client.get("/api/v1/buckets").status_code == 200


# ---------------------------------------------------------------------------
# buckets
# ---------------------------------------------------------------------------
def test_bucket_lifecycle_against_the_engine(auth_client, unique_bucket):
    created = auth_client.post("/api/v1/buckets", json={"name": unique_bucket})
    assert created.status_code == 201, created.text

    listed = auth_client.get("/api/v1/buckets").json()["buckets"]
    assert any(b["name"] == unique_bucket for b in listed)

    detail = auth_client.get(f"/api/v1/buckets/{unique_bucket}")
    assert detail.status_code == 200
    features = detail.json()["engine_features"]
    # Measured, not assumed: the engine said what it supports.
    assert features["multipart"] is True
    assert isinstance(features["cors"], bool)

    deleted = auth_client.request(
        "DELETE",
        f"/api/v1/buckets/{unique_bucket}",
        json={"confirm_name": unique_bucket},
    )
    assert deleted.status_code == 200
    assert auth_client.get(f"/api/v1/buckets/{unique_bucket}").status_code == 404


def test_delete_requires_exact_name_confirmation(auth_client, unique_bucket):
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    response = auth_client.request(
        "DELETE", f"/api/v1/buckets/{unique_bucket}", json={"confirm_name": "wrong-name"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    # Bucket must still exist.
    assert auth_client.get(f"/api/v1/buckets/{unique_bucket}").status_code == 200


def test_non_empty_bucket_is_not_deleted_without_force(auth_client, unique_bucket):
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    # Put an object through the engine directly so the app's view of "empty" is real.
    services = auth_client.app.state.services
    services.adapter.put_object(unique_bucket, "keep.txt", b"data", content_type="text/plain")

    response = auth_client.request(
        "DELETE", f"/api/v1/buckets/{unique_bucket}", json={"confirm_name": unique_bucket}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert auth_client.get(f"/api/v1/buckets/{unique_bucket}").status_code == 200


def test_invalid_bucket_names_are_rejected(auth_client):
    """Names invalid under S3's DNS-compatible rules are refused.

    Length violations are caught by request validation (422); structural
    violations reach the domain rule and return 400. Either way the bucket is
    never created.
    """
    too_short_or_long = ["ab", "a", "a" * 64]
    structural = [
        "Documents",  # uppercase: rejected rather than silently lowercased
        "has_underscore",
        "-leading",
        "trailing-",
        "double..dot",
        "dot.-dash",
    ]
    for name in too_short_or_long:
        response = auth_client.post("/api/v1/buckets", json={"name": name})
        assert response.status_code == 422, f"{name!r} should fail validation"

    for name in structural:
        response = auth_client.post("/api/v1/buckets", json={"name": name})
        assert response.status_code == 400, f"{name!r} should be rejected"
        body = response.json()
        assert body["error"]["code"] == "validation_error"

    # None of them became real buckets.
    listed = {b["name"] for b in auth_client.get("/api/v1/buckets").json()["buckets"]}
    assert not listed.intersection(too_short_or_long + structural)


def test_uppercase_name_is_not_silently_renamed(auth_client):
    response = auth_client.post("/api/v1/buckets", json={"name": "Documents"})
    assert response.status_code == 400
    assert response.json()["error"]["details"]["suggested"] == "documents"
    listed = {b["name"] for b in auth_client.get("/api/v1/buckets").json()["buckets"]}
    assert "Documents" not in listed and "documents" not in listed


def test_duplicate_bucket_is_rejected(auth_client, unique_bucket):
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    second = auth_client.post("/api/v1/buckets", json={"name": unique_bucket})
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# objects
# ---------------------------------------------------------------------------
def test_object_listing_and_prefix_navigation(auth_client, unique_bucket):
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    services = auth_client.app.state.services
    for key in ("reports/2026/q1.csv", "reports/2026/q2.csv", "reports/2025/q1.csv", "top.txt"):
        services.adapter.put_object(unique_bucket, key, b"x" * 16)

    root = auth_client.get(f"/api/v1/buckets/{unique_bucket}/objects").json()
    assert [o["key"] for o in root["objects"]] == ["top.txt"]
    assert "reports" in root["common_prefixes"]

    nested = auth_client.get(
        f"/api/v1/buckets/{unique_bucket}/objects",
        params={"prefix": "reports/", "delimiter": "/"},
    ).json()
    assert nested["common_prefixes"] == ["reports/2025", "reports/2026"]

    leaf = auth_client.get(
        f"/api/v1/buckets/{unique_bucket}/objects",
        params={"prefix": "reports/2026/", "delimiter": ""},
    ).json()
    assert sorted(o["key"] for o in leaf["objects"]) == [
        "reports/2026/q1.csv",
        "reports/2026/q2.csv",
    ]


def test_object_pagination_is_paginated(auth_client, unique_bucket):
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    services = auth_client.app.state.services
    for i in range(5):
        services.adapter.put_object(unique_bucket, f"page/{i:02d}.txt", b"y")

    first = auth_client.get(
        f"/api/v1/buckets/{unique_bucket}/objects",
        params={"prefix": "page/", "max_keys": 2},
    ).json()
    assert len(first["objects"]) == 2
    assert first["is_truncated"] is True
    assert first["next_marker"]

    second = auth_client.get(
        f"/api/v1/buckets/{unique_bucket}/objects",
        params={"prefix": "page/", "max_keys": 2, "marker": first["next_marker"]},
    ).json()
    assert [o["key"] for o in second["objects"]] != [o["key"] for o in first["objects"]]


def test_object_detail_and_stat(auth_client, unique_bucket):
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    services = auth_client.app.state.services
    services.adapter.put_object(unique_bucket, "a/b.txt", b"hello", content_type="text/plain")

    detail = auth_client.get(
        f"/api/v1/buckets/{unique_bucket}/objects/detail", params={"key": "a/b.txt"}
    ).json()["object"]
    assert detail["size"] == 5
    assert detail["content_type"] == "text/plain"

    stat = auth_client.get(f"/api/v1/buckets/{unique_bucket}/objects/stat").json()
    assert stat["object_count"] == 1
    assert stat["storage_bytes"] == 5


def test_traversal_keys_are_rejected(auth_client, unique_bucket):
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    response = auth_client.get(
        f"/api/v1/buckets/{unique_bucket}/objects/detail", params={"key": "../../etc/passwd"}
    )
    assert response.status_code == 400


def test_bulk_delete_reports_counts(auth_client, unique_bucket):
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    services = auth_client.app.state.services
    for key in ("d/1.txt", "d/2.txt", "d/3.txt"):
        services.adapter.put_object(unique_bucket, key, b"z")

    response = auth_client.request(
        "DELETE", f"/api/v1/buckets/{unique_bucket}/objects", json={"keys": ["d/1.txt", "d/2.txt"]}
    )
    assert response.status_code == 200
    assert response.json() == {"deleted": 2, "errors": 0, "requested": 2}


def test_presigned_download_url_is_issued_and_not_logged(auth_client, unique_bucket, capsys):
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    services = auth_client.app.state.services
    services.adapter.put_object(unique_bucket, "secret.txt", b"confidential")

    response = auth_client.post(
        f"/api/v1/buckets/{unique_bucket}/presigned-download",
        json={"key": "secret.txt", "expires_in": 120},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["method"] == "GET"
    assert "X-Amz-Signature" in payload["url"]

    # The audit trail must record that a URL was issued, never the URL itself.
    events = auth_client.get("/api/v1/audit-events", params={"action": "object.presign_download"}).json()
    assert events["events"], "a presign event should be recorded"
    blob = str(events["events"])
    assert "X-Amz-Signature" not in blob


def test_presign_ttl_is_capped(auth_client, unique_bucket):
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    response = auth_client.post(
        f"/api/v1/buckets/{unique_bucket}/presigned-download",
        json={"key": "x.txt", "expires_in": 99_999},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------
def test_credential_secret_is_shown_exactly_once(auth_client):
    created = auth_client.post(
        "/api/v1/credentials", json={"permissions": ["readwrite"], "description": "test key"}
    )
    assert created.status_code == 201
    credential = created.json()["credential"]
    secret = credential["secret_access_key"]
    assert len(credential["access_key_id"]) == 20
    assert len(secret) == 40

    listed = auth_client.get("/api/v1/credentials").json()["credentials"]
    matching = [c for c in listed if c["access_key_id"] == credential["access_key_id"]]
    assert matching, "the credential should be listed"
    assert "secret_access_key" not in matching[0]
    assert secret not in str(listed)


def test_credential_is_stored_encrypted(auth_client):
    created = auth_client.post("/api/v1/credentials", json={"permissions": ["read"]})
    secret = created.json()["credential"]["secret_access_key"]
    services = auth_client.app.state.services
    row = services.db.fetch_one(
        "SELECT secret_ciphertext FROM credentials WHERE access_key_id = %s",
        (created.json()["credential"]["access_key_id"],),
    )
    assert row is not None
    blob = bytes(row["secret_ciphertext"])
    assert secret.encode() not in blob


def test_credential_roundtrip_decrypts(auth_client):
    created = auth_client.post("/api/v1/credentials", json={"permissions": ["readwrite"]})
    body = created.json()["credential"]
    services = auth_client.app.state.services
    resolved = services.credentials.resolve_secret(body["access_key_id"])
    assert resolved["secret_access_key"] == body["secret_access_key"]


def test_revoked_credential_cannot_resolve(auth_client):
    created = auth_client.post("/api/v1/credentials", json={"permissions": ["read"]})
    credential_id = created.json()["credential"]["id"]
    services = auth_client.app.state.services

    from s3vault.errors import NotFoundError

    services.credentials.resolve_secret(created.json()["credential"]["access_key_id"])
    assert auth_client.delete(f"/api/v1/credentials/{credential_id}").status_code == 200
    with pytest.raises(NotFoundError):
        services.credentials.resolve_secret(created.json()["credential"]["access_key_id"])


def test_credential_rotation_issues_a_new_secret(auth_client):
    created = auth_client.post("/api/v1/credentials", json={"permissions": ["read"]})
    credential_id = created.json()["credential"]["id"]
    original_akid = created.json()["credential"]["access_key_id"]

    rotated = auth_client.post(f"/api/v1/credentials/{credential_id}/rotate")
    assert rotated.status_code == 200
    assert rotated.json()["credential"]["access_key_id"] != original_akid

    listed = auth_client.get("/api/v1/credentials").json()["credentials"]
    assert not any(c["access_key_id"] == original_akid for c in listed)


def test_invalid_permission_is_rejected(auth_client):
    response = auth_client.post("/api/v1/credentials", json={"permissions": ["destroy"]})
    assert response.status_code == 400


def test_credential_scope_is_enforced():
    from s3vault.services.credentials import CredentialService

    credential = {
        "permissions": ["read"],
        "bucket_scope": ["reports"],
        "prefix_scope": "public/",
    }
    assert CredentialService.is_allowed(credential, action="read", bucket="reports", key="public/a.txt")
    assert not CredentialService.is_allowed(
        credential, action="read", bucket="reports", key="private/a.txt"
    )
    assert not CredentialService.is_allowed(credential, action="read", bucket="other", key="public/a.txt")
    assert not CredentialService.is_allowed(credential, action="write", bucket="reports", key="public/a.txt")


# ---------------------------------------------------------------------------
# cross-user isolation
# ---------------------------------------------------------------------------
def test_user_cannot_touch_another_users_bucket(auth_client, unique_bucket):
    services = auth_client.app.state.services
    from s3vault.services.users import Actor

    victim = f"victim-{uuid.uuid4().hex[:8]}"
    services.users.create_user(
        username=victim, email=f"{victim}@s3vault.test", password="VictimPassword123!"
    )

    other = f"intruder-{uuid.uuid4().hex[:8]}"
    services.users.create_user(
        username=other, email=f"{other}@s3vault.test", password="IntruderPassword123!"
    )

    owner_row = services.db.fetch_one(
        "SELECT id FROM users WHERE username = %s", (victim,)
    )
    owner_actor = Actor(
        user_id=str(owner_row["id"]), username=victim, role="owner"
    )
    services.buckets().create(
        name=unique_bucket, owner_id=str(owner_row["id"]), actor=owner_actor
    )

    from fastapi.testclient import TestClient

    with TestClient(auth_client.app) as intruder_client:
        intruder_client.post(
            "/api/v1/auth/login",
            json={"username": other, "password": "IntruderPassword123!"},
        )
        csrf = intruder_client.cookies.get("s3vault_csrf")
        intruder_client.headers.update({"X-CSRF-Token": csrf or ""})

        assert intruder_client.get(f"/api/v1/buckets/{unique_bucket}").status_code == 403
        listed = intruder_client.get("/api/v1/buckets").json()["buckets"]
        assert not any(b["name"] == unique_bucket for b in listed)
        delete = intruder_client.request(
            "DELETE", f"/api/v1/buckets/{unique_bucket}", json={"confirm_name": unique_bucket}
        )
        assert delete.status_code == 403


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
def test_login_and_bucket_actions_are_audited(auth_client, unique_bucket):
    auth_client.post("/api/v1/buckets", json={"name": unique_bucket})

    events = auth_client.get("/api/v1/audit-events", params={"limit": 200}).json()["events"]
    actions = {e["action"] for e in events}
    assert "bucket.created" in actions
    assert "auth.login.success" in actions

    created = next(e for e in events if e["action"] == "bucket.created")
    assert created["outcome"] == "success"
    assert created["resource_id"] == unique_bucket
    assert created["request_id"]


def test_audit_never_contains_credentials(auth_client):
    secret = auth_client.post(
        "/api/v1/credentials", json={"permissions": ["readwrite"]}
    ).json()["credential"]["secret_access_key"]
    text = auth_client.get("/api/v1/audit-events", params={"limit": 500}).text
    assert secret not in text


def test_audit_export_is_ndjson(auth_client):
    response = auth_client.get("/api/v1/audit-events/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    first = next((line for line in response.text.splitlines() if line.strip()), None)
    assert first and first.startswith("{")
    import json

    json.loads(first)


# ---------------------------------------------------------------------------
# overview / settings
# ---------------------------------------------------------------------------
def test_overview_labels_every_measurement(auth_client, unique_bucket):
    auth_client.post("/api/v1/buckets", json={"name": unique_bucket})
    body = auth_client.get("/api/v1/overview").json()

    assert body["engine"]["healthy"] is True
    assert body["database"]["healthy"] is True
    assert body["totals"]["buckets"]["source"] == "database"
    storage = body["totals"]["storage_bytes"]
    assert storage["source"] in {"engine-listing", "unavailable"}
    if storage["source"] == "unavailable":
        assert storage["value"] is None
    assert body["capacity"]["available"] in {True, False}
    assert "quota" in body and "user" in body["quota"]


def test_settings_never_return_secret_values(auth_client):
    body = auth_client.get("/api/v1/settings").json()
    text = str(body)
    assert auth_client.app.state.config.security.kek_base64 not in text
    assert auth_client.app.state.config.storage.root_secret_key not in text
    for entry in body["secrets"].values():
        assert entry["value"] is None
        assert entry["configured"] is True


def test_engine_capabilities_endpoint(auth_client):
    body = auth_client.get("/api/v1/settings/engine-capabilities").json()
    assert body["engine"] == "seaweedfs"
    assert "object.put" in body["supported"]
    assert isinstance(body["notes"], dict)