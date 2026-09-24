# Popcorn CLI

CLI for the [Popcorn](https://popcorn.ai) API. Send messages, search conversations, publish app bundles, and manage your workspace from the terminal.

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

# Search messages — optionally scoped by channel, author or time
popcorn message search "deployment"
popcorn message search "deployment" --in '#general' --since 2026-01-01
popcorn message search --from ana@example.com --in '#general'   # filters, no query

# List channels
popcorn channel list

# Notifications
popcorn workspace inbox --unread

# Watch a channel live
popcorn message list '#general' --watch
```

## Commands

Run `popcorn commands` for full JSON schema, or `popcorn help` for the help page.

Wherever the table below shows a channel as a positional (`<conv>`, `[channel]`),
`--channel <name-or-uuid>` does the same thing. That spelling is accepted by
every command that acts on a channel, including the ones that take it only as a
flag, so it is the form to reach for when you would otherwise have to look up
which family this command belongs to.

The same holds for the checkout or bundle directory: wherever the table shows
it as a positional (`[dir]`, `<dir>`), `--dir <path>` does the same thing. On
`popcorn app checkout` prefer the flag — a bare `--fork` cannot be told apart
from the directory positional, so `--fork mydir` names the *line* `mydir`.

| Command | Purpose |
|---------|---------|
| **Messages** | |
| `popcorn message send <conv> "msg" [--thread ID] [--file PATH] [--batch] [--fail-fast]` | Send a message |
| `popcorn message list <conv> [--thread ID] [--limit N] [--before ID] [--after ID]` | Read message history |
| `popcorn message threads <conv> [--limit N] [--offset N]` | List threads with reply counts |
| `popcorn message get <msg_id>` | Get a single message by ID |
| `popcorn message edit <conv> <msg_id> "content"` | Edit a message |
| `popcorn message delete <conv> <msg_id>` | Delete a message |
| `popcorn message react <conv> <msg_id> <emoji> [--remove]` | Add/remove reaction |
| `popcorn message search [query] [--in CHANNELS] [--from USERS] [--since T] [--until T] [--has WHAT] [--sort ORDER]` | Full-text message search. `--in` and `--from` accept comma-separated names or UUIDs; `--since`/`--until` take ISO 8601 times and `--has` takes `file,images,link,mention,video`. The query may be omitted when at least one filter other than `--sort` is given |
| `popcorn message download <file_key> [-o PATH]` | Download a file |
| **Channels** | |
| `popcorn channel list [query] [--dms] [--include-archived] [--include-hidden]` | List channels or DMs, following the server's cursor to the last page. Archived and hidden conversations are excluded unless asked for |
| `popcorn channel create <name> [--type TYPE] [--members IDS] [--template T] [--if-not-exists]` | Create a channel; `--template` installs a registry template into it (the only way to install one) |
| `popcorn channel info <conv>` | Channel details + members |
| `popcorn channel join <conv>` | Join a channel |
| `popcorn channel leave <conv>` | Leave a channel |
| `popcorn channel invite <conv> <user_ids>` | Invite users to a channel |
| `popcorn channel kick <conv> <user_id>` | Remove a user from a channel |
| `popcorn channel edit <conv> [--name N] [--description D]` | Update channel name or description |
| `popcorn channel archive <conv> [--undo]` | Archive/unarchive a channel |
| `popcorn channel delete <conv>` | Delete a channel |
| `popcorn channel templates` | List the channel templates the registry can install |
| **Flows** | |
| `popcorn flow activities [--tier T] [--status S] [--category C]` | List the DSL activity catalog |
| `popcorn flow validate <file\|dir> --channel <conv>` | Statically validate flow YAML without installing (exit 1 if any fail) |
| `popcorn flow list --channel <conv> [--limit N] [--offset N]` | List flows in a channel |
| `popcorn flow get <flow_id> --channel <conv> [--no-triggers]` | Get a flow definition and what starts it on the channel (schedules, webhooks, message triggers, document uploads, state edges, sibling flows) |
| `popcorn flow run <flow_id> --channel <conv> [--inputs JSON] [--wait] [--timeout-run N]` | Start a flow run (`--wait` polls until the server reports the run finished; non-zero exit unless it succeeded) |
| `popcorn flow runs list --channel <conv> [--status S] [--flow <name>] [--limit N] [--page-token T]` | List flow runs, each with its flow name; `--flow` narrows to one flow (pass it again with `--page-token`); older runs may be stamped with the flow's id instead of its name, and passing that id lists them |
| `popcorn flow runs get <workflow_id> --channel <conv> [--run-id R] [--include-errors]` | Get a flow run's detail (incl. the queue/tier it landed on) |
| `popcorn flow runs cancel <workflow_id> --channel <conv> [--run-id R] [--force] [--reason S]` | Stop one run (cooperative cancel; `--force` terminates) |
| `popcorn flow runs cancel --flow <name> --channel <conv> [--force] [--page-token T]` | Stop every running run of a flow — the brake on a driver like `run_eval` whose launched runs outlive it |
| **Templates** | |
| `popcorn template check <dir> [--strict]` | Check a template bundle's structure offline — no channel, no server (exit 1 on errors; `--strict` also on warnings) |
| **Tables** (channel data-store) | |
| `popcorn table list --channel <conv>` | List tables in a channel |
| `popcorn table schema <name> --channel <conv>` | Show a table's columns |
| `popcorn table rows <name> --channel <conv> [--filter JSON] [--limit N] [--cursor C]` | List rows in a table |
| `popcorn table row get <name> <record_id> --channel <conv>` | Get one row |
| `popcorn table row patch <name> <record_id> --channel <conv> --data JSON` | Patch one row's columns |
| `popcorn table row delete <name> <record_id> --channel <conv>` | Delete one row (confirms) |
| `popcorn table scalar list --channel <conv> [--limit N]` | List channel scalars |
| `popcorn table scalar get <key> --channel <conv>` | Read one scalar |
| `popcorn table scalar set <key> <value> --channel <conv>` | Write one scalar |
| `popcorn table audit --channel <conv> [--entity-type T] [--entity-id ID] [--since ISO8601] [--limit N] [--cursor C]` | Recent data-store audit entries. The filters are server-side, so `--entity-id` reads one row's whole history rather than the current page's |
| **Webhooks** | |
| `popcorn webhook create <conv> <name> [--description D] [--action-mode MODE] [--trigger-flow-name F]` | Create a webhook. The flow binding is fixed here — `update` cannot re-point it |
| `popcorn webhook list <conv>` | List webhooks (`--show-url` for the ingest URL, which carries a secret token) |
| `popcorn webhook get <webhook> [--channel <conv>] [--show-url]` | Show one webhook's settings (`<webhook>` is a UUID, or a name with `--channel`) |
| `popcorn webhook update <webhook> [--name N] [--description D] [--action-mode MODE] [--activate\|--deactivate] [--enforce-hmac\|--no-enforce-hmac]` | Change a webhook's settings. `--enforce-hmac` only takes effect once the webhook has an HMAC secret |
| `popcorn webhook delete <webhook>` | Delete a webhook. Prompts; `--yes` to skip |
| `popcorn webhook override-rules get\|set <webhook> [rules]` | Per-event overrides; `set` replaces the whole set (`@-` reads stdin, `@path` a file) |
| `popcorn webhook deliveries <conv> [--limit N] [--since ISO] [--status S]` | List webhook deliveries |
| `popcorn webhook event-types` | List valid webhook sources and action modes |
| `popcorn webhook send <target> [payload] [--channel <conv>]` | POST a payload to a webhook (target: ingest URL, webhook UUID, or name) |
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

$ popcorn channel info '#nope' --json
{"ok": false, "error": "Channel not found: #nope",
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

## Conversation References

Channels can be referenced by name (`#general`) or UUID. Names are cached for 5 minutes.

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

### Proxy Mode (VM Sidecar)

For deployments behind a local proxy/sidecar that handles authentication:

```bash
export POPCORN_PROXY_MODE=1
export POPCORN_API_URL=http://sidecar:8091/popcorn
export POPCORN_WORKSPACE_ID=ws-xxxx
export POPCORN_USER_ID=user-xxxx
```

In proxy mode the CLI skips auth (no browser login or token refresh) and sends `X-Actor-User-ID` / `X-Workspace-ID` headers instead of `Authorization`.

## License

MIT
