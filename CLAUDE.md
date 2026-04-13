# CLAUDE.md — popcorn-cli

CLI for the Popcorn API. Published to PyPI as `popcorn-cli`, installs the `popcorn` command.

## Structure

```
popcorn-cli/
├── src/
│   ├── popcorn_core/          ← Shared lib (auth, client, config, resolve, operations)
│   └── popcorn_cli/           ← CLI (argparse, handlers, formatting)
├── tests/                     ← pytest (~228 tests)
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

**Proxy mode** (`POPCORN_PROXY_MODE=1`): For VM sidecar deployments. Skips auth entirely — no token refresh, no browser login. Sends `X-Actor-User-ID` and `X-Workspace-ID` headers instead of `Authorization`. Configured via `POPCORN_API_URL`, `POPCORN_WORKSPACE_ID`, `POPCORN_USER_ID`.

**No internal URLs or credentials are shipped in this package.**

## Testing Installation

```bash
./scripts/test-install.sh    # Docker-based: tests pip, pipx, uv install
```

Builds the wheel and verifies it installs correctly with each package manager in isolated containers.

## Versioning

**Bump the version after every meaningful commit to main** (direct or PR merge).

- **Patch** (0.5.5 → 0.5.6): default for most changes — bug fixes, small features, refactors
- **Minor** (0.5.x → 0.6.0): larger features, new commands, breaking-ish changes
- **Major**: never bump unless explicitly told

```bash
make bump             # Auto-patch bump (0.7.4 → 0.7.5)
make bump v=X.Y.Z    # Explicit version
```

Version lives only in `pyproject.toml` — runtime reads it via `importlib.metadata`.

A pre-commit hook (`scripts/check-version-bump.sh`) warns if `src/` files are staged without a `pyproject.toml` change, as a reminder to bump.

## Publishing

```bash
make bump             # or: make bump v=X.Y.Z
uv build
uv publish
```

## API Alignment

**The backend OpenAPI spec is the source of truth.** Always fetch and check it when adding or modifying commands:

```bash
popcorn api /openapi.json --raw > /tmp/popcorn-openapi.json
```

The spec is auto-generated from FastAPI's Pydantic models and route definitions. It gives you exact field names, types, HTTP methods, and required/optional status for every endpoint. Do not guess or assume — fetch the spec.

## Agent-Facing Contract

This CLI is designed to be consumed by LLM agents as well as humans. Treat the following as a **stable public contract** — breaking any of it is a minor version bump at minimum.

- **Agent mode:** `POPCORN_AGENT=1` implies `--json`, `--quiet`, `--no-color`, and `POPCORN_NO_UPDATE_CHECK=1`. Injected in `_hoist_global_flags` (`cli.py`).
- **Success envelope:** `{"ok": true, "data": ...}`. `_json_ok` (`cli.py`) strips any leaked top-level `ok` key from `data` so the CLI envelope is never shadowed by an upstream API response envelope.
- **Error envelope:** `{"ok": false, "error": "...", "error_code": "...", "code": "...", "retryable": bool, ...}`.
  - `error_code` is the **stable** machine-readable enum agents should branch on.
  - `code` is the Python exception class name (legacy, avoid branching on).
  - Enum values and their descriptions live in `popcorn_core.errors.ERROR_CODES`. `APIError.error_code` derives from HTTP status via `_api_status_to_error_code`.
  - When raising `PopcornError` for a specific failure (e.g. not found, conflict), pass `error_code="not_found"` so agents can branch cleanly.
- **Exit codes:** defined in `popcorn_core.errors` (`EXIT_OK`, `EXIT_VALIDATION`, `EXIT_AUTH`, `EXIT_CLIENT`, `EXIT_SERVER`, `EXIT_UNHEALTHY`, `EXIT_INTERRUPT`). Semantic — agents switch on these to decide retry vs bail.
- **Schema discovery:** `popcorn commands --json` emits the full schema including `exit_codes`, `error_codes`, `envelope`, `agent_mode`, `global_flags`, and every command's arg types. Update this when adding agent-facing surface (`cmd_commands` in `cli.py`).
- **Confirmation prompts:** interactive confirmations go through `_confirm(args, prompt)` in `cli.py`. It honors `--yes`/`-y` and `POPCORN_ASSUME_YES=1`, and **fails loudly** (raises `PopcornError`) in non-TTY mode otherwise — never silently no-op or hang. When adding a destructive op that needs confirmation, use `_confirm`, not `input()`.
- **`api --data` body sources:** `_resolve_data_arg` accepts literal JSON, `@-` (stdin), or `@path` (file), matching `curl` and `gh api`. Agents piping large payloads should use `@-`.
- **Streaming (`--watch`):** goes through `_json_line` (not `_json_ok`) — one NDJSON envelope per line, no pretty-printing, flushed every write. Same `_strip_leaked_ok` applies. `_json_ok` / `_json_line` are the two allowed JSON-output paths; don't hand-roll envelopes.
- **Pagination:** paginated commands include `data.pagination.next` — a dict of CLI flag→value pairs the agent feeds back to the same command for the next page, or `null` when no more. Use `_attach_pagination(data, next_flags)` to emit the field. Applied to `message list` (cursor-based, `has_more`), `message search` (offset-based, `has_more`), `message threads` and `workspace inbox` (offset-based, heuristic `len == limit` — worst case the agent fetches one empty page). `webhook deliveries` is deferred until the API exposes a reliable cursor.
- **`popcorn doctor`:** returns a structured diagnostic report (auth state, API reachability + latency, config file permissions, relevant env vars, list of detected `issues`). `--json` emits the full dict — the canonical agent/support-debug entry point when a user reports "popcorn isn't working". When adding a new failure mode the CLI should diagnose, append to the `issues` list in `cmd_doctor`.

## Conventions

- Color output respects `NO_COLOR` env var and `--no-color` flag
- All API errors surfaced as `PopcornError` subclasses (no tracebacks for users)
- Channel name resolution cached 5 min (`#name` → UUID)
- Pre-commit runs ruff (format + lint) and version-bump reminder on every commit
