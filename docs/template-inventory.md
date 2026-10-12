# Repository Template Inventory

This repository provides a secure, reusable baseline for new GitHub projects and a reference baseline for hardening existing repositories.

## Governance and community

- `AGENTS.md`
- `README.md`
- `ABOUT.md`
- `CONTRIBUTING.md`
- `CODE_OF_CONDUCT.md`
- `GOVERNANCE.md`
- `SECURITY.md`
- `.github/SUPPORT.md`
- issue forms / pull-request template
- CODEOWNERS

## Repository automation and security

- baseline CI
- CodeQL
- Dependency Review
- Dependabot
- release-note configuration
- immutable SHA pins for baseline Actions
- least-privilege workflow permissions
- `scripts/validate_repo.py`
- `scripts/github_admin.py`
- protected-branch/security-setting read-back verification

## AI/agent execution layer

- `ZEAZ-INTRODUCTION.md`
- `AGENTS.md`
- `CLAUDE.md`
- `OPENCODE.md`
- `docs/ai/guides/`
- `docs/ai/playbooks/`
- `docs/ai/prompts/`
- `skills/`
- `components.d/`
- `plugins.d/`
- `ecc-install.json`
- `.agents/skills/scrutinize/SKILL.md`

## Engineering lifecycle

- `CHANGELOG.md`
- `ROADMAP.md`
- `IMPLEMENTATION-CHECKLIST.md`
- architecture/development/release/ADR documentation
- Cloudflare/Terraform ownership contract
- repository rollout guide
- Dockerfile / Makefile / environment example
- EditorConfig / Git attributes / Git ignore baseline

## Project initialization

- `scripts/bootstrap.py`
- `tests/test_bootstrap.py`
- `templates/project-readme.md`
- `templates/project-about.md`
- `docs/startup.md`
- `docs/profiles.md`

Application Makefile targets intentionally fail until customized instead of reporting false success.

## Adoption principle

For a new project, inherit the baseline then customize it.

For an existing project, audit first and port only missing compatible controls. Do not overwrite repository-specific architecture, CI, AGENTS rules, release contracts, or operational evidence merely to match the template.

See [repository rollout](repository-rollout.md).

Never copy production credentials into a generated or migrated repository.
