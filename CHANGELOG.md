# Changelog

All notable changes to this template are documented here.

The format follows Keep a Changelog conventions; generated projects should adopt an explicit versioning policy appropriate to their product.

## [Unreleased]

### Added

- Safe project identity bootstrap with explicit dry-run/apply and idempotence tests.
- `tests/integration/test_api.py`: `test_credential_rotation_records_rotated_from_on_replacement` verifies `rotated_from` is recorded on the replacement row (not inverted onto the revoked row).
- `tests/integration/test_api.py`: `test_credential_rotation_chain_maintains_rotated_from` verifies multi-rotation chain integrity.
- `tests/integration/test_api.py`: `test_credential_rotation_of_revoked_credential_fails` verifies revoked credentials cannot be re-rotated.
- `tests/integration/test_api.py`: `test_concurrent_credential_rotation_is_safe` verifies `SELECT FOR UPDATE` serializes concurrent credential rotations.
- `docs/audit/`: executive summary, architecture review, security assessment, gap analysis, remediation report, test evidence, production readiness, and residual risks.

### Fixed

- `services/credentials.py`: `CredentialService.rotate()` wrote `rotated_from` onto the revoked (old) row pointing at the replacement — semantically inverted. The replacement row now records `rotated_from = <old credential id>` so the column reads "this credential was rotated from <old id>".
- `services/credentials.py`: `CredentialService.rotate()` now performs all mutations (insert replacement, revoke original, set `rotated_from`) inside a single database transaction with `SELECT ... FOR UPDATE` row lock, so a crash can never leave both credentials active or lose the rotation audit relationship, and concurrent rotations are serialized.
- `.env` and `.env.production`: replaced realistic secret values (engine keys, DB password, KEK, bootstrap admin password, hostnames) with `REPLACE_ME_...` placeholders; both files remain gitignored and untracked.
- `security/tokens.py`: removed dead `_SECRET_KEY_ALPHABET` expression and unused `base64` import.
- `main.py`: removed dead `cfg.host if hasattr(cfg, "host")` path (AppConfig has no `host`); uvicorn host is `0.0.0.0`.
- `scripts/{backup,restore,test-integration}.sh`: `cd "$REPO_ROOT" || exit` (shellcheck SC2164), unused loop variable removed (SC2034), and a targeted `shellcheck disable=SC2329` with rationale for the trap-invoked `cleanup()`. `shellcheck scripts/*.sh` now exits 0.

### Security

- `.env.production` secret placeholder hygiene removes a local secret-leakage risk; no `.env`, `.pem`, `id_rsa`, or secret-shaped files are tracked.
- Generated README/ABOUT templates and startup/profile documentation.
- CI bootstrap test coverage and fail-closed Makefile placeholders.
- ZEAZ cross-agent engineering execution framework.
- Claude Code and OpenCode adapters.
- Reusable AI guides, playbooks, prompts, skills, component manifests, and plugin-source manifests.
- ECC OSS CLI install configuration with explicit separation from hosted ECC App evidence.
- Repository structure and local Markdown-link validator.
- GitHub administration automation with dry-run, explicit apply, and read-back verification.
- Repository rollout guidance for applying the baseline safely to existing repositories.

### Changed

- Reorganized reusable AI documentation into `guides/`, `playbooks/`, and `prompts/`.
- Pinned baseline first-party GitHub Actions to immutable commit SHAs.
- Expanded CODEOWNERS coverage for repository policy, AI, ECC, skills, components, and plugin manifests.
- Clarified that release-note configuration is not an artifact-publishing workflow.
- Documented branch/security administration as an explicit evidence gate rather than a documentation-only checklist.

### Fixed

- Corrected generated README link validation so template links are resolved from their generated root location.
- Removed stale wording that could imply green CI or configuration files alone establish production readiness.

### Security

- Added fail-closed repository-administration verification.
- Added protected-branch controls for required reviews/checks, conversation resolution, force-push prevention, and deletion prevention.
- Added documented verification for Dependabot, private vulnerability reporting, secret scanning/push protection, and least-privilege Actions permissions where supported.
