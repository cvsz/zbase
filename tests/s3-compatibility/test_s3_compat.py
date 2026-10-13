"""S3 protocol compatibility suite.

Uses the official AWS SDK for Python (``boto3``, SigV4) against the live
SeaweedFS gateway, and the S3Vault admin API through the application. Both
successful and denied paths are exercised.

This suite establishes the compatibility matrix in
docs/S3-COMPATIBILITY.md. Every assertion here corresponds to a row of that
document.
"""

from __future__ import annotations

import os
import uuid

import boto3
import botocore.exceptions as be
import pytest
from botocore.config import Config as BotoConfig

pytestmark = pytest.mark.s3compat

ENDPOINT = os.environ.get("S3VAULT_TEST_S3_ENDPOINT", "http://127.0.0.1:8333")
ACCESS_KEY = os.environ.get("S3VAULT_TEST_S3_ACCESS_KEY", "s3vaulttest")
SECRET_KEY = os.environ.get("S3VAULT_TEST_S3_SECRET_KEY", "s3vaulttestsecret")
MB5 = 1024 * 1024


def _client():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=60,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


@pytest.fixture
def s3():
    return _client()


@pytest.fixture
def bucket(s3):
    """A disposable bucket, removed afterwards."""
    name = f"s3vault-compat-{uuid.uuid4().hex[:12]}"
    s3.create_bucket(Bucket=name)
    try:
        yield name
    finally:
        try:
            keys = [
                {"Key": obj["Key"]}
                for obj in s3.list_objects_v2(Bucket=name).get("Contents", [])
            ]
            if keys:
                s3.delete_objects(Bucket=name, Delete={"Objects": keys})
            # Clear multipart residue so deletion succeeds.
            for upload in s3.list_multipart_uploads(Bucket=name).get("Uploads", []):
                s3.abort_multipart_upload(
                    Bucket=name, Key=upload["Key"], UploadId=upload["UploadId"]
                )
            s3.delete_bucket(Bucket=name)
        except Exception:  # noqa: BLE001,S110 - cleanup is best effort
            pass


# ---------------------------------------------------------------------------
# bucket operations
# ---------------------------------------------------------------------------
def test_create_and_list_bucket(s3, bucket):
    assert any(b["Name"] == bucket for b in s3.list_buckets()["Buckets"])


def test_head_bucket(s3, bucket):
    s3.head_bucket(Bucket=bucket)  # must not raise


def test_missing_bucket_head_is_404(s3):
    with pytest.raises(botocore_error()):
        s3.head_bucket(Bucket="s3vault-does-not-exist-0123456789")


def botocore_error():
    return be.ClientError


def test_engine_deletes_non_empty_bucket_without_refusal(s3, bucket):
    """Documents a real engine gap that S3Vault must compensate for.

    SeaweedFS 3.97 removes a bucket even when it still holds objects, so
    relying on the engine for data-loss protection would be unsafe. S3Vault
    therefore performs the emptiness check itself; see
    docs/S3-COMPATIBILITY.md#bucket-deletion.
    """
    s3.put_object(Bucket=bucket, Key="blocker", Body=b"x")
    s3.delete_bucket(Bucket=bucket)  # engine allows this

    # The objects are gone with it: the engine did not protect them.
    with pytest.raises(botocore_error()):
        s3.head_bucket(Bucket=bucket)


def test_delete_missing_bucket_is_denied(s3):
    with pytest.raises(botocore_error()):
        s3.delete_bucket(Bucket="s3vault-does-not-exist-0123456789")


# ---------------------------------------------------------------------------
# object CRUD
# ---------------------------------------------------------------------------
def test_put_head_get_delete(s3, bucket):
    payload = b"s3vault compatibility payload"
    put = s3.put_object(
        Bucket=bucket, Key="dir/file.txt", Body=payload, ContentType="text/plain"
    )
    assert put["ETag"]

    head = s3.head_object(Bucket=bucket, Key="dir/file.txt")
    assert head["ContentLength"] == len(payload)
    assert head["ContentType"] == "text/plain"

    got = s3.get_object(Bucket=bucket, Key="dir/file.txt")
    assert got["Body"].read() == payload

    s3.delete_object(Bucket=bucket, Key="dir/file.txt")
    with pytest.raises(botocore_error()):
        s3.head_object(Bucket=bucket, Key="dir/file.txt")


def test_get_missing_key_is_denied(s3, bucket):
    with pytest.raises(botocore_error()):
        s3.get_object(Bucket=bucket, Key="never-written")


def test_range_read(s3, bucket):
    s3.put_object(Bucket=bucket, Key="range.bin", Body=bytes(range(256)) * 16)
    resp = s3.get_object(Bucket=bucket, Key="range.bin", Range="bytes=10-19")
    assert resp["Body"].read() == bytes(range(10, 20))
    assert resp["ContentRange"] == "bytes 10-19/4096"


def test_copy_object(s3, bucket):
    s3.put_object(Bucket=bucket, Key="source.txt", Body=b"copy me")
    s3.copy_object(Bucket=bucket, Key="dest.txt", CopySource={"Bucket": bucket, "Key": "source.txt"})
    assert s3.get_object(Bucket=bucket, Key="dest.txt")["Body"].read() == b"copy me"


def test_metadata_roundtrip(s3, bucket):
    s3.put_object(
        Bucket=bucket,
        Key="meta.txt",
        Body=b"m",
        Metadata={"owner": "qa-team", "ticket": "ENG-1234"},
    )
    head = s3.head_object(Bucket=bucket, Key="meta.txt")
    assert head["Metadata"]["owner"] == "qa-team"
    assert head["Metadata"]["ticket"] == "ENG-1234"


def test_list_with_prefix_and_delimiter(s3, bucket):
    for key in ("a/1.txt", "a/2.txt", "b/1.txt", "root.txt"):
        s3.put_object(Bucket=bucket, Key=key, Body=b"x")

    listed = s3.list_objects_v2(Bucket=bucket, Delimiter="/")
    assert [o["Key"] for o in listed["Contents"]] == ["root.txt"]
    assert [p["Prefix"] for p in listed["CommonPrefixes"]] == ["a/", "b/"]

    nested = s3.list_objects_v2(Bucket=bucket, Prefix="a/", Delimiter="/")
    assert sorted(o["Key"] for o in nested["Contents"]) == ["a/1.txt", "a/2.txt"]


def test_list_pagination_with_marker(s3, bucket):
    for i in range(6):
        s3.put_object(Bucket=bucket, Key=f"p/{i:02d}", Body=b"y")
    first = s3.list_objects_v2(Bucket=bucket, Prefix="p/", MaxKeys=2)
    assert len(first["Contents"]) == 2
    assert first["IsTruncated"] is True
    second = s3.list_objects_v2(
        Bucket=bucket, Prefix="p/", MaxKeys=2, ContinuationToken=first["NextContinuationToken"]
    )
    assert {o["Key"] for o in second["Contents"]}.isdisjoint({o["Key"] for o in first["Contents"]})


def test_bulk_delete(s3, bucket):
    for i in range(4):
        s3.put_object(Bucket=bucket, Key=f"bulk/{i}", Body=b"z")
    resp = s3.delete_objects(
        Bucket=bucket, Delete={"Objects": [{"Key": f"bulk/{i}"} for i in range(4)]}
    )
    assert len(resp.get("Deleted", [])) == 4
    assert s3.list_objects_v2(Bucket=bucket).get("KeyCount", 0) == 0


# ---------------------------------------------------------------------------
# multipart
# ---------------------------------------------------------------------------
def test_multipart_roundtrip(s3, bucket):
    """Two 5 MiB parts: SeaweedFS requires >= 5 MiB per part for multi-part."""
    upload = s3.create_multipart_upload(Bucket=bucket, Key="large.bin")["UploadId"]
    parts = []
    for number in (1, 2):
        result = s3.upload_part(
            Bucket=bucket,
            Key="large.bin",
            UploadId=upload,
            PartNumber=number,
            Body=bytes([number]) * (5 * MB5),
        )
        parts.append({"PartNumber": number, "ETag": result["ETag"]})

    listed = s3.list_parts(Bucket=bucket, Key="large.bin", UploadId=upload)["Parts"]
    assert [p["PartNumber"] for p in listed] == [1, 2]

    s3.complete_multipart_upload(
        Bucket=bucket, Key="large.bin", UploadId=upload, MultipartUpload={"Parts": parts}
    )
    head = s3.head_object(Bucket=bucket, Key="large.bin")
    assert head["ContentLength"] == 10 * MB5


def test_multipart_abort(s3, bucket):
    upload = s3.create_multipart_upload(Bucket=bucket, Key="aborted.bin")["UploadId"]
    assert any(
        u["UploadId"] == upload
        for u in s3.list_multipart_uploads(Bucket=bucket).get("Uploads", [])
    )
    s3.abort_multipart_upload(Bucket=bucket, Key="aborted.bin", UploadId=upload)
    assert not any(
        u["UploadId"] == upload
        for u in s3.list_multipart_uploads(Bucket=bucket).get("Uploads", [])
    )


def test_small_multipart_is_rejected_by_engine(s3, bucket):
    """Documents a real SeaweedFS constraint rather than assuming it.

    A multi-part upload whose parts are below 5 MiB is refused with
    EntityTooSmall. A single-part upload and a single-shot PUT are accepted,
    which is why the check applies from the second part onwards.
    """
    upload = s3.create_multipart_upload(Bucket=bucket, Key="tiny.bin")["UploadId"]
    parts = []
    for number in (1, 2):
        result = s3.upload_part(
            Bucket=bucket,
            Key="tiny.bin",
            UploadId=upload,
            PartNumber=number,
            Body=b"z" * 1024,
        )
        parts.append({"PartNumber": number, "ETag": result["ETag"]})

    with pytest.raises(botocore_error()) as excinfo:
        s3.complete_multipart_upload(
            Bucket=bucket,
            Key="tiny.bin",
            UploadId=upload,
            MultipartUpload={"Parts": parts},
        )
    code = excinfo.value.response["Error"]["Code"]
    assert code == "EntityTooSmall"

    # The same bytes as a single-shot PUT are fine.
    s3.put_object(Bucket=bucket, Key="tiny-direct.bin", Body=b"z" * 1024)
    assert s3.head_object(Bucket=bucket, Key="tiny-direct.bin")["ContentLength"] == 1024

    # A single-part multipart upload is also fine.
    single = s3.create_multipart_upload(Bucket=bucket, Key="single.bin")["UploadId"]
    only = s3.upload_part(
        Bucket=bucket, Key="single.bin", UploadId=single, PartNumber=1, Body=b"z" * 1024
    )
    s3.complete_multipart_upload(
        Bucket=bucket,
        Key="single.bin",
        UploadId=single,
        MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": only["ETag"]}]},
    )
    assert s3.head_object(Bucket=bucket, Key="single.bin")["ContentLength"] == 1024


# ---------------------------------------------------------------------------
# presigned URLs
# ---------------------------------------------------------------------------
def test_presigned_get_url_works(s3, bucket):
    import urllib.request

    s3.put_object(Bucket=bucket, Key="presigned.txt", Body=b"presigned body")
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": "presigned.txt"}, ExpiresIn=120
    )
    assert "X-Amz-Signature" in url
    with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 - test URL
        assert resp.read() == b"presigned body"


def test_presigned_put_url_works(s3, bucket):
    import urllib.request

    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": "presigned-upload.txt"},
        ExpiresIn=120,
    )
    request = urllib.request.Request(url, data=b"uploaded directly", method="PUT")
    with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310 - test URL
        assert resp.status == 200
    assert s3.get_object(Bucket=bucket, Key="presigned-upload.txt")["Body"].read() == b"uploaded directly"


# ---------------------------------------------------------------------------
# versioning
# ---------------------------------------------------------------------------
def test_versioning_roundtrip(s3, bucket):
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    assert s3.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled"
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Suspended"})
    assert s3.get_bucket_versioning(Bucket=bucket).get("Status") == "Suspended"


# ---------------------------------------------------------------------------
# documented-unsupported features
# ---------------------------------------------------------------------------
def test_cors_is_supported_by_this_engine(s3, bucket):
    """CORS round-trips. An earlier probe wrongly reported this as unsupported
    because ``GetBucketCors`` answers "not configured" with an error code."""
    s3.put_bucket_cors(
        Bucket=bucket,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedHeaders": ["*"],
                    "AllowedMethods": ["GET", "HEAD"],
                    "AllowedOrigins": ["https://console.example.com"],
                    "MaxAgeSeconds": 600,
                }
            ]
        },
    )
    rules = s3.get_bucket_cors(Bucket=bucket)["CORSRules"]
    assert rules[0]["AllowedOrigins"] == ["https://console.example.com"]
    s3.delete_bucket_cors(Bucket=bucket)


def test_lifecycle_is_supported_by_this_engine(s3, bucket):
    """Lifecycle configuration round-trips.

    SeaweedFS 3.97 normalises a rule: it stores the filter prefix as the rule
    ``ID`` and echoes the prefix as ``Prefix`` rather than returning the
    ``Filter`` document. The effective behaviour is correct, so the assertions
    cover the semantics that matter and record the normalisation.
    See docs/S3-COMPATIBILITY.md#lifecycle-normalisation.
    """
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-probe",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "tmp/"},
                    "Expiration": {"Days": 1},
                }
            ]
        },
    )
    rules = s3.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["Status"] == "Enabled"
    assert rule["Expiration"]["Days"] == 1
    # Normalisation, not loss: the target prefix survives.
    assert rule.get("Prefix") == "tmp/" or rule.get("ID") == "tmp/"
    s3.delete_bucket_lifecycle(Bucket=bucket)


def test_bucket_policy_is_not_supported_by_this_engine(s3, bucket):
    """The one bucket-level feature SeaweedFS 3.97 genuinely lacks."""
    with pytest.raises(botocore_error()):
        s3.put_bucket_policy(
            Bucket=bucket,
            Policy=(
                '{"Version":"2012-10-17","Statement":[{"Sid":"p","Effect":"Allow",'
                '"Principal":{"AWS":["arn:aws:iam::000000000000:root"]},'
                '"Action":["s3:GetObject"],'
                f'"Resource":["arn:aws:s3:::{bucket}/*"]}}]}}'
            ),
        )
