# CLAUDE.md — popcorn-cli

CLI for the Popcorn API. Published to PyPI as `popcorn-cli`, installs the `popcorn` command.

## Structure

```
popcorn-cli/
├── src/
│   ├── popcorn_core/          ← Shared lib (auth, client, config, resolve, operations)
│   └── popcorn_cli/           ← CLI (argparse, handlers, formatting)
├── tests/                     ← pytest (189 tests)
├── scripts/                   ← test-install.sh (Docker-based install tests)
├── pyproject.toml             ← Single package config
├── Makefile                   ← fmt, lint, typecheck, test, check, dev
└── .pre-commit-config.yaml
```

## Development

```bash
make install    # uv sync + pre-commit install
make dev        # create bin/popcorn wrapper for local dev
make fmt        # ruff format
make lint       # ruff check --fix
make typecheck  # mypy
make test       # pytest
make check      # lint + typecheck + test
```

## Key Details

- **Entry point:** `popcorn = "popcorn_cli.cli:main"`
- **Dependencies:** `httpx` (HTTP client), `pyjwt` (JWT decode)
- **Build system:** hatchling
- **Python:** >=3.10
- **Version:** runtime via `importlib.metadata` — update only in `pyproject.toml`

## Auth

Clerk OAuth PKCE flow with two modes:
- **Browser:** opens auth page, local callback server on port 28771
- **Headless:** `echo $TOKEN | popcorn auth login --with-token`
- **Refresh:** automatic on 401, uses stored refresh_token

Config stored at `~/.config/popcorn/auth.json` (0600 permissions).

## Environments

Default: `https://api.popcorn.ai` (production)

Custom environments via env vars (for internal/dev use):
- `POPCORN_API_URL` — API base URL
- `POPCORN_CLERK_ISSUER` — Clerk OIDC issuer URL
- `POPCORN_CLERK_CLIENT_ID` — Clerk OAuth client ID

Multiple profiles are stored in the config file. Switch with `popcorn env <name>`.

**No internal URLs or credentials are shipped in this package.**

## Testing Installation

```bash
./scripts/test-install.sh    # Docker-based: tests pip, pipx, uv install
```

Builds the wheel and verifies it installs correctly with each package manager in isolated containers.

## Publishing

```bash
# Bump version in pyproject.toml
make bump v=X.Y.Z
uv build
uv publish
```

## Conventions

- Color output respects `NO_COLOR` env var and `--no-color` flag
- All API errors surfaced as `PopcornError` subclasses (no tracebacks for users)
- Channel name resolution cached 5 min (`#name` → UUID)
- Pre-commit runs ruff (format + lint) on every commit
