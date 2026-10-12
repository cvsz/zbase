# Development

## Local setup

1. Clone the repository.
2. Copy `.env.example` to an ignored `.env` and populate local-only values.
3. Install the selected runtime and dependencies.
4. Replace placeholder Makefile targets with real project commands.
5. Run:

   ```bash
   make validate-template
   ```

6. Run project-specific formatting, linting, type checks, tests, build, and security checks before opening a pull request.

## Quality expectations

- Keep changes small and reviewable.
- Read `AGENTS.md` and narrower subtree instructions before editing.
- Add tests for behavior changes.
- Prefer deterministic/reproducible tooling.
- Keep CI permissions least privilege.
- Pin Actions/dependencies according to the project's supply-chain policy.
- Do not commit secrets or local credentials.
- Do not weaken security/CI gates to obtain a passing build.
- Fix the root cause of failed validation rather than excluding relevant checks.

## Pull-request validation

Before merge, capture exact-head evidence for the checks that apply to the change. Do not rely on a previously green commit after the PR head has changed.

For repository administration changes, use the read-back verifier:

```bash
python3 scripts/github_admin.py --repo OWNER/REPO --verify
```

## Documentation

Update architecture, development, release, governance, security, ADRs, and operational docs when their assumptions change.

Documentation must distinguish intended configuration from verified effective state.
