# Implementation Checklist

Use this checklist after creating a repository from `ztemplate`, or as an audit checklist when porting the baseline into an existing repository.

## Bootstrap

- [ ] Create the new repository with **Use this template** when independent history is desired.
- [ ] Preview and apply `scripts/bootstrap.py` with the actual name, owner, code owner, and description.
- [ ] Review the changed identity/ownership files and commit `.ztemplate-initialized.json` as nonsecret setup evidence.
- [ ] Run `make validate-template`.
- [ ] Choose a project profile in `docs/profiles.md` and record non-goals.
- [ ] Confirm README and ABOUT describe the real project.

## Repository identity

- [ ] Replace template identity/placeholders with the real project.
- [ ] Confirm license choice and preserve required attribution.
- [ ] Configure repository description, topics, homepage, visibility, and template status as applicable.

## Ownership and governance

- [ ] Update `.github/CODEOWNERS`.
- [ ] Review `CONTRIBUTING.md`, `GOVERNANCE.md`, and `CODE_OF_CONDUCT.md`.
- [ ] Run the GitHub administration helper in dry-run mode.
- [ ] Apply repository administration controls with an authenticated admin identity.
- [ ] Verify the effective controls:

  ```bash
  python3 scripts/github_admin.py --repo OWNER/REPO --verify
  ```

- [ ] Require pull-request review and CODEOWNERS approval.
- [ ] Require passing status checks and conversation resolution.
- [ ] Block force pushes and deletion of the protected default branch.
- [ ] Document any break-glass role/process.

## Security

- [ ] Replace the generic support policy in `SECURITY.md`.
- [ ] Configure private vulnerability reporting.
- [ ] Enable Dependabot alerts and security updates.
- [ ] Review CodeQL language support for the actual stack.
- [ ] Keep dependency review enabled where supported.
- [ ] Enable secret scanning and push protection where available.
- [ ] Add stack-specific SAST, container, IaC, SBOM, provenance, and signing checks as appropriate.
- [ ] Confirm Actions permissions follow least privilege.
- [ ] Confirm fork PRs cannot access unsafe secrets or write tokens.
- [ ] Pin third-party/first-party Actions according to project supply-chain policy.

## Development

- [ ] Select runtime/framework/package manager and supported versions.
- [ ] Add formatter/linter/type-checker configuration as appropriate.
- [ ] Add unit, integration, and end-to-end tests.
- [ ] Replace placeholder Makefile targets with real commands.
- [ ] Replace/remove the placeholder Dockerfile.
- [ ] Populate `.env.example` with safe non-secret names only.
- [ ] Document local setup and deterministic dependency installation.

## Cloudflare and DNS

- [ ] Read `docs/cloudflare-terraform.md`.
- [ ] Keep public DNS/shared tunnel ownership in one designated infrastructure repository.
- [ ] Confirm the service answers locally before requesting a public hostname.
- [ ] Import existing records instead of recreating them.
- [ ] Confirm infrastructure plans do not destroy unrelated resources.
- [ ] Verify sibling/shared routes after changes.

## CI/CD

- [ ] Customize CI for the selected stack.
- [ ] Pin runtime versions and define supported-version matrices.
- [ ] Add real lint/test/build/package/security validation.
- [ ] Configure artifact retention and provenance where needed.
- [ ] Configure environments, approvals, and deployment protections.
- [ ] Validate CI from both pull requests and the protected default branch.

## Release

- [ ] Choose an explicit versioning policy.
- [ ] Maintain `CHANGELOG.md` and release notes.
- [ ] Configure publishing only when required.
- [ ] Publish from trusted workflows only.
- [ ] Add artifact signing/attestation where appropriate.
- [ ] Document known-good rollback.
- [ ] Verify rollback on production-equivalent infrastructure when applicable.

## Operations

- [ ] Define health/readiness checks.
- [ ] Configure structured logs, metrics, traces, and alert routing appropriate to the service.
- [ ] Define backup scope, retention, encryption, and ownership.
- [ ] Perform an isolated restore drill for stateful systems.
- [ ] Define RPO/RTO where applicable.
- [ ] Document incident response and escalation.
- [ ] Perform load/capacity testing when performance or scale is a readiness gate.

## Documentation

- [ ] Complete `docs/architecture.md`.
- [ ] Complete `docs/development.md`.
- [ ] Complete `docs/release.md`.
- [ ] Add ADRs for material decisions.
- [ ] Document operational ownership and support expectations.

## Final verification

- [ ] Fresh clone works with documented setup.
- [ ] Repository validator passes.
- [ ] Required CI/security checks pass on the exact release head.
- [ ] Protected-branch/admin verification succeeds.
- [ ] No secrets/private data are committed.
- [ ] Deployment is verified in the intended environment.
- [ ] Backup/restore and rollback evidence exists where applicable.
- [ ] Every applicable production-readiness gate is `VERIFIED`; any `NOT APPLICABLE` gate has an explicit reason.

A green template CI run alone is never sufficient to claim application production readiness.
