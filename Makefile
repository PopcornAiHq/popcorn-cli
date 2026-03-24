.PHONY: install install-local dev fmt lint typecheck test check clean release

# ── Setup ────────────────────────────────────────────────────────────

install:  ## Install package + dev deps
	uv sync
	uv run pre-commit install

install-local:  ## Install CLI globally from local repo (uv tool)
	uv tool install --force "$(CURDIR)"
	@popcorn --version

dev:  ## Create bin/popcorn wrapper for local dev (doesn't affect global install)
	@mkdir -p bin
	@printf '#!/bin/sh\nexec uv run --project "%s" popcorn "$$@"\n' "$(CURDIR)" > bin/popcorn
	@chmod +x bin/popcorn
	@echo "Created bin/popcorn — add $(CURDIR)/bin to PATH or run ./bin/popcorn"

# ── Code quality ─────────────────────────────────────────────────────

fmt:  ## Format code
	uv run ruff format .

lint:  ## Lint code (with auto-fix)
	uv run ruff check --fix .

typecheck:  ## Type-check with mypy
	uv run mypy src/popcorn_core src/popcorn_cli

test:  ## Run tests
	uv run pytest $(if $(p),$(p),tests/)

test-cov:  ## Run tests with coverage
	uv run pytest --cov=popcorn_core --cov=popcorn_cli \
		--cov-report=term-missing tests/

check: lint typecheck test  ## Run all checks (lint + typecheck + test)

# ── Version ──────────────────────────────────────────────────────────

bump:  ## Bump version + tag: make bump v=0.2.0
	@[ "$(v)" ] || { echo "Usage: make bump v=X.Y.Z"; exit 1; }
	@echo "Bumping to $(v) ..."
	@sed -i '' 's/^version = ".*"/version = "$(v)"/' pyproject.toml
	@uv lock -q
	@git add pyproject.toml uv.lock
	@git commit -m "chore: bump version to $(v)"
	@git tag "v$(v)"
	@echo "Done — $(v) (tagged v$(v))"
	@echo "Run 'git push && git push --tags' to publish, then 'make release' for GitHub release"

release:  ## Create GitHub release from latest tag
	@tag=$$(git describe --tags --abbrev=0) && \
	prev=$$(git describe --tags --abbrev=0 "$$tag^" 2>/dev/null || git rev-list --max-parents=0 HEAD) && \
	notes=$$(git log --pretty=format:"- %s" "$$prev..$$tag" | grep -v "^- chore:") && \
	echo "Creating release $$tag ..." && \
	gh release create "$$tag" --title "$$tag" --notes "$$notes" && \
	echo "Done — $$tag released"

# ── Cleanup ──────────────────────────────────────────────────────────

clean:  ## Remove caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true

# ── Help ─────────────────────────────────────────────────────────────

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
