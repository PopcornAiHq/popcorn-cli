# Popcorn CLI

CLI for the [Popcorn](https://popcorn.ai) API. Deploy sites, send messages, search conversations, and manage your workspace from the terminal.

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

# Deploy a site
popcorn site deploy

# Read messages
popcorn message list '#general'
popcorn message list '#general' --thread <thread-id>

# Send a message
popcorn message send '#general' "Hello from the CLI!"
echo "piped message" | popcorn message send '#general'
popcorn message send '#general' "see attached" --file ./screenshot.png

# Search messages
popcorn message search "deployment"

# List channels
popcorn channel list

# Notifications
popcorn workspace inbox --unread

# Watch a channel live
popcorn message list '#general' --watch
```

## Commands

Run `popcorn commands` for full JSON schema, or `popcorn help` for the help page.

| Command | Purpose |
|---------|---------|
| **Sites** | |
| `popcorn site deploy [NAME] [--context "..."] [--force] [--skip-check]` | Deploy site to a channel |
| `popcorn site export [channel] [--version V] [-o PATH] [--force] [--revert]` | Export site code from VM to local |
| `popcorn site status [channel]` | Show site deployment status |
| `popcorn site log [channel] [--limit N]` | Show site version history |
| `popcorn site trace <ch> [item] [--list] [--watch] [--raw]` | Show agent execution trace |
| `popcorn site cancel <ch> [--item ID]` | Cancel active agent task |
| `popcorn site rollback <ch> [--version N] [--raw]` | Roll back to previous version |
| **Messages** | |
| `popcorn message send <conv> "msg" [--thread ID] [--file PATH] [--batch] [--fail-fast]` | Send a message |
| `popcorn message list <conv> [--thread ID] [--limit N] [--before ID] [--after ID]` | Read message history |
| `popcorn message threads <conv> [--limit N] [--offset N]` | List threads with reply counts |
| `popcorn message get <msg_id>` | Get a single message by ID |
| `popcorn message edit <conv> <msg_id> "content"` | Edit a message |
| `popcorn message delete <conv> <msg_id>` | Delete a message |
| `popcorn message react <conv> <msg_id> <emoji> [--remove]` | Add/remove reaction |
| `popcorn message search <query>` | Full-text message search |
| `popcorn message download <file_key> [-o PATH]` | Download a file |
| **Channels** | |
| `popcorn channel list [query] [--dms]` | List channels or DMs |
| `popcorn channel create <name> [--type TYPE] [--members IDS] [--if-not-exists]` | Create a channel |
| `popcorn channel info <conv>` | Channel details + members |
| `popcorn channel join <conv>` | Join a channel |
| `popcorn channel leave <conv>` | Leave a channel |
| `popcorn channel invite <conv> <user_ids>` | Invite users to a channel |
| `popcorn channel kick <conv> <user_id>` | Remove a user from a channel |
| `popcorn channel edit <conv> [--name N] [--description D]` | Update channel name or description |
| `popcorn channel archive <conv> [--undo]` | Archive/unarchive a channel |
| `popcorn channel delete <conv>` | Delete a channel |
| **Webhooks** | |
| `popcorn webhook create <conv> <name> [--description D] [--action-mode MODE]` | Create a webhook |
| `popcorn webhook list <conv>` | List webhooks |
| `popcorn webhook deliveries <conv> [--limit N] [--since ISO] [--status S]` | List webhook deliveries |
| **VM** | |
| `popcorn vm monitor [--watch] [-n INTERVAL] [--raw]` | Show active workers and queue |
| `popcorn vm usage [--hours N] [--days N] [--queue NAME] [--raw]` | Token and cost analytics |
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
| `popcorn completion bash\|zsh` | Generate shell completions |

## Flags

| Flag | Purpose |
|------|---------|
| `--json` | JSON output (envelope: `{"ok": true, "data": ...}`) |
| `-q` / `--quiet` | Suppress informational stderr messages |
| `--timeout N` | HTTP request timeout in seconds (default: 30) |
| `-e` / `--env` | Profile name to use |
| `--workspace <uuid>` | Override workspace |
| `--no-color` | Disable color output |
| `--debug` | Log HTTP requests/responses to stderr |

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
