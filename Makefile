# S3Vault developer and operator targets.
#
# `validate-template` is inherited from ztemplate and must keep working; the
# project targets below replace the template placeholders with real commands.

SHELL := /bin/bash
PY := .venv/bin/python
PIP := .venv/bin/pip
COMPOSE ?= docker compose
HOSTNAME ?= s3vault.test

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- inherited
.PHONY: validate-template
validate-template: ## Inherited ztemplate structure and bootstrap validation
	python3 scripts/validate_repo.py
	python3 -m unittest discover -s tests -v

# ------------------------------------------------------------------- set-up
.PHONY: venv
venv: ## Create the virtualenv and install pinned dependencies
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

.PHONY: fmt-check
fmt-check: ## Syntax-compile every module (formatting gate)
	$(PY) -m compileall -q apps scripts/../tests

.PHONY: lint
lint: ## Lint with ruff if available; otherwise compile-check
	@if .venv/bin/ruff --version >/dev/null 2>&1; then \
	  .venv/bin/ruff check apps tests; \
	else \
	  echo "ruff not installed; falling back to compileall"; \
	  $(PY) -m compileall -q apps; \
	fi

.PHONY: typecheck
typecheck: ## Static typing when mypy is available
	@if .venv/bin/mypy --version >/dev/null 2>&1; then \
	  .venv/bin/mypy --ignore-missing-imports apps; \
	else \
	  echo "mypy not installed; skipping (install with: $(PIP) install mypy)"; \
	fi

# -------------------------------------------------------------------- tests
.PHONY: test
test: test-unit ## Unit tests only (no external services)

.PHONY: test-unit
test-unit: ## Unit tests
	PYTHONPATH=apps/api $(PY) -m pytest tests/unit -q

.PHONY: test-integration
test-integration: ## Integration, S3-compatibility and security suites against an isolated stack
	scripts/test-integration.sh --unit

.PHONY: test-stack-up
test-stack-up: ## Start the isolated PostgreSQL + SeaweedFS stack
	scripts/test-stack.sh up

.PHONY: test-stack-down
test-stack-down: ## Stop the isolated stack
	scripts/test-stack.sh down

.PHONY: test-stack-status
test-stack-status: ## Report the isolated stack
	scripts/test-stack.sh status

# --------------------------------------------------------------------- ci
.PHONY: ci
ci: fmt-check lint test-unit validate-template ## Fast CI gate (no containers)

.PHONY: check
check: ci ## Alias for ci

# ------------------------------------------------------------------ build
.PHONY: build
build: ## Build the container image
	$(COMPOSE) build

.PHONY: up
up: ## Start the deployment
	$(COMPOSE) up -d

.PHONY: down
down: ## Stop the deployment
	$(COMPOSE) down

.PHONY: logs
logs: ## Follow logs
	$(COMPOSE) logs -f --tail=100

.PHONY: ps
ps: ## Show container status
	$(COMPOSE) ps

.PHONY: restart
restart: ## Restart the application and worker only
	$(COMPOSE) restart api worker

# ------------------------------------------------------------- operations
.PHONY: migrate
migrate: ## Apply database migrations
	$(COMPOSE) exec -T api python -c "import sys; sys.path.insert(0,'/app/apps/api'); \
	 from s3vault.config import load_config; from s3vault.db import Database, Migrator; \
	 d=Database(load_config()); print(Migrator(d).apply()); d.close()"

.PHONY: bootstrap-admin
bootstrap-admin: ## Create the first administrator from BOOTSTRAP_ADMIN_* variables
	$(COMPOSE) exec -T api python -c "import sys; sys.path.insert(0,'/app/apps/api'); \
	 from s3vault.bootstrap import main; raise SystemExit(main())"

.PHONY: backup
backup: ## Create a backup
	scripts/backup.sh

.PHONY: verify-backup
verify-backup: ## Verify the newest backup
	@latest=$$(ls -1dt backups/*/ 2>/dev/null | head -1); \
	 if [ -z "$$latest" ]; then echo "no backups found"; exit 1; fi; \
	 scripts/verify-backup.sh "$$latest"

.PHONY: restore-drill
restore-drill: ## Restore the newest backup into an isolated drill stack
	@latest=$$(ls -1dt backups/*/ 2>/dev/null | head -1); \
	 if [ -z "$$latest" ]; then echo "no backups found"; exit 1; fi; \
	 scripts/restore.sh "$$latest"

.PHONY: verify-install
verify-install: ## Verify a deployed instance
	scripts/verify-install.sh "$(BASE_URL)"

.PHONY: shell
shell: ## Open a shell in the API container
	$(COMPOSE) exec api /bin/sh

.PHONY: clean
clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true