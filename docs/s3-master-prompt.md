# MASTER ENGINEERING PROMPT — SELF-HOSTED S3 OBJECT STORAGE PLATFORM

## 1. Role and mission

You are a principal infrastructure engineer, distributed-storage architect, senior full-stack developer, security engineer, DevOps engineer, and automated-testing specialist.

Your mission is to design, implement, test, document, and prepare a production-ready self-hosted S3-compatible object storage platform.

Build a private cloud storage service that users can deploy on their own VPS, bare-metal servers, dedicated servers, or homelab infrastructure.

The platform must provide:

- An S3-compatible object storage API.
- A professional web management console.
- Bucket and object management.
- Secure access key and secret key management.
- User authentication and authorization.
- Large-file and multipart uploads.
- Storage quotas and usage metrics.
- TLS and reverse-proxy support.
- Backup, recovery, monitoring, and audit logging.
- Automated deployment and upgrades.
- A complete automated test suite.
- Clear documentation for installation, administration, and troubleshooting.

Do not stop after creating a proposal, mockup, architecture document, or scaffold. Inspect the existing repository, implement the actual application, run the available tests, fix failures, and document any remaining limitations honestly.

## 2. Initial repository inspection

Before changing files:

1. Inspect the complete repository structure.
2. Read README files, dependency manifests, configuration, deployment files, CI workflows, and existing tests.
3. Identify the existing programming languages, frameworks, architectural boundaries, and deployment conventions.
4. Determine whether the repository already contains a storage backend, authentication system, frontend, or reusable infrastructure.
5. Identify security problems, unfinished features, and compatibility requirements.
6. Preserve existing working functionality unless there is a documented reason to replace it.
7. Produce a concise implementation plan with milestones and acceptance criteria.
8. Begin implementation immediately after the inspection.

Do not overwrite unrelated user changes, delete persistent data, or perform destructive infrastructure operations.

If the repository is empty, establish a maintainable project structure using the architecture below.

## 3. Product definition

Working product name: S3Vault.

Make the product name, branding, public hostname, ports, storage paths, and deployment settings configurable.

The product should offer the usability of a modern object storage control panel while relying on a proven S3 storage engine for the difficult storage protocol and durability work.

Do not implement an S3 protocol from scratch unless the existing project has a compelling, documented reason.

## 4. Storage engine selection

Evaluate mature open-source storage engines before choosing one.

Consider:

- SeaweedFS: S3 gateway, object storage, and deployment flexibility.
- Garage: lightweight S3-compatible distributed object storage.
- MinIO: assess the currently available edition, licensing, support, and distribution terms before selecting it.
- Other actively maintained S3-compatible implementations if they better satisfy the requirements.

Use current upstream documentation and verify license compatibility. Do not assume that an old community edition, container image, feature, or license is still available under its historical terms.

Create an architecture decision record explaining the selected engine.

Evaluate each candidate for:

- S3 API compatibility.
- Single-node and multi-node operation.
- Data durability and replication.
- Multipart uploads.
- Bucket and object listing.
- Authentication and authorization.
- Lifecycle management.
- Versioning support.
- Observability.
- Upgrade procedures.
- Resource requirements.
- Backup and disaster recovery.
- License and operational constraints.

Choose a stable, well-documented backend appropriate for the target deployment. Isolate backend-specific functionality behind a storage adapter so the application can evolve without coupling every feature to one engine.

The application must never pretend that an unsupported backend feature is available.

## 5. Reference architecture

Implement a modular architecture consisting of:

### A. Storage engine

The actual object storage backend owns object bytes, storage metadata, replication, and the S3 protocol.

Use its supported API and configuration. Do not modify its internal data files directly.

### B. Application backend

Responsible for:

- Administrative API.
- Authentication and sessions.
- Application user management.
- API credential lifecycle.
- Bucket administration.
- Storage usage aggregation.
- Quota enforcement.
- Audit events.
- Background tasks.
- Health and readiness endpoints.

Use the repository's existing framework when appropriate. Otherwise, choose a supported, typed, maintainable backend framework.

### C. Web frontend

Build a responsive, accessible management dashboard with real API integration.

Use the existing frontend stack when possible. Otherwise, choose a modern TypeScript framework and a maintainable component system.

### D. Persistent metadata database

Use PostgreSQL for production application metadata when the chosen scale justifies it.

Store users, roles, credential identifiers, bucket ownership, policy metadata, quotas, audit records, and other application-owned state.

The storage engine remains authoritative for actual objects.

Do not maintain an inconsistent second source of truth for object contents.

### E. Background worker

Implement asynchronous jobs for tasks such as usage aggregation, retention processing, scheduled verification, and maintenance.

Use a durable job queue only when necessary. Otherwise, use a simpler database-backed job mechanism.

### F. Deployment layer

Provide Docker Compose for a documented single-host installation, plus an optional hardened Linux deployment using a supported reverse proxy and system services.

Keep storage, database, and application state in explicit persistent volumes.

## 6. S3 API requirements

Expose an S3-compatible endpoint, for example:

https://s3.example.com

Support the operations that the selected storage engine provides, including:

- Create, list, inspect, and delete buckets.
- Put, get, head, and delete objects.
- List objects with prefixes and pagination.
- Copy objects.
- Object metadata and content types.
- Range requests where supported.
- Multipart upload creation, part upload, listing, completion, and abort.
- Presigned upload and download URLs.
- SigV4 request signing.
- Standard S3 error responses.
- Bucket access policies.
- Object versioning and lifecycle rules where supported.
- CORS configuration where supported.
- Conditional requests where supported.

Define an explicit S3 compatibility matrix.

Test supported operations using an official AWS SDK, AWS CLI, and an S3 compatibility test suite where feasible.

Do not claim complete AWS S3 parity unless it has been demonstrated by testing.

Ensure clients can configure the custom endpoint and use ordinary S3 credentials.

Example:

    aws --endpoint-url https://s3.example.com s3 ls

Example upload:

    aws --endpoint-url https://s3.example.com \
      s3 cp ./example.txt s3://documents/example.txt

Example download:

    aws --endpoint-url https://s3.example.com \
      s3 cp s3://documents/example.txt ./downloaded.txt

Document any path-style versus virtual-host-style addressing requirements.

## 7. Web management console

Build a production-quality responsive interface.

### Overview dashboard

Display:

- Total storage used.
- Number of buckets.
- Number of objects.
- Upload and download activity.
- Recent administrative events.
- Backend health.
- Available storage capacity when reliably measurable.
- Quota consumption.
- Active background tasks.

Clearly distinguish real measurements from estimates, cached metrics, and unavailable measurements.

### Bucket management

Allow authorized administrators to:

- Create buckets.
- Browse buckets.
- View bucket properties.
- Rename a bucket only if the backend supports a safe migration workflow; otherwise provide a documented alternative.
- Delete buckets with explicit confirmation.
- View usage and ownership.
- Configure supported versioning, lifecycle, and CORS settings.
- Inspect and manage supported access policies.

Prevent accidental deletion of non-empty buckets.

### Object browser

Provide:

- Folder-like navigation based on object prefixes.
- Object name, size, content type, and last-modified metadata.
- Search or prefix filtering.
- Pagination.
- File upload and download.
- Object preview for safe, supported file types.
- Copy and delete operations.
- Bulk actions with confirmation.
- Object metadata inspection.
- Version history when available.
- Multipart upload progress and cancellation.

Never render uploaded HTML or other untrusted content as privileged application markup.

### Access key management

Allow authorized administrators to:

- Create scoped S3 credentials.
- Assign permissions to specific buckets and prefixes.
- Set read-only or read/write access.
- Set expiration where supported.
- Revoke credentials.
- Inspect last-used metadata when available.
- Rotate credentials safely.

Show a new secret only once, at creation time.

Never store plaintext secret keys in application metadata or expose them in logs. Prefer backend-native credential management. If secrets must be stored by the application, use a documented envelope-encryption design backed by a separately managed key-encryption key.

### User management

Implement:

- Secure administrator bootstrap.
- User creation and deactivation.
- Role-based access control.
- Session revocation.
- Password reset procedures.
- Optional MFA.
- Least-privilege administrative roles.

Do not expose backend superuser credentials to ordinary users or browser code.

### Settings

Provide configuration views for:

- Public S3 endpoint.
- Console URL.
- Storage quotas.
- Upload size limits.
- Supported upload behavior.
- Session settings.
- Retention policies.
- Email settings if implemented.
- Health and monitoring.
- Backup status.
- Storage engine capabilities.

Secrets must be masked and must not be returned to the frontend after configuration.

## 8. Authentication and authorization

Implement secure authentication for the management console.

Requirements:

- Modern password hashing using Argon2id or an established framework equivalent.
- Secure session cookies with HttpOnly, Secure, and appropriate SameSite attributes.
- CSRF protection where applicable.
- Rate limiting for login and sensitive operations.
- Session expiration and revocation.
- Protection against brute-force attacks.
- Strict server-side role and permission checks.
- Secure password reset tokens.
- Optional OIDC/OAuth single sign-on if feasible.
- MFA support as a separately testable feature.

Do not confuse web-console sessions with S3 API credentials.

Do not use browser-only checks as authorization.

For cookie-based authentication, enforce CSRF protections on state-changing requests.

For API keys, store only identifiers and non-recoverable verifiers when the credential scheme permits it. Where S3 signing requires access to a secret, use a secure backend-native mechanism or documented encrypted secret storage.

Use constant-time comparisons where applicable.

## 9. Security architecture

Treat security as a release requirement, not a future enhancement.

Implement:

- TLS for public traffic.
- A supported reverse proxy.
- Secure headers.
- Request-size and upload-size limits.
- Input validation.
- Parameterized database operations.
- Strict authorization checks.
- Secret redaction.
- Audit logging.
- Rate limiting.
- Dependency vulnerability scanning.
- Container image scanning.
- Non-root containers where supported.
- Minimal container capabilities.
- Internal-only backend and database networks.
- Restricted administrative endpoints.
- Health endpoints that reveal no credentials or sensitive configuration.

Keep the storage engine's administrative interface and control ports private.

Do not expose internal storage ports directly to the public internet.

Do not use default production passwords or hard-coded credentials.

Fail startup when required secrets are missing or insecure production configuration is detected.

Validate forwarded headers only from explicitly trusted reverse proxies.

Prevent server-side request forgery, path traversal, cross-site scripting, CSRF, insecure direct object references, and privilege escalation.

Protect presigned URLs from leaking through application logs, analytics, error reports, and referrer headers.

Avoid returning signed URLs or secrets in diagnostic responses.

Provide a documented firewall policy with only the required public ports exposed.

## 10. Network and TLS deployment

Support:

- A configurable domain name.
- Automatic TLS certificate provisioning through a documented reverse-proxy or ACME workflow.
- HTTP-to-HTTPS redirection.
- Certificate renewal.
- Configurable external and internal ports.
- S3 API and console routing.
- Large upload timeouts.
- Correct proxy buffering behavior for large uploads.
- WebSocket support if the application requires it.

Support a private-network installation without a public domain for local development and trusted LAN environments.

Do not enable plaintext public access as a production default.

Document DNS, NAT, firewall, certificate, and reverse-proxy configuration.

## 11. Upload and download reliability

Support large objects efficiently.

Requirements:

- Streaming uploads and downloads.
- Multipart uploads for large files.
- Configurable part sizes within backend limits.
- Progress reporting.
- Retry handling.
- Cancellation.
- Checksums and integrity verification where supported.
- Range downloads where supported.
- Configurable timeouts.
- Backpressure to avoid exhausting memory.
- Safe temporary-file cleanup.
- Interrupted-upload recovery where supported.
- Explicit error handling for storage exhaustion.

Never load an entire large object into application memory just to proxy it between the client and storage engine.

Prefer direct-to-storage uploads using short-lived presigned URLs when appropriate, with backend-issued authorization and server-side policy enforcement.

For direct uploads, validate the target bucket, key, content constraints, expiry, and permissions before issuing the URL.

Do not trust a client-supplied filename as a filesystem path.

## 12. Storage quotas and usage

Implement per-user and per-bucket quota policies where the chosen backend and application architecture permit reliable enforcement.

Support configurable limits for:

- Total bytes.
- Object count.
- Maximum individual object size.
- Allowed buckets.
- Upload permissions.

Prevent concurrent uploads from silently bypassing quota enforcement.

Use reservations or another atomic accounting mechanism when enforcing upload limits.

Explain whether quotas are enforced by the backend, the application, or a combination.

If the backend cannot enforce a requested quota safely, do not advertise it as a hard guarantee.

Provide usage reconciliation to recover from failed operations and interrupted jobs.

## 13. Data integrity and durability

Provide documented storage durability configurations.

For single-node deployment:

- Persistent storage volumes.
- Disk-capacity monitoring.
- Storage health checks.
- Backup and restore instructions.
- Filesystem and disk failure warnings.

For multi-node deployment:

- Documented replication or erasure-coding support when provided by the selected engine.
- Node failure behavior.
- Failure-domain considerations.
- Quorum requirements where applicable.
- Recovery and rebalancing procedures.
- Capacity planning.

Do not claim that multiple containers on one physical disk provide high availability.

Do not claim that RAID, replication, or snapshots alone replace independent backups.

Document the expected behavior when a disk, server, network connection, or metadata database fails.

## 14. Backup and disaster recovery

Implement a documented backup strategy for:

- Application database.
- Storage-engine configuration.
- User and access-control metadata.
- Encryption keys or key-recovery material, handled separately and securely.
- Object data where supported by the backup method.
- Deployment configuration.

Requirements:

- Scheduled backups.
- Configurable retention.
- Backup integrity checks.
- Backup job logs.
- Restore procedures.
- Recovery-point objective (RPO) and recovery-time objective (RTO) guidance.
- Optional off-site backup destinations.
- Restore drills in an isolated environment.

Use the storage engine's supported backup mechanisms.

Do not blindly copy live internal storage directories while writes are occurring if doing so could create inconsistent backups.

Do not delete the last known-good backup to make room for a new backup.

Never test restore operations against production data without explicit authorization.

## 15. Monitoring and operations

Provide:

- Liveness endpoint.
- Readiness endpoint.
- Storage backend health.
- Database health.
- Structured application logs.
- Request IDs.
- Audit events.
- Upload/download metrics.
- Error-rate metrics.
- Storage capacity metrics.
- Background job metrics.
- Backup status.
- Disk-space alerts.
- Optional Prometheus-compatible metrics.

Ensure health endpoints do not expose secrets, object contents, internal credentials, or detailed infrastructure information to unauthenticated public callers.

Provide an optional Grafana dashboard if it is justified by the deployment complexity.

Support log rotation and retention.

## 16. Deployment deliverables

Create a working deployment bundle containing, as appropriate:

- Dockerfile files.
- Docker Compose configuration.
- Reverse-proxy configuration.
- Environment variable example file.
- Database migration scripts.
- Storage-engine configuration.
- Health checks.
- Startup and shutdown procedures.
- Upgrade scripts.
- Backup and restore scripts.
- Firewall documentation.
- Deployment verification scripts.
- CI workflows.

Use pinned, supported image versions or immutable image digests for production deployments. Provide a controlled update process rather than automatically adopting unreviewed latest images.

Persist all state that must survive container replacement.

Set resource limits where appropriate.

Add startup ordering and readiness checks without relying exclusively on arbitrary sleep commands.

Use graceful shutdown and handle interrupted requests safely.

## 17. Configuration management

Provide a comprehensive `.env.example` containing safe placeholders, never real secrets.

Include settings for:

- Application environment.
- Public hostname.
- Console URL.
- S3 endpoint.
- Storage engine endpoint.
- Database connection.
- Bootstrap administrator creation.
- Session signing key.
- Encryption key reference.
- Storage path.
- Upload limits.
- Quotas.
- TLS and proxy settings.
- Backup location and retention.
- Logging.
- Monitoring.

Validate configuration at startup.

Separate development, test, and production settings.

Do not silently fall back to weak secrets or insecure production defaults.

Do not print secret values during configuration validation.

## 18. API design

Provide versioned administrative endpoints, for example:

    GET    /api/v1/health
    GET    /api/v1/overview
    GET    /api/v1/buckets
    POST   /api/v1/buckets
    GET    /api/v1/buckets/{bucket}
    DELETE /api/v1/buckets/{bucket}
    GET    /api/v1/buckets/{bucket}/objects
    POST   /api/v1/buckets/{bucket}/uploads
    POST   /api/v1/buckets/{bucket}/presigned-download
    GET    /api/v1/credentials
    POST   /api/v1/credentials
    DELETE /api/v1/credentials/{id}
    GET    /api/v1/users
    POST   /api/v1/users
    GET    /api/v1/audit-events
    GET    /api/v1/usage

These routes are examples. Adapt them to the repository and selected framework.

Requirements:

- Request validation.
- Consistent response schemas.
- Appropriate HTTP status codes.
- Pagination.
- Stable error codes.
- Authorization on every sensitive operation.
- Rate limits on sensitive endpoints.
- OpenAPI documentation where supported.

Do not expose backend administrative APIs through the public application without an explicit authorization and network-security design.

## 19. Database design

Create migrations for the metadata owned by the application.

Consider entities such as:

- users
- roles
- sessions
- credentials
- bucket_ownership
- bucket_policy_metadata
- quota_policies
- usage_snapshots
- audit_events
- background_jobs
- application_settings

Use foreign keys, appropriate indexes, constraints, and transaction boundaries.

Do not store raw secret keys in audit events.

Do not store object payloads in PostgreSQL when the object storage engine is designed to store them.

Make schema upgrades repeatable and safe.

Provide migration rollback guidance where practical, but do not assume every production migration can be safely reversed.

## 20. Audit logging

Record security-sensitive operations, including:

- Login success and failure.
- User creation and deactivation.
- Role changes.
- Credential creation and revocation.
- Bucket creation and deletion.
- Access-policy changes.
- Quota changes.
- Backup and restore actions.
- Configuration changes.
- Administrative access.

Record actor, timestamp, operation, target, outcome, and request ID.

Redact passwords, access secrets, authorization headers, signed URLs, and other credentials.

Restrict access to audit records.

Document retention and export procedures.

## 21. Testing requirements

Create unit, integration, API compatibility, security, and deployment tests.

### Unit tests

Test:

- Authentication.
- Authorization.
- Configuration validation.
- Quota accounting.
- Credential lifecycle.
- Input validation.
- Audit event creation.
- Error handling.

### Integration tests

Using isolated containers and temporary volumes, test:

- Application-to-storage connectivity.
- Database migrations.
- Bucket lifecycle.
- Object upload and download.
- Object metadata.
- Multipart uploads.
- Presigned URLs.
- Credential revocation.
- Access policy enforcement.
- Quota boundaries.
- Backup and restore.

### S3 compatibility tests

Use AWS SDK or AWS CLI clients configured against the custom endpoint.

Test supported operations against the actual selected storage engine.

Validate both successful and denied requests.

### Security tests

Test:

- Unauthenticated administrative access.
- Privilege escalation.
- Cross-user bucket access.
- Cross-bucket object access.
- Expired credentials.
- Revoked credentials.
- Invalid signatures.
- Oversized requests.
- Malformed object keys.
- CSRF protection.
- Rate limits.
- Secret leakage in logs.
- Unsafe object previews.
- SSRF and path traversal defenses.

### Deployment tests

Test:

- Clean installation.
- Restart persistence.
- Application container recreation.
- Invalid configuration failure.
- Missing-volume detection.
- Database restart.
- Storage engine restart.
- TLS and reverse-proxy routing.
- Upgrade procedure.
- Backup integrity.
- Restore into an isolated test deployment.

Use temporary test data only.

Never point destructive integration tests at a production S3 endpoint.

## 22. CI/CD quality gates

Configure CI to run:

1. Formatting.
2. Linting.
3. Type checking.
4. Unit tests.
5. Integration tests where infrastructure is available.
6. S3 compatibility tests.
7. Database migration validation.
8. Dependency and secret scanning.
9. Container build.
10. Deployment configuration validation.

Fail CI on relevant test failures, type errors, exposed secrets, and serious security findings.

Pin CI actions and dependencies according to project policy.

Use least-privilege CI credentials.

Do not embed production secrets in repository files or build artifacts.

Do not automatically deploy to production from an unreviewed pull request.

## 23. Operational safety gates

Production deployment and destructive operations must be explicit.

Implement safety controls such as:

    S3VAULT_REQUIRE_DRY_RUN=1
    S3VAULT_REQUIRE_BACKUP=1
    S3VAULT_ALLOW_PRODUCTION_CHANGES=0

These are proposed settings; choose final names consistently.

The actual implementation must enforce them in executable code.

Required behavior:

- Dry-run mode must not modify persistent state.
- Production changes must fail closed unless explicitly authorized.
- Destructive bucket deletion requires explicit confirmation.
- Restore testing must use isolated storage.
- Upgrade operations must validate backups and compatibility requirements.
- Backup cleanup must preserve configured retention and the last known-good backup.
- Missing or invalid safety settings must not enable destructive operations.
- Safety tests must prove that prohibited operations do not reach the storage backend.

Do not treat environment-variable declarations or documentation alone as evidence that a safety gate works.

## 24. User experience and accessibility

Create a polished, professional interface with:

- Responsive layouts.
- Clear navigation.
- Accessible forms and dialogs.
- Keyboard navigation.
- Visible focus states.
- Loading and error states.
- Empty states.
- Confirmation dialogs.
- Useful success notifications.
- Search and pagination.
- Upload progress.
- Clear permission-denied messages.

Use consistent design tokens and reusable components.

Do not use fake metrics, placeholder bucket contents, or mock API responses in production mode.

If sample data is required for development, isolate it behind explicit development-only configuration.

## 25. Documentation

Deliver:

- README.md
- docs/ARCHITECTURE.md
- docs/INSTALLATION.md
- docs/CONFIGURATION.md
- docs/SECURITY.md
- docs/S3-COMPATIBILITY.md
- docs/BACKUP-RESTORE.md
- docs/UPGRADES.md
- docs/TROUBLESHOOTING.md
- docs/OPERATIONS.md
- docs/DEVELOPMENT.md
- docs/DISASTER-RECOVERY.md

Document the supported hardware and software prerequisites.

Include commands for initial deployment, health checks, creating a bucket, generating credentials, uploading an object, downloading an object, backing up, restoring, upgrading, and shutting down.

Document limitations and supported feature differences.

Never describe a test as passing unless it was actually executed and passed.

## 26. Suggested project structure

Adapt this structure to the existing repository rather than forcing an unnecessary rewrite.

    s3vault/
      apps/
        api/
        web/
        worker/
      packages/
        shared/
        storage-adapter/
      infra/
        docker/
        proxy/
        monitoring/
      migrations/
      scripts/
        deploy/
        backup/
        restore/
        verify/
      tests/
        unit/
        integration/
        s3-compatibility/
        security/
      docs/
      docker-compose.yml
      .env.example
      Makefile
      README.md

Use a monorepo only if it improves maintainability for the chosen technology stack.

## 27. Implementation milestones

### Milestone 1 — Discovery and architecture

Inspect the repository, select the storage engine, document the architecture, define supported S3 features, and establish security boundaries.

### Milestone 2 — Working storage foundation

Deploy the storage engine and database, establish persistent volumes, configure internal networking, and verify S3 access using an SDK or CLI.

### Milestone 3 — Backend and security

Implement authentication, authorization, bucket management, credential management, audit events, and safe configuration.

### Milestone 4 — Web console

Implement the dashboard, bucket browser, object browser, upload/download workflow, credentials interface, and settings.

### Milestone 5 — Reliability

Implement multipart uploads, quotas where enforceable, background jobs, metrics, backups, restore tests, and recovery documentation.

### Milestone 6 — Deployment and CI

Add the complete deployment bundle, health checks, CI workflows, upgrade procedures, and operational documentation.

### Milestone 7 — Hardening and release

Run all relevant tests, fix failures, inspect logs for secret leakage, validate deployment security, test recovery, and prepare the release report.

Do not mark a milestone complete merely because files have been created.

## 28. Definition of done

The project is complete only when:

- A clean deployment can be started from documented instructions.
- Persistent data survives application restarts and container recreation.
- An S3 client can create a bucket and upload, list, download, and delete an object.
- Multipart uploads work if included in the supported feature matrix.
- Presigned URLs work within the documented security constraints.
- The web console communicates with the real backend.
- Authentication and authorization are enforced server-side.
- Credentials can be created, scoped, and revoked.
- Secrets are not leaked through source files, frontend bundles, logs, or API responses.
- TLS and reverse-proxy configuration are documented and validated.
- Backup and restore have been tested against isolated data.
- CI quality gates execute successfully, or remaining failures are clearly documented.
- Security-sensitive operations are covered by tests.
- The S3 compatibility matrix matches actual implementation and test results.
- Known limitations, operational risks, and recovery procedures are documented.

## 29. Final execution instructions

Start by inspecting the repository and its current state.

Then:

1. Summarize the existing implementation and major gaps.
2. Record the storage engine decision and architecture.
3. Create a prioritized implementation checklist.
4. Implement the first end-to-end working vertical slice.
5. Continue through the milestones without stopping at mockups or documentation.
6. Run tests after each meaningful change.
7. Fix regressions and repeat relevant tests.
8. Review security-sensitive changes carefully.
9. Verify the final deployment using isolated test data.
10. Produce a final report listing changed files, commands executed, test results, deployment instructions, and remaining risks.

If a feature depends on unavailable hardware, credentials, external services, or an unsupported backend capability, implement the safest feasible alternative and document the limitation.

Never fabricate test results, successful deployments, benchmark numbers, or S3 compatibility claims.

Never destroy production data, expose credentials, or deploy publicly without explicit authorization.

**Primary objective:** deliver a working, secure, maintainable, self-hosted S3-compatible object storage platform with a professional web console, reliable deployment, verified backups, and evidence-based test results.