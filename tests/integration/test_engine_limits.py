"""The control plane must describe the engine truthfully.

Every assertion here compares what S3Vault advertises against what the live
SeaweedFS gateway actually does, verified by the sibling S3-compatibility
suite. The rule under test: S3Vault never claims a capability the engine
lacks, and never hides one it has.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_policy_is_reported_unsupported(auth_client, unique_bucket):
    """Bucket policies are the one bucket feature SeaweedFS 3.97 lacks."""
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201

    write = auth_client.put(
        f"/api/v1/buckets/{unique_bucket}/policy",
        json={
            "policy": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "p",
                        "Effect": "Allow",
                        "Principal": {"AWS": ["arn:aws:iam::000000000000:root"]},
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{unique_bucket}/*"],
                    }
                ],
            }
        },
    )
    assert write.status_code == 501
    assert write.json()["error"]["code"] == "unsupported_feature"

    read = auth_client.get(f"/api/v1/buckets/{unique_bucket}/policy").json()
    assert read["supported"] is False
    assert read["policy"] is None
    assert read["reason"]


def test_supported_bucket_configuration_roundtrips(auth_client, unique_bucket):
    """CORS, lifecycle and versioning are supported and must work end to end."""
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201

    cors = auth_client.put(
        f"/api/v1/buckets/{unique_bucket}/cors", json={"origins": ["https://console.example.com"]}
    )
    assert cors.status_code == 200
    assert auth_client.get(f"/api/v1/buckets/{unique_bucket}/cors").json()["origins"] == [
        "https://console.example.com"
    ]

    # A non-http(s) origin would be an injection vector; it is refused.
    bad_origin = auth_client.put(
        f"/api/v1/buckets/{unique_bucket}/cors", json={"origins": ["javascript:alert(1)"]}
    )
    assert bad_origin.status_code == 400

    lifecycle = auth_client.put(
        f"/api/v1/buckets/{unique_bucket}/lifecycle",
        json={
            "rules": [
                {
                    "ID": "expire-tmp",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "tmp/"},
                    "Expiration": {"Days": 7},
                }
            ]
        },
    )
    assert lifecycle.status_code == 200
    assert lifecycle.json()["rule_count"] == 1

    versioning = auth_client.put(
        f"/api/v1/buckets/{unique_bucket}/versioning", json={"status": "Enabled"}
    )
    assert versioning.status_code == 200
    assert auth_client.get(f"/api/v1/buckets/{unique_bucket}/versioning").json()["status"] == "Enabled"

    bad_status = auth_client.put(
        f"/api/v1/buckets/{unique_bucket}/versioning", json={"status": "Sideways"}
    )
    assert bad_status.status_code == 422


def test_capability_report_matches_probe(auth_client):
    """The endpoint's feature list is the probed set, not a hard-coded list."""
    reported = auth_client.get("/api/v1/settings/engine-capabilities").json()
    services = auth_client.app.state.services
    probed = services.capabilities

    assert set(reported["supported"]) == {f.value for f in probed.supported}
    assert reported["engine"] == probed.engine_name
    assert "object.put" in reported["supported"]
    # The one measured absence.
    assert "bucket.policies" not in reported["supported"]
    assert reported["notes"].get("bucket.policies")


def test_bucket_detail_reports_engine_features(auth_client, unique_bucket):
    assert auth_client.post("/api/v1/buckets", json={"name": unique_bucket}).status_code == 201
    detail = auth_client.get(f"/api/v1/buckets/{unique_bucket}").json()
    features = detail["engine_features"]
    assert features["versioning"] is True
    assert features["cors"] is True
    assert features["lifecycle"] is True
    assert features["policies"] is False
    assert features["multipart"] is True