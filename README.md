# Popcorn CLI

CLI for the [Popcorn](https://popcorn.ai) API. Send messages, search projects, publish apps, and manage your workspace from the terminal.

> **Using this from an agent or script?** Set `POPCORN_AGENT=1` to enable agent-friendly defaults (`--json`, `--quiet`, `--no-color`, no upgrade prompts). Every command supports `--json`, which returns a stable envelope (`{"ok": true, "data": ...}`) with machine-readable `error_code` and semantic exit codes. Run `popcorn commands --json` to discover the full schema. For the formal contract, see [SPEC.md](./SPEC.md). Quick overview below.

## Install

```bash
# With uv (recommended)
uv tool install git+https://github.com/PopcornAiHq/popcorn-cli.git

# With pipx
pipx install git+https://github.com/PopcornAiHq/popcorn-cli.git

# With pip
pip install git+https://github.com/PopcornAiHq/popcorn-cli.git
```

## Update

```bash
popcorn upgrade
```

The CLI auto-detects how it was installed (uv, pipx) and runs the right upgrade command. It also checks for updates automatically every 5 minutes — if a new version is available, it upgrades and re-runs your command seamlessly.

To disable auto-update (e.g., in CI): `export POPCORN_NO_UPDATE_CHECK=1`

## Quick Start

```bash
# Authenticate (opens browser)
popcorn auth login

# See who you are
popcorn whoami


# Read messages
popcorn message list '#general'
popcorn message list '#general' --thread <thread-id>

# Send a message
popcorn message send '#general' "Hello from the CLI!"
echo "piped message" | popcorn message send '#general'
popcorn message send '#general' "see attached" --file ./screenshot.png

# Search messages — optionally scoped by project, author or time
popcorn message search "deployment"
popcorn message search "deployment" --in '#general' --since 2026-01-01
popcorn message search --from ana@example.com --in '#general'   # filters, no query

# List projects
popcorn project list

# Notifications
popcorn workspace inbox --unread

# Watch a project live
popcorn message list '#general' --watch
```

## Commands

Run `popcorn commands` for full JSON schema, or `popcorn help` for the help page.

A project is what the API calls a channel: one tracker, running one app.
Wherever the table below shows a project as a positional (`<project>`),
`--project <name-or-uuid>` does the same thing. That spelling is accepted by
every command that acts on a project, including the ones that take it only as a
flag, so it is the form to reach for when you would otherwise have to look up
which family this command belongs to.

The old spellings from before projects and apps were named that way —
`popcorn channel …`, `channel-config`, `channel templates`, `--channel`,
`--template` — still work and print a note naming the replacement; see
[SPEC.md](SPEC.md#renamed-spellings). DMs and group DMs are not projects:
`project list --dms` is deprecated and will be removed.

The same holds for the checkout directory: wherever the table shows
it as a positional (`[dir]`, `<dir>`), `--dir <path>` does the same thing. On
`popcorn app checkout` prefer the flag — a bare `--fork` cannot be told apart
from the directory positional, so `--fork mydir` names the *line* `mydir`.

| Command | Purpose |
|---------|---------|
| **Messages** | |
| `popcorn message send <project> "msg" [--thread ID] [--file PATH] [--batch] [--fail-fast]` | Send a message |
| `popcorn message list <project> [--thread ID] [--limit N] [--before ID] [--after ID]` | Read message history |
| `popcorn message threads <project> [--limit N] [--offset N]` | List threads with reply counts |
| `popcorn message get <msg_id>` | Get a single message by ID |
| `popcorn message edit <project> <msg_id> "content"` | Edit a message |
| `popcorn message delete <project> <msg_id>` | Delete a message |
| `popcorn message react <project> <msg_id> <emoji> [--remove]` | Add/remove reaction |
| `popcorn message search [query] [--in PROJECTS] [--from USERS] [--since T] [--until T] [--has WHAT] [--sort ORDER]` | Full-text message search. `--in` and `--from` accept comma-separated names or UUIDs; `--since`/`--until` take ISO 8601 times and `--has` takes `file,images,link,mention,video`. The query may be omitted when at least one filter other than `--sort` is given |
| `popcorn message download <file_key> [-o PATH]` | Download a file |
| **Projects** | |
| `popcorn project list [query] [--include-archived] [--include-hidden]` | List projects, following the server's cursor to the last page. Archived and hidden projects are excluded unless asked for. `--dms` lists DMs instead, and is deprecated |
| `popcorn project create <name> [--type TYPE] [--members IDS] [--app A] [--if-not-exists]` | Create a project. `--type` defaults to `workspace_channel` (everyone in the workspace is a member, now and as people join, so `--members` is ignored); `public_channel` is visible to anyone and joined on demand, `private_channel` holds only invited members. `--app` runs that app in it (the only way to install one at creation). `--if-not-exists` returns a project you are a member of that already has the name (`already_existed: true`), matched case-sensitively by the server |
| `popcorn project info <project>` | Project details + members |
| `popcorn project join <project>` | Join a project |
| `popcorn project leave <project>` | Leave a project |
| `popcorn project invite <project> <user_ids>` | Invite users to a project |
| `popcorn project kick <project> <user_id>` | Remove a user from a project |
| `popcorn project edit <project> [--name N] [--description D]` | Update project name or description |
| `popcorn project archive <project> [--undo]` | Archive/unarchive a project |
| `popcorn project delete <project>` | Delete a project |
| `popcorn project apps` | List the apps a project can be created with |
| **Flows** | |
| `popcorn flow activities [--tier T] [--status S] [--category C]` | List the DSL activity catalog |
| `popcorn flow validate <file\|dir> --project <project>` | Statically validate flow YAML without installing (exit 1 if any fail) |
| `popcorn flow list --project <project> [--limit N] [--offset N]` | List flows in a project |
| `popcorn flow get <flow_id> --project <project> [--no-triggers]` | Get a flow definition and what starts it on the project (schedules, webhooks, message triggers, document uploads, state edges, sibling flows) |
| `popcorn flow run <flow_id> --project <project> [--inputs JSON] [--wait] [--timeout-run N]` | Start a flow run (`--wait` polls until the server reports the run finished; non-zero exit unless it succeeded) |
| `popcorn flow runs list --project <project> [--status S] [--flow <name>] [--limit N] [--page-token T]` | List flow runs, each with its flow name; `--flow` narrows to one flow (pass it again with `--page-token`); older runs may be stamped with the flow's id instead of its name, and passing that id lists them |
| `popcorn flow runs get <workflow_id> --project <project> [--run-id R] [--include-errors]` | Get a flow run's detail (incl. the queue/tier it landed on, its inputs and the version it ran) |
| `popcorn flow runs timeline <workflow_id> --project <project> [--run-id R] [--before N] [--limit N]` | List a run's steps (activities, timers, signals) newest first, with outcome, duration and attempt; page with `--before` and `--run-id` from `pagination.next` |
| `popcorn flow runs cancel <workflow_id> --project <project> [--run-id R] [--force] [--reason S]` | Stop one run (cooperative cancel; `--force` terminates) |
| `popcorn flow runs cancel --flow <name> --project <project> [--force] [--page-token T]` | Stop every running run of a flow — the brake on a driver like `run_eval` whose launched runs outlive it |
| **Schedules** | |
| `popcorn schedule list --project <project>` | List a project's live scheduled flows, with cadence and next run |
| `popcorn schedule get <schedule> --project <project>` | One schedule's cadence, overlap policy, inputs and run counters (`<schedule>` is a slug, flow id or full schedule_id) |
| `popcorn schedule trigger <schedule> --project <project> [--overlap-policy P]` | Run a declared schedule now with its stored inputs; prints the run's workflow id to follow with `flow runs get` |
| **Apps** | |
| `popcorn app validate <dir> [--strict]` | Check an app before publishing: its structure offline, and, in a fork checkout while logged in, the server's publish checks — the manifest's tables among them — without publishing. Says when the server checks were skipped and why (exit 1 on errors; `--strict` also on warnings). `popcorn template check` is the old name and still works |
| **Tables** (project agent store) | |
| `popcorn table list --project <project>` | List tables in a project |
| `popcorn table schema <name> --project <project>` | Show a table's columns |
| `popcorn table rows <name> --project <project> [--filter JSON] [--limit N] [--cursor C]` | List rows in a table |
| `popcorn table row get <name> <record_id> --project <project>` | Get one row |
| `popcorn table row patch <name> <record_id> --project <project> --data JSON` | Patch one row's columns |
| `popcorn table row delete <name> <record_id> --project <project>` | Delete one row (confirms) |
| `popcorn table scalar list --project <project> [--limit N]` | List project scalars |
| `popcorn table scalar get <key> --project <project>` | Read one scalar |
| `popcorn table scalar set <key> <value> --project <project>` | Write one scalar |
| `popcorn table audit --project <project> [--entity-type T] [--entity-id ID] [--since ISO8601] [--limit N] [--cursor C]` | Recent agent-store audit entries. The filters are server-side, so `--entity-id` reads one row's whole history rather than the current page's |
| **Webhooks** | |
| `popcorn webhook create <project> <name> [--description D] [--action-mode MODE] [--trigger-flow-name F]` | Create a webhook. The flow binding is fixed here — `update` cannot re-point it |
| `popcorn webhook list <project>` | List webhooks (`--show-url` for the ingest URL, which carries a secret token) |
| `popcorn webhook get <webhook> [--project <project>] [--show-url]` | Show one webhook's settings (`<webhook>` is a UUID, or a name with `--project`) |
| `popcorn webhook update <webhook> [--name N] [--description D] [--action-mode MODE] [--activate\|--deactivate] [--enforce-hmac\|--no-enforce-hmac]` | Change a webhook's settings. `--enforce-hmac` only takes effect once the webhook has an HMAC secret |
| `popcorn webhook delete <webhook>` | Delete a webhook. Prompts; `--yes` to skip |
| `popcorn webhook override-rules get\|set <webhook> [rules]` | Per-event overrides; `set` replaces the whole set (`@-` reads stdin, `@path` a file) |
| `popcorn webhook deliveries <project> [--limit N] [--since ISO] [--status S]` | List webhook deliveries |
| `popcorn webhook event-types` | List valid webhook sources and action modes |
| `popcorn webhook send <target> [payload] [--project <project>]` | POST a payload to a webhook (target: ingest URL, webhook UUID, or name) |
| **Auth & identity** | |
| `popcorn auth login [--with-token] [--force] [--workspace NAME]` | Log in |
| `popcorn auth status` | Show auth state |
| `popcorn auth logout` | Clear tokens |
| `popcorn auth token` | Print token to stdout |
| `popcorn env [name]` | Show or switch profile |
| `popcorn workspace check-access <owner/repo>` | Check repo access |
| `popcorn workspace inbox [--unread\|--read] [--limit N]` | Notifications |
| `popcorn workspace list` | List workspaces |
| `popcorn workspace switch [name\|uuid]` | Switch active workspace |
| `popcorn workspace users [query]` | List workspace users |
| `popcorn whoami` | Current user + workspace |
| **Other** | |
| `popcorn api <path> [-X METHOD] [-d DATA] [--raw]` | Raw API call |
| `popcorn upgrade` | Upgrade to the latest version |
| `popcorn version [--check]` | Show version / check for updates |
| `popcorn commands` | Dump CLI schema as JSON |
| `popcorn doctor` | Diagnose local setup (auth, API, config, env) |
| `popcorn completion bash\|zsh` | Generate shell completions |

## Flags

| Flag | Purpose |
|------|---------|
| `--json` | JSON output (envelope: `{"ok": true, "data": ...}`) |
| `-q` / `--quiet` | Suppress informational stderr messages |
| `--timeout N` | HTTP request timeout in seconds (default: 30) |
| `-e` / `--env` | Profile name to use |
| `--workspace <name-or-uuid>` | Override workspace; at login, selects it instead of prompting |
| `--no-color` | Disable color output |
| `--debug` | Log HTTP requests/responses to stderr |
| `-y` / `--yes` | Auto-confirm prompts (also `POPCORN_ASSUME_YES=1`) |

## Agent / script usage

The CLI is designed to be driven by LLM agents and scripts as well as humans.

**Agent mode** — one-shot setup for scripted use:

```bash
export POPCORN_AGENT=1   # implies --json, --quiet, --no-color; suppresses auto-upgrade
```

**Stable JSON envelope** — every command accepts `--json`:

```bash
$ popcorn whoami --json
{"ok": true, "data": {"user": {...}, "workspace": {...}, "workspaces": [...]}}

$ popcorn project info '#nope' --json
{"ok": false, "error": "Project not found: #nope",
 "error_code": "not_found", "code": "PopcornError", "retryable": false}   # exit 1
```

- Success: `{"ok": true, "data": ...}` (data never contains a leaked top-level `ok`)
- Failure: `{"ok": false, "error": "...", "error_code": "...", "retryable": ...}` + non-zero exit
- `error_code` is the stable machine-readable code — branch on this, not `code` (class name)

**Exit codes** (agents can switch on these):

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Validation (bad input, invalid state) |
| `2` | Auth — re-login required |
| `3` | 4xx API error — request is wrong |
| `4` | 5xx API error — retryable with backoff |
| `5` | Ran fine, but what it checked is unhealthy |
| `130` | Interrupted (Ctrl+C) |

**Error codes** (stable `error_code` enum):

`validation` · `unauthorized` · `forbidden` · `not_found` · `conflict` · `rate_limited` · `client_error` · `server_error` · `network_error` · `unhealthy` · `internal`

**Discover everything programmatically** — no scraping `--help`:

```bash
popcorn commands --json   # full schema: exit_codes, error_codes, envelope shape,
                          # global flags, and every command's args/types
popcorn whoami --json     # user + workspace bootstrap
```

**Bulk operations** — stream NDJSON on stdin:

```bash
cat batch.ndjson | popcorn message send --batch --json
# each line: {"conversation": "#general", "message": "..."}
```

**Raw API access** — for endpoints not yet wrapped:

```bash
popcorn api /openapi.json --raw > schema.json
popcorn api /v1/... -X POST -d '{"...": "..."}' --json

# curl/gh-style body sources
echo '{"foo": 1}' | popcorn api /v1/... -X POST -d @-
popcorn api /v1/... -X POST -d @body.json
```

**Non-interactive confirmation** — for destructive prompts (e.g. export over
uncommitted changes), pass `--yes` / `-y` or set `POPCORN_ASSUME_YES=1`.
Without it, the CLI fails loudly in non-TTY mode instead of hanging.

**Pagination** — paginated commands include `data.pagination.next`:

```bash
$ popcorn message list '#general' --limit 25 --json | jq '.data.pagination'
{"next": {"before": "msg-abc-123"}}    # feed back: --before msg-abc-123
# or null when no more pages
```

**Streaming commands** — `--watch` emits NDJSON when combined with `--json`:
one complete envelope per line, newline-terminated, stdout-flushed. Pipe
directly into a line-oriented consumer.

```bash
popcorn message list '#general' --watch --json | while read line; do
  echo "$line" | jq -r '.data.content.parts[0].content // empty'
done
```

## Project References

Projects can be referenced by name (`#general`) or UUID. Names are cached for 5 minutes.

## Shell Completions

```bash
# Bash — add to ~/.bashrc
eval "$(popcorn completion bash)"

# Zsh — add to ~/.zshrc
eval "$(popcorn completion zsh)"
```

## Configuration

Tokens and workspace selection are stored in `~/.config/popcorn/auth.json` (permissions `0600`).

Custom API endpoints can be configured via environment variables:

```bash
POPCORN_API_URL=https://api.example.com popcorn auth login
POPCORN_CLERK_ISSUER=https://clerk.example.com popcorn auth login
POPCORN_CLERK_CLIENT_ID=your_client_id popcorn auth login
```

## License

MIT
