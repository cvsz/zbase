# Start a project from ztemplate

This repository is a language-agnostic governance/tooling baseline, not a deployable product.

Use GitHub **Use this template** when independent history is desired, clone the generated repository, and work through a feature branch.

## 1. Initialize project identity

Requires Python 3.10+ and Git.

Preview:

```bash
python3 scripts/bootstrap.py --name my-service --owner my-org --codeowner my-org/maintainers --description 'Describe the product'
```

Apply:

```bash
python3 scripts/bootstrap.py --name my-service --owner my-org --codeowner my-org/maintainers --description 'Describe the product' --apply
```

Review `git diff` and commit the generated identity marker only after confirming the resulting project metadata and ownership.

## 2. Validate the inherited baseline

```bash
make validate-template
```

This validates repository structure, local Markdown links, and bootstrap behavior. Application-specific Makefile targets intentionally fail until replaced.

## 3. Configure repository administration

The inherited script is dry-run by default:

```bash
python3 scripts/github_admin.py --repo my-org/my-service
```

Apply and read back effective settings:

```bash
python3 scripts/github_admin.py --repo my-org/my-service --apply
```

Later verification:

```bash
python3 scripts/github_admin.py --repo my-org/my-service --verify
```

This step requires an authenticated `gh` identity with repository Administration permission.

Do not treat the presence of the script as proof that branch/security settings are enabled.

## 4. Make project-specific decisions

- choose language/runtime/framework/package manager and supported versions
- review licensing and attribution
- replace the placeholder Dockerfile and Makefile application commands
- set real maintainers/CODEOWNERS
- replace the generic security support policy
- define authentication/authorization and data-retention requirements
- define secrets, environments, release policy, and deployment protections
- add stack-specific CI/security checks
- choose backup/recovery/rollback requirements
- define observability and incident ownership

See [profiles](profiles.md) and the [implementation checklist](../IMPLEMENTATION-CHECKLIST.md).

## 5. Development baseline

Copy `.env.example` to a local ignored `.env`. Add real code, tests, build tooling, security checks, and deterministic dependency installation.

Do not remove inherited security checks merely to simplify CI.

## 6. Operations and release evidence

Complete:

- [architecture](architecture.md)
- [development](development.md)
- [release](release.md)
- [implementation checklist](../IMPLEMENTATION-CHECKLIST.md)

For an application intended for production, establish environment-appropriate evidence for applicable gates such as:

- deployment verification
- authenticated health/readiness checks
- structured logs/metrics/traces
- alert routing
- backup and isolated restore
- rollback to a known-good build
- RPO/RTO
- load/capacity validation
- security response contacts
- incident/runbook procedures

Green repository CI is not proof of application production readiness.

## 7. Public hostnames

Follow the [central Cloudflare/DNS ownership contract](cloudflare-terraform.md). Never introduce duplicate ownership of public DNS or a shared tunnel from an application repository when a designated infrastructure repository already owns it.

## 8. Existing repositories

When adopting this baseline into an established project, do not copy the template wholesale. Follow the [repository rollout guide](repository-rollout.md) and preserve repository-specific architecture, policy, tests, and operational contracts.
