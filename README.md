# zTemplate

A reusable, security-oriented GitHub project starting point with governance, engineering guidance, CI, AI-agent operating rules, repository validation, and an automated GitHub administration gate.

This repository is a **template foundation**, not a deployable application. A repository generated from it still requires stack-specific implementation, deployment, recovery, observability, and security evidence before that application can be called production ready.

## Create a new project

1. Click **Use this template** on GitHub and clone the generated repository.
2. Preview project initialization:

   ```bash
   python3 scripts/bootstrap.py --name my-service --owner my-org --codeowner my-org/maintainers --description 'New service'
   ```

3. Apply explicitly, inspect the diff, and review ownership/security files:

   ```bash
   python3 scripts/bootstrap.py --name my-service --owner my-org --codeowner my-org/maintainers --description 'New service' --apply
   make validate-template
   ```

4. Select an optional [project profile](docs/profiles.md), replace placeholder `Makefile` / `Dockerfile`, and complete the [Implementation Checklist](IMPLEMENTATION-CHECKLIST.md).
5. Configure and verify repository administration controls from an authenticated GitHub admin identity:

   ```bash
   python3 scripts/github_admin.py --repo my-org/my-service --apply
   ```

6. Add stack-specific CI, security, release, deployment, backup/restore, rollback, monitoring, and operational evidence.

See the complete [startup guide](docs/startup.md).

## Included

- Issue and pull request templates
- CODEOWNERS and governance guidance
- Security and support policies
- CI repository-baseline validation
- CodeQL security scanning
- Dependency Review
- Dependabot configuration
- Immutable SHA pinning for baseline GitHub Actions
- Release guidance and release-note configuration
- Repository structure and local Markdown-link validation
- GitHub administration automation with read-back verification
- Conventional commit / PR guidance
- Documentation, ADR, changelog, and roadmap structure
- Environment example
- Docker and Makefile placeholders
- Cloudflare/Terraform ownership contract
- Cross-agent ZEAZ engineering execution layer
- Reusable skill/catalog structure

## Repository administration gate

`scripts/github_admin.py` is dry-run by default.

Apply and verify:

```bash
python3 scripts/github_admin.py --repo OWNER/REPO --apply
```

Verify without mutation:

```bash
python3 scripts/github_admin.py --repo OWNER/REPO --verify
```

The helper is designed to enforce or verify:

- pull-request review before merge
- CODEOWNERS review
- stale-review dismissal
- approval after the latest push
- conversation resolution
- strict required status checks
- administrator enforcement
- no force pushes
- no protected-branch deletion
- Dependabot vulnerability alerts/security fixes
- private vulnerability reporting
- secret scanning/push protection when available
- read-only default Actions token permissions
- Actions cannot approve pull requests

Presence of this script is not evidence that a generated repository is configured. The effective settings must be read back successfully.

See [GitHub repository administration gate](docs/ai/guides/github-repository-admin.md).

## AI engineering execution layer

- [ZEAZ engineering execution framework](ZEAZ-INTRODUCTION.md)
- [Repository agent contract](AGENTS.md)
- [Claude Code instructions](CLAUDE.md)
- [OpenCode instructions](OPENCODE.md)
- [Reusable AI playbooks and prompts](docs/ai/README.md)
- [ECC integration](docs/ai/guides/ecc-integration.md)
- [ZEAZ skills catalog](skills/README.md)

These files guide execution and evidence handling. They are not production-readiness evidence by themselves.

## DNS and public hostnames

Do not add duplicate Cloudflare/DNS ownership to a generated project. Public DNS and shared tunnel ingress must have one designated owning repository.

See [Cloudflare and Terraform ownership](docs/cloudflare-terraform.md).

## Template limitations

- Baseline CI validates template structure and bootstrap behavior; generated projects must add real application lint/build/test/security checks.
- The included Dockerfile and application Makefile targets are placeholders and must not ship unchanged.
- Shared infrastructure remains owned by the designated infrastructure repository.
- `scripts/github_admin.py` requires an authenticated GitHub identity with repository Administration permission.
- A green CI run proves only the checks that actually ran; it does not prove application production readiness.
- Generated repositories must independently verify deployment, rollback, backup/restore, observability, security, capacity, and incident-response gates that apply to the real system.

## Repository structure

```text
.github/
  ISSUE_TEMPLATE/
  workflows/
  CODEOWNERS
  PULL_REQUEST_TEMPLATE.md
  dependabot.yml
  release.yml
docs/
  adr/
  ai/
    guides/
    playbooks/
    prompts/
  architecture.md
  development.md
  release.md
  repository-rollout.md
  startup.md
scripts/
  bootstrap.py
  github_admin.py
  validate_repo.py
skills/
components.d/
plugins.d/
AGENTS.md
CLAUDE.md
OPENCODE.md
ZEAZ-INTRODUCTION.md
ecc-install.json
CHANGELOG.md
CODE_OF_CONDUCT.md
CONTRIBUTING.md
GOVERNANCE.md
IMPLEMENTATION-CHECKLIST.md
LICENSE
Makefile
README.md
ROADMAP.md
SECURITY.md
```

## Principles

- Secure by default
- Least privilege
- Immutable/reproducible automation where practical
- Small, reviewable pull requests
- Documentation as part of delivery
- Evidence-backed readiness claims
- No weakening of security gates merely to make CI green
- Explicit rollback/recovery practices
- Repository-specific policy is preserved when rolling the baseline into existing projects

## Rollout to existing repositories

Do not bulk-copy this template over an established repository.

Use the [repository rollout guide](docs/repository-rollout.md) to audit the target first and port only missing compatible controls.

## License

MIT. See `LICENSE`.
