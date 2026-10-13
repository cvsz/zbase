# ADR 0001 — Storage engine selection

- **Status:** accepted
- **Date:** 2026-10-13
- **Deciders:** S3Vault maintainers

## Context

S3Vault needs an object store that owns object bytes, durability and the S3
protocol. The application layer must not reimplement any of that. The engine
choice therefore determines the compatibility surface, the failure modes and
the backup story.

## Decision

Use **SeaweedFS 3.97** in S3 gateway mode as the default storage engine,
behind the `StorageAdapter` protocol, selected by `STORAGE_ENGINE`.

## Considered options

### SeaweedFS — selected

- **S3 compatibility:** measured, not assumed. The probe in
  `SeaweedFSAdapter.probe()` exercises each feature against a live engine and
  reports 16 supported features and 2 unsupported ones. See
  [S3-COMPATIBILITY.md](../S3-COMPATIBILITY.md).
- **Single and multi node:** both supported; single node is what S3Vault ships.
- **Durability:** depends entirely on the volume configuration. S3Vault does
  not claim redundancy it has not configured.
- **Multipart:** supported, with a measured 5 MiB per-part minimum once a
  multipart upload has two or more parts.
- **Authentication:** the engine has a single root identity. S3Vault holds it
  in the environment and never exposes it; scoping is enforced by S3Vault
  before it signs a request.
- **Lifecycle:** supported, with rule-ID normalisation.
- **Observability:** basic. No usage API, so S3Vault measures by listing.
- **Upgrades:** image tag plus digest pinning; `scripts/update-images.sh` for
  deliberate updates.
- **Resources:** modest. Comfortable on a 2-core/4 GiB host.
- **Backup:** no documented snapshot API. Backups archive the volume, and
  `scripts/backup.sh` records in the manifest which method produced a snapshot.
- **Licence:** Apache-2.0. Compatible, with attribution only.

### Garage — rejected

Lightweight and genuinely distributed, but its V2 S3 API is partial and
documented upstream as having SigV4 quirks. For a product whose primary job is
S3 compatibility, that is a poor trade against SeaweedFS's broader surface.

### MinIO — rejected for now

Historically the closest S3 parity, but the community edition's licensing and
distribution terms have changed repeatedly, and the current release requires
accepting AGPLv3. Rather than depend on terms that have moved before, S3Vault
targets Apache-2.0. The adapter boundary means MinIO can be added later if a
permissively licensed distribution stabilises.

## Consequences

**Accepted:**

- Bucket policies cannot be offered; the engine has no implementation.
- Usage figures must be measured by listing, which is O(objects).
- Bucket deletion is not protected by the engine, so S3Vault performs its own
  emptiness check and requires an explicit name confirmation.
- Multipart uploads must respect a 5 MiB per-part minimum above two parts.

**Gained:**

- A permissive licence with no operator obligations beyond attribution.
- A single-binary deployment that fits a small host.
- An adapter boundary that keeps the choice reversible.

**Neutralised by design:**

- The engine's single root identity is contained: it lives only in the API
  process, and every scoped decision is made by S3Vault before signing.

## Revisit when

- The engine's usage API becomes available, removing the O(objects) listing.
- Bucket policies become available, unlocking cross-account sharing.
- A permissively licensed MinIO distribution stabilises.
- Requirements change to need erasure coding or multi-region durability.