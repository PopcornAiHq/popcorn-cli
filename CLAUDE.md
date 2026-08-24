# CLAUDE.md — popcorn-cli

CLI for the Popcorn API, installing the `popcorn` command. **Not published to PyPI** — it is installed and upgraded straight from this GitHub repo.

## Structure

```
popcorn-cli/
├── src/
│   ├── popcorn_core/          ← Shared lib (auth, client, config, resolve, operations)
│   └── popcorn_cli/           ← CLI (argparse, handlers, formatting)
├── tests/                     ← pytest (~590 tests)
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
make check      # lint + typecheck + test (lint auto-fixes)
make ci         # what CI runs — same gates, but fails instead of auto-fixing
```

`make check` runs `ruff check --fix`, so a lint violation is silently repaired
rather than reported. `make ci` is the non-mutating form (`ruff check`,
`ruff format --check`) and is what `.github/workflows/ci.yml` executes on every
PR across Python 3.10–3.13. Run `make ci` before pushing if you want the same
answer CI will give.

## Key Details

- **Entry point:** `popcorn = "popcorn_cli.cli:main"`
- **Dependencies:** `httpx` (HTTP client), `pyjwt` (JWT decode)
- **Build system:** hatchling
- **Python:** >=3.10
- **Version:** runtime via `importlib.metadata` — update only in `pyproject.toml`

## Command architecture

New command families go in `src/popcorn_cli/commands/<name>.py` and are declared
once via `src/popcorn_cli/registry.py` — which derives argparse, dispatch, both
shell completions, `popcorn commands --json`, and the fuzzy-match list. Do **not**
add a family by hand-editing the completion generators or the schema builder.
Families still living in `cli.py` are mid-migration; see
`docs/architecture-commands.md`.

## Channel templates

`docs/TEMPLATE_AUTHORING.md` is the guide for authoring a channel-template
bundle with this CLI. Its §2 is the thing to keep straight: a **new** app type
is a backend PR into `CHANNEL_TEMPLATES` plus a deploy, but **editing** one is
a pure CLI loop (`app fork` → `checkout` → `publish`) with no deploy in it.
`popcorn flow import` is gone and neither path replaces it.

Two complete, live-verified bundles back the guide:

| Bundle | Point |
|---|---|
| `examples/alerttracker/` | four producers; must *derive* severity/env, so one LLM call per delivery. `GOTCHAS.md` is the evidence log behind most of the guide |
| `examples/deploywatch/` | one producer (GitHub `deployment_status`); *extracts* every field by path, zero LLM calls. The worked `fields.extract` example |

The contrast between them is the guide's §6 and is deliberate — don't collapse
them into one bundle or make either multi-producer.

The guide deliberately does **not** restate activity names, arguments, or
result schemas — those are served by `flow activities` and enforced by `flow
validate`, and duplicating them here would rot. Keep it that way when editing.

### `popcorn template check`

`src/popcorn_core/template_check.py` is an **offline** structural checker
(`popcorn template check <dir>`); the command lives in
`src/popcorn_cli/commands/template.py`. It exists because nothing in CI parsed
the shipped examples, and a broken `alert_tick` lived in one for a day.

It is scoped to what a server cannot tell you or will accept in silence:
cross-file agreement, the importer's flattening contract, undeclared column
writes, `output_schema` properties that are dereferenced but not `required`.
**It is not a second validator** — never teach it activity names or argument
schemas, which are the server's to own and would rot here exactly as the guide
says.

The line is *catalog vs grammar*. It must know the DSL's shape — a step is one
of `activity`/`sleep_seconds`/`await_approval`/`steps`, a block's inner ids are
private, `$trigger` has seven keys, `collect:` publishes a second name — because
without that it cannot tell a reference from a typo. It must not know what
`foundation.store.upsert_rows` takes.

**Where it will not follow: `when:`.** Four rails, routed legacy-first (see the
guide's §4). Mirroring that offline means reimplementing the predicate parser,
so the checker validates the references inside a `when:` and asserts nothing
about its grammar. A near-miss reimplementation is worse than no check: the
"exactly one comparison" rule it used to enforce rejected 55 valid clauses.

Finding `code` values are a stable contract (CI and agents branch on them);
renaming one is a minor version bump.

Three test layers, and the gap at the bottom is deliberate:

| | Runs | Guards |
|---|---|---|
| `tests/test_example_bundles.py` | CI | every `examples/*/`, so a new example is gated the moment it is added |
| `tests/test_template_check.py` | CI | one grammar feature per test, derived from what real templates do |
| `tests/test_backend_templates.py` | **local only** | the five shipped `popcorn-backend` templates, read from the real checkout |

The last one skips without a backend checkout (`POPCORN_BACKEND_FLOWS`, or
`~/popcorn/backend/lib/temporal/flows`), so **it does not run in CI** — vendoring
copies would rot within a release. It exists because the checker shipped with
~180 false positives against those templates while passing everything in this
repo: `examples/` uses no block, no `collect:`, no expression-rail `when:`, no
`$trigger`, no `.md.j2` prompt. Run it after touching the checker.

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

**Not on PyPI, and there is no `uv publish` step.** Distribution is the repo
itself — users install and upgrade from
`git+https://github.com/PopcornAiHq/popcorn-cli.git` (see README), and the
CLI's own self-upgrade hardcodes that URL (`cli.py — _GITHUB_URL`). So there is
no artifact to push anywhere; a release is just a tag.

```bash
make bump             # or: make bump v=X.Y.Z — bumps, commits, AND tags
git push && git push --tags
```

Pushing a `v*` tag fires `.github/workflows/release.yml`, which builds notes
from the commits since the previous tag (dropping `chore:` lines) and creates
the GitHub release on its own. `make release` does the same thing by hand and
is only for when the workflow did not run — running both against one tag makes
the second fail, since the release already exists.

Because `make bump` tags as well as commits, run it on `main` after a merge,
never on a feature branch: a tag on a branch points at a commit that is about
to be squashed away. If a PR already carried the version bump, tag the merge
commit directly (`git tag vX.Y.Z && git push --tags`) rather than bumping
again.

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
