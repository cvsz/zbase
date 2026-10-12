# Changelog

All notable changes to this template are documented here.

The format follows Keep a Changelog conventions; generated projects should adopt an explicit versioning policy appropriate to their product.

## [Unreleased]

### Added

- Safe project identity bootstrap with explicit dry-run/apply and idempotence tests.
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
