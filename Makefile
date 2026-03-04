.PHONY: install fmt lint typecheck test check clean

# ── Setup ────────────────────────────────────────────────────────────

install:  ## Install package + dev deps
	uv sync
	uv run pre-commit install

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

bump:  ## Bump version: make bump v=0.2.0
	@[ "$(v)" ] || { echo "Usage: make bump v=X.Y.Z"; exit 1; }
	@echo "Bumping to $(v) ..."
	@sed -i '' 's/^version = ".*"/version = "$(v)"/' pyproject.toml
	@uv lock -q
	@echo "Done — $(v)"

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
