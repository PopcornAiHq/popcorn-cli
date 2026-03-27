# VM Commands — Design Spec

## Overview

Add a `popcorn vm` command group to the CLI for inspecting and managing workspace VM agent execution. Primary use case: local Claude Code analyzing VM agent traces to provide feedback on prompts, workflows, and tool usage.

## Commands

### `popcorn vm trace <channel> [item_id]`

Full execution trace for an agent work item.

**Arguments:**
- `channel` (required) — channel name (`#my-channel` or `my-channel`)
- `item_id` (optional) — specific item ID. If omitted, fetches the most recent item.

**Flags:**
- `--list` — show recent items instead of a full trace
- `--watch` — tail a live in-progress trace, printing new events as they arrive
- `--status <status>` — filter by status (`complete`, `failed`, `processing`, `pending`)
- `--raw` — output full JSON
- `--limit N` — number of items for `--list` (default 10)

**Data flow:**
1. Use channel name as `queue_id` directly (VM queues are named after channels)
2. If no `item_id`: call `GET /api/appchannels/usage?workspace_id=...` → find latest item for that queue
3. Call `GET /api/appchannels/trace/{queue_id}/{item_id}?workspace_id=...`

**Watch mode:**
- Poll trace endpoint every 3 seconds
- Track `len(events)` — print only new events since last poll
- Show running header with turn count, elapsed time, current tool
- Exit when item status changes from `processing` to `complete`/`failed`
- Ctrl+C exits cleanly

**Human output (full trace):**

```
Trace: build landing page hero section
Queue: my-channel  |  Status: complete  |  Model: claude-sonnet-4-20250514
Duration: 2m 34s  |  Cost: $0.0847

Prompt:
  Build a hero section with a gradient background and CTA button

Tool calls (14 turns):
  1. Read    sites/my-channel/index.html                    3s
  2. Edit    sites/my-channel/index.html                    2s
  3. Bash    npm run build                                 12s
  ...

Files written:
  sites/my-channel/index.html
  sites/my-channel/styles.css

Result:
  Built hero section with gradient background (#1a1a2e → #16213e)...

Tokens: 45.2k in / 3.1k out / 128.4k cache read  |  Cache hit: 72.3%
```

**Human output (--list):**

```
Recent items for my-channel:

  ID         Name                          Status     Cost      Duration   Completed
  abc123     build landing page hero        complete   $0.08     2m 34s     2m ago
  def456     fix mobile responsive layout   complete   $0.12     4m 12s     18m ago
  ghi789     add dark mode toggle           failed     $0.03     0m 45s     1h ago
```

**Human output (--watch, live tail):**

```
Watching: build landing page hero  (processing, turn 4, $0.03, 1m 12s)

  14:23:01  Read     sites/my-channel/index.html         3s
  14:23:04  Edit     sites/my-channel/index.html         2s
  14:23:18  Bash     npm run build                      12s
  14:23:20  Read     sites/my-channel/styles.css         ...
```

### `popcorn vm monitor`

Snapshot of active workers and queue items across all channels.

**Flags:**
- `--watch` — poll and redraw (clear screen between updates)
- `-n <seconds>` — poll interval for watch mode (default 5)
- `--raw` — output full JSON

**Data flow:**
- `GET /api/appchannels/monitor?workspace_id=...`

**Human output:**

```
Workers:
  my-channel      pid 1234   up 12m   build hero section [Edit]
  other-channel   pid 5678   up 3m    idle

Active items:
  my-channel/abc123   build hero section   turn 8   $0.06   2m 12s
  other-channel       (idle)

Total cost: $0.06
```

Watch mode clears terminal and redraws each interval.

### `popcorn vm usage`

Token and cost analytics.

**Flags:**
- `--hours N` — filter to last N hours
- `--days N` — filter to last N days
- `--queue <channel>` — filter by channel name
- `--limit N` — number of recent items (default 20)
- `--raw` — output full JSON

**Data flow:**
- `GET /api/appchannels/usage?workspace_id=...&hours=...&days=...&queue=...`

**Human output:**

```
Usage (last 24h):
  Tasks: 47  |  Total cost: $3.82

  By channel:
    my-channel       23 tasks   $2.14
    other-channel    18 tasks   $1.42
    experiments       6 tasks   $0.26

  By model:
    claude-sonnet    41 tasks   $2.90
    claude-haiku      6 tasks   $0.92

  Tokens: 1.2M in / 89k out / 4.1M cache read
  Cache hit rate: 72.3%  |  Cache savings: $1.24
```

### `popcorn vm cancel <channel>`

Cancel the currently processing work item in a channel.

**Arguments:**
- `channel` (required) — channel name

**Flags:**
- `--item <item_id>` — cancel a specific item (otherwise cancels current processing item)

**Data flow:**
1. `GET /api/appchannels/monitor?workspace_id=...` → find processing item for queue
2. `POST /api/appchannels/queues/{queue_id}/items/{item_id}/cancel?workspace_id=...`

**Human output:**

```
Cancelled: build landing page hero (abc123) in my-channel
```

If no processing item found:
```
No active task in my-channel
```

### `popcorn vm rollback <channel>`

Roll back a site to its previous version.

**Arguments:**
- `channel` (required) — channel/site name

**Flags:**
- `--version N` — roll back to a specific version number (default: previous version)

**Data flow:**
- `POST /api/appchannels/sites/{name}/rollback?workspace_id=...` with body `{"version": N}`

**Human output:**

```
Rolled back my-channel to v3 (was v4)
```

## Architecture

### Code organization

No new files. New functions added to existing modules:

**`popcorn_core/operations.py`** — business logic:
- `vm_trace(client, queue_id, item_id)` — fetch full trace
- `vm_trace_list(client, queue_id, ...)` — recent items for a queue (from usage endpoint)
- `vm_monitor(client)` — fetch monitor snapshot
- `vm_usage(client, ...)` — fetch usage analytics
- `vm_cancel(client, queue_id, item_id)` — cancel item
- `vm_rollback(client, site_name, version)` — rollback site

**`popcorn_cli/cli.py`** — command handlers + argparse:
- `cmd_vm_trace()`, `cmd_vm_monitor()`, `cmd_vm_usage()`, `cmd_vm_cancel()`, `cmd_vm_rollback()`
- `vm` subparser group with nested subparsers

**`popcorn_cli/formatting.py`** — display helpers:
- `format_trace()`, `format_trace_list()`, `format_monitor()`, `format_usage()`
- `format_duration()`, `format_tokens()`, `format_cost()`
- `format_trace_event()` — single event line for watch mode

### Channel name resolution

Channel names are used directly as queue_id / site name (no UUID resolution needed for VM endpoints). Strip leading `#` if present.

This is different from the main CLI commands which resolve `#name` → conversation UUID. The VM endpoints use names, not UUIDs.

### API endpoints used

All go through the backend proxy with JWT auth + `workspace_id` query param:

| CLI command | HTTP call |
|-------------|-----------|
| `vm trace` | `GET /api/appchannels/trace/{queue_id}/{item_id}` |
| `vm trace --list` | `GET /api/appchannels/usage` (filtered by queue) |
| `vm monitor` | `GET /api/appchannels/monitor` |
| `vm usage` | `GET /api/appchannels/usage` |
| `vm cancel` | `GET /api/appchannels/monitor` + `POST /api/appchannels/queues/{q}/items/{i}/cancel` |
| `vm rollback` | `POST /api/appchannels/sites/{name}/rollback` |

### Watch mode pattern

Both `vm trace --watch` and `vm monitor --watch` use the same loop pattern:

```python
while True:
    data = fetch()
    render(data, previous_data)
    previous_data = data
    time.sleep(interval)
```

- `vm monitor --watch`: clear screen + full redraw each cycle
- `vm trace --watch`: append-only (print only new events), update header line in-place
- Both catch `KeyboardInterrupt` for clean exit

### --raw flag

All commands check `args.raw` and output `json.dumps(data, indent=2)` for the full API response. Consistent with `popcorn api --raw`.

## Out of scope

- Queue management (start/stop workers, create queues)
- Agent interaction (steer, clarify, permission)
- Site creation/forking/promoting (covered by `popcorn pop`)
- VM admin operations (restart ECS, manage secrets)
- Backend changes — CLI-only, all endpoints already exist
