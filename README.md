# Popcorn CLI

CLI for the [Popcorn](https://popcorn.ai) API. Read channels, send messages, search conversations, and manage your workspace from the terminal.

## Install

```bash
# With uv (recommended)
uv tool install git+https://github.com/PopcornAiHq/popcorn-cli.git

# With pipx
pipx install git+https://github.com/PopcornAiHq/popcorn-cli.git

# With pip
pip install git+https://github.com/PopcornAiHq/popcorn-cli.git
```

## Quick Start

```bash
# Authenticate (opens browser)
popcorn auth login

# See who you are
popcorn whoami

# Read messages
popcorn read '#general'
popcorn read '#general' --thread <thread-id>

# Send a message
popcorn send '#general' "Hello from the CLI!"
echo "piped message" | popcorn send '#general'
popcorn send '#general' "see attached" --file ./screenshot.png

# Search
popcorn search channels
popcorn search messages "deployment"

# Notifications
popcorn inbox --unread

# Watch a channel live
popcorn watch '#general'
```

## Commands

| Command | Purpose |
|---------|---------|
| `popcorn auth login` | Browser OAuth login (`--with-token` for stdin, `--force` to re-auth) |
| `popcorn auth status` | Show auth state |
| `popcorn auth logout` | Clear tokens |
| `popcorn auth token` | Print token to stdout (for piping) |
| `popcorn env [name]` | Show or switch profile |
| `popcorn workspace list` | List workspaces |
| `popcorn workspace switch [name\|uuid]` | Switch active workspace |
| `popcorn whoami` | Current user + workspace |
| `popcorn search channels\|dms\|users [query]` | Search/list entities |
| `popcorn search messages <query>` | Full-text message search |
| `popcorn read <conv> [--thread ID] [--limit N]` | Message history |
| `popcorn info <conv>` | Conversation details + members |
| `popcorn send <conv> "msg" [--thread ID] [--file PATH]` | Post a message |
| `popcorn react <conv> <msg_id> <emoji> [--remove]` | Add/remove reaction |
| `popcorn edit <conv> <msg_id> "content"` | Edit a message |
| `popcorn delete <conv> <msg_id>` | Delete a message |
| `popcorn inbox [--unread\|--read] [--limit N]` | Notifications |
| `popcorn watch <conv> [--interval N]` | Live-tail a channel |
| `popcorn download <file_key> [-o PATH]` | Download a file |
| `popcorn create <name> [--type TYPE]` | Create a channel |
| `popcorn join <conv>` | Join a channel |
| `popcorn leave <conv>` | Leave a channel |
| `popcorn archive <conv> [--undo]` | Archive/unarchive a channel |
| `popcorn api <path> [-X METHOD] [-d DATA]` | Raw API call |
| `popcorn completion bash\|zsh` | Generate shell completions |

## Flags

- `--json` — Raw JSON output (for scripting)
- `-e` / `--env` — Profile name to use
- `--workspace <uuid>` — Override workspace
- `--no-color` — Disable color output

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

## License

MIT
