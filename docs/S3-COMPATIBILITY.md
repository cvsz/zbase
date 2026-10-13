# S3 compatibility matrix

Every row below was **measured**, not assumed. The suite in
`tests/s3-compatibility/` exercises each row against a live SeaweedFS 3.97
gateway using the official AWS SDK for Python (`boto3`, SigV4), and
`tests/integration/test_engine_limits.py` asserts that the S3Vault API reports
the same thing the engine actually does.

Run the evidence yourself:

```bash
make test-integration
```

## How S3Vault talks to the storage engine

S3Vault is a **control plane and signing proxy**, not an S3 server. It does not
implement the S3 protocol and does not accept S3 wire-protocol requests from
the public internet.

```
client (aws cli / boto3)          browser
            │                          │
            │ SigV4                    │ cookie + CSRF
            ▼                          ▼
   ┌──────────────────────────────────────────┐
   │  reverse proxy (TLS, no buffering)       │
   └──────────────────────────────────────────┘
            │                          │
            ▼                          ▼
   ┌──────────────────────────────────────────┐
   │  S3Vault application backend             │
   │  sessions · RBAC · credentials · quotas  │
   │  audit · policy decision                  │
   └──────────────────────────────────────────┘
            │ SigV4 (server-side, after policy check)
            ▼
   ┌──────────────────────────────────────────┐
   │  SeaweedFS S3 gateway  (internal only)   │
   └──────────────────────────────────────────┘
```

**What this means for S3 clients.** The public HTTPS endpoint serves the
administrative API, not the S3 wire protocol. AWS CLI / boto3 clients reach the
engine through the endpoint S3Vault issues as a presigned URL, or through an
engine-side integration, depending on deployment. Presigned URLs are the
supported direct path today; see "Presigned URLs" below and the limitations
section.

This is a deliberate trade: it is what makes per-bucket, per-prefix credential
scoping enforceable in one place. The cost is that arbitrary S3 tooling is not
pointable at the public hostname. That trade is recorded in
[Known limitations](#known-limitations).

## Measured engine capabilities

Engine: `chrislusf/seaweedfs:3.97`, S3 gateway, single node, `-s3.allowEmptyFolder=true`.

| Capability | Status | Evidence |
|---|---|---|
| Create bucket | Supported | `test_create_and_list_bucket` |
| Head bucket | Supported | `test_head_bucket` |
| List buckets | Supported | `test_create_and_list_bucket` |
| Delete bucket | Supported **but unsafe when non-empty** | [bucket deletion](#bucket-deletion) |
| Put object | Supported | `test_put_head_get_delete` |
| Get object | Supported | `test_put_head_get_delete` |
| Head object | Supported | `test_put_head_get_delete` |
| Delete object | Supported | `test_put_head_get_delete` |
| Bulk delete (1000/batch) | Supported | `test_bulk_delete` |
| List with prefix + delimiter | Supported | `test_list_with_prefix_and_delimiter` |
| List pagination | Supported (`ContinuationToken`) | `test_list_pagination_with_marker` |
| Range read | Supported (`Content-Range` honoured) | `test_range_read` |
| Copy object | Supported | `test_copy_object` |
| User metadata round-trip | Supported | `test_metadata_roundtrip` |
| Multipart upload | Supported | [multipart constraints](#multipart-constraints) |
| List parts | Supported | `test_multipart_roundtrip` |
| List multipart uploads | Supported | `test_multipart_abort` |
| Abort multipart upload | Supported | `test_multipart_abort` |
| Presigned GET / PUT | Supported | `test_presigned_get_url_works`, `test_presigned_put_url_works` |
| Bucket versioning | Supported | `test_versioning_roundtrip` |
| Bucket CORS | Supported | `test_cors_is_supported_by_this_engine` |
| Bucket lifecycle | Supported, **with normalisation** | [lifecycle normalisation](#lifecycle-normalisation) |
| Bucket policy | **Not supported** | `test_bucket_policy_is_not_supported_by_this_engine` |
| Conditional read (`If-Match`) | Supported | probed at startup |
| Storage usage metrics | **Not exposed over S3** | measured by listing instead |
| S3 Select | Not supported | not implemented by the engine |
| Object tagging | Not exposed by the adapter | see limitations |

S3Vault probes these at startup against a disposable bucket and reports the
result. It never advertises a capability the engine lacks: a request for an
unsupported feature returns HTTP `501 unsupported_feature`, and
`GET /api/v1/settings/engine-capabilities` returns the measured set.

## Engine quirks that S3Vault compensates for

### Bucket deletion

**Measured:** SeaweedFS 3.97 removes a bucket even when it still holds objects.
It does not return `BucketNotEmpty`.

```
test_engine_deletes_non_empty_bucket_without_refusal
```

Reling on the engine would mean an accidental delete silently destroys data.
S3Vault therefore performs its own emptiness check before deleting and refuses
a non-empty bucket unless `force` is set:

```
409 conflict  {"bucket": "...", "objects_present": true}
```

The operator must additionally send `confirm_name` matching the bucket name
exactly.

### Multipart constraints

**Measured:** a multipart upload completed with **two or more parts** requires
every part to be at least 5 MiB, or the engine answers `EntityTooSmall`. A
single-part multipart upload and a single-shot `PutObject` have no minimum.

| Shape | Result |
|---|---|
| 1 part × 1 KiB | accepted |
| 2 parts × 1 KiB | `EntityTooSmall` |
| 5 parts × 1 KiB | `EntityTooSmall` |
| 2 parts × 5 MiB | accepted (10 MiB object) |
| 2 parts × 4 MiB | `EntityTooSmall` |

S3Vault encodes this as `MIN_MULTIPART_PART_BYTES = 5 MiB` and sets
`MIN_PART_BYTES` accordingly, so the failure is caught at validation time
rather than at completion. Evidence:
`test_small_multipart_is_rejected_by_engine`.

### Lifecycle normalisation

**Measured:** SeaweedFS stores a lifecycle rule using the filter prefix as the
rule `ID` and returns the prefix as `Prefix` rather than the `Filter`
document. The effective behaviour is correct; the identity of the rule is not
preserved. Evidence: `test_lifecycle_is_supported_by_this_engine`.

S3Vault keeps the rule document it submitted in PostgreSQL so the console can
show what was requested, and notes that the engine's copy is normalised.

### "Not configured" is not "unsupported"

An early version of S3Vault's capability probe reported CORS and lifecycle as
unsupported, because `GetBucketCors` and `GetBucketLifecycleConfiguration`
answer "not configured" with an error code rather than an empty document. The
probe now treats those codes as an empty success. A false negative here would
have hidden working features from operators.

## Presigned URLs

Presigned PUT and GET URLs are issued by the engine and returned to the
authenticated console.

| Property | Value |
|---|---|
| Default TTL | 900 s (`PRESIGN_TTL_S`) |
| Maximum TTL | 3600 s (`PRESIGN_MAX_TTL_S`) |
| Requested TTL out of range | `400 validation_error` |
| Enforced by | `test_presign_ttl_is_capped` |

Security properties, each covered by a test:

- The URL is issued only after a server-side permission check
  (`test_presigned_download_url_is_issued_and_not_logged`).
- The URL is never written to logs or audit details; the audit row records
  that a URL was issued, with bucket and TTL, and not the URL.
- `Referrer-Policy: no-referrer` is set globally, so the signature cannot leak
  to a third party through the referrer header.
- The redactor masks `X-Amz-Signature`, `X-Amz-Credential`, query-string
  signatures, JWTs and `Authorization` headers in any log line
  (`tests/unit/test_config.py::TestRedaction`).

## Addressing style

`STORAGE_ADDRESSING_STYLE` defaults to `path`, because a single-hostname
deployment usually has no wildcard DNS. Set `virtual` only when
`bucket.hostname` resolves for every bucket.

```bash
aws --endpoint-url https://s3.example.com s3 ls
```

## Quotas are admission control, not a hard ceiling

S3Vault enforces quotas in the application, using reservations taken before an
upload and settled afterwards. The engine exposes no quota API, so usage is
derived by listing objects. A crash between commit and settlement is corrected
by the worker's reconciliation. **Quotas are therefore a strong admission
control, not a storage-level guarantee.** See
[docs/ARCHITECTURE.md#quotas](ARCHITECTURE.md#quotas).

## Known limitations

Honest list of what is not implemented or not equivalent to AWS S3:

1. **No public S3 wire protocol.** The public endpoint is the administrative
   API. Direct client access is via presigned URLs. Arbitrary S3 tooling cannot
   be pointed at the public hostname.
2. **No bucket policies.** The engine does not support them, so cross-account
   and anonymous bucket access cannot be granted.
3. **No S3 Select**, no object tagging surface in the adapter, no
   `ListObjectVersions` in the admin API.
4. **No multi-region, no replication, no erasure coding** in the single-node
   configuration.
5. **Usage figures are measured by listing**, which is O(objects). For very
   large buckets the worker's measurement cost grows linearly.
6. **Rate limiting is per process**, so N replicas enforce N independent
   windows rather than one shared limit.
7. **No MFA and no OIDC SSO.** Both are called for in the specification and are
   not implemented.
8. **Password reset has no mail transport.** In `development` the token is
   returned in the response; elsewhere the endpoint returns an identical
   "accepted" shape whether or not the account exists, so no reset can be
   performed without an out-of-band token delivery mechanism.
9. **Multipart uploads are not proxied through the API.** Large uploads go
   direct to the engine by presigned URL.

Each item maps to a documented gap rather than an assumption of parity.