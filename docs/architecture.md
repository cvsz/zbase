# Architecture

## Purpose

`ztemplate` is a language-agnostic repository foundation rather than an application runtime.

Its architecture is intentionally layered so generated projects can replace application-specific pieces without weakening repository governance.

## Layers

### Repository governance

Root policy documents define ownership, contribution, security, evidence, and release expectations:

- `AGENTS.md`
- `ZEAZ-INTRODUCTION.md`
- `GOVERNANCE.md`
- `SECURITY.md`
- `CONTRIBUTING.md`

### GitHub control plane

`.github/` provides CI/security/community baselines.

`scripts/github_admin.py` handles repository settings that cannot be enforced by committed files alone and verifies effective provider state after mutation.

### Validation

`scripts/validate_repo.py` checks required template structure and local Markdown links.

`tests/` validates bootstrap behavior.

Application-specific lint/test/build/security validation must be added by generated projects.

### Project initialization

`scripts/bootstrap.py` changes project identity/ownership routing only. It does not create application architecture or deploy infrastructure.

### AI/agent execution

`docs/ai/`, `skills/`, `components.d/`, and `plugins.d/` provide reusable agent execution guidance and discovery without making application-runtime assumptions.

### Application placeholders

The root Dockerfile and application Makefile targets are intentionally non-production placeholders. Generated projects replace them with stack-specific implementations.

## Existing-repository adoption

The architecture is composable. Established repositories should use [repository rollout](repository-rollout.md) and port only compatible missing layers rather than copying the tree wholesale.

## Production boundary

Repository-foundation readiness and application production readiness are separate claims.

The template can verify repository controls, documentation structure, and baseline automation. A generated application must independently verify runtime architecture, deployment, data, recovery, observability, capacity, security, and incident-response gates that apply to it.
