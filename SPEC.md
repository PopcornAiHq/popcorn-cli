# Popcorn CLI — Agent Contract

This document specifies the stable, machine-oriented surface of the `popcorn` CLI. Everything here is the contract LLM agents and scripts can rely on. Anything **not** specified here is implementation detail and may change between releases.

> **Status:** Draft for 1.0.0. Shapes here are frozen at the 1.0.0 tag. See [§ Versioning](#versioning--stability-guarantees).

## Contents

- [Quick start for agents](#quick-start-for-agents)
- [Agent mode](#agent-mode)
- [Envelope](#envelope)
- [Error codes](#error-codes)
- [Exit codes](#exit-codes)
- [Pagination](#pagination)
- [Streaming (NDJSON)](#streaming-ndjson)
- [Schema discovery](#schema-discovery)
- [Authentication](#authentication)
- [Diagnostics](#diagnostics)
- [Raw API access](#raw-api-access)
- [Versioning & stability guarantees](#versioning--stability-guarantees)

---

## Quick start for agents

```bash
# 1. One-time setup for agent/script use
export POPCORN_AGENT=1

# 2. Bootstrap: confirm the environment works
popcorn doctor     # structured diagnostic; non-zero exit if unhealthy

# 3. Discover the command surface programmatically
popcorn commands   # full JSON schema: commands, flags, exit_codes, error_codes

# 4. Do work
popcorn whoami
popcorn message list '#general' --limit 25
```

**Every command accepts `--json`**, returns a stable envelope, and uses semantic exit codes. An agent does not need to parse human-readable CLI output.

---

## Agent mode

Setting `POPCORN_AGENT=1` implies the following defaults on every invocation, unless overridden:

| Implied | Effect |
|---|---|
| `--json` | JSON envelope output |
| `--quiet` / `-q` | Suppress informational stderr messages |
| `--no-color` | Disable ANSI escapes |
| `POPCORN_NO_UPDATE_CHECK=1` | Suppress auto-upgrade prompts |

Agent mode does **not** imply `--yes`. Destructive confirmations must be opted into explicitly via `--yes` / `-y` or `POPCORN_ASSUME_YES=1`. Without them, the CLI fails loudly in non-TTY contexts instead of hanging.

Accepts `1`, `true`, or `yes` (case-insensitive) as the enabling value.

---

## Envelope

Every command invoked with `--json` emits one of two shapes on stdout.

### Success

```json
{
  "ok": true,
  "data": "<command-specific payload>"
}
```

- `data` is the command's payload. Agents should never see a top-level `ok` *inside* `data` — the CLI strips any leaked upstream envelope so the outer `ok` is authoritative.
- Exit code: `0`.

### Error

```json
{
  "ok": false,
  "error": "<human-readable message>",
  "error_code": "<stable machine code — see § Error codes>",
  "code": "<Python exception class name — legacy, do not branch on>",
  "retryable": false
}
```

Additional fields that may appear on errors:

| Field | When | Type |
|---|---|---|
| `status` | API errors | integer HTTP status |
| `retry_after` | 429 or `Retry-After` header present | number of seconds |
| `hint` | CLI suggests a follow-up command | string |
| `request_id` | backend returned `x-request-id` | string |
| `body` | unparsed or parsed API response body | object/string |

**Always branch on `error_code`**, not `code`. `code` is the internal Python class name and is retained only for backward compatibility.

Exit code on error: non-zero; see [§ Exit codes](#exit-codes).

---

## Error codes

Stable enum. All values are lowercase `snake_case`. The set is frozen at 1.0.0; new codes may be added in minor releases but existing codes will not be removed or repurposed.

| `error_code` | Meaning | Typical cause |
|---|---|---|
| `validation` | Bad input, missing args, or invalid state | Malformed flag, wrong argument count, 422 from API |
| `unauthorized` | Not logged in or token expired | 401, expired JWT |
| `forbidden` | Authenticated but lacks permission | 403 |
| `not_found` | Resource does not exist | 404, unknown channel name |
| `conflict` | Conflicts with current state | 409, already-exists |
| `rate_limited` | Rate limited — honor `retry_after` | 429 |
| `client_error` | Other 4xx | |
| `server_error` | 5xx — retryable with backoff | |
| `network_error` | Transport failure (no HTTP response) | DNS, TLS, connection refused |
| `unhealthy` | Deploy succeeded but site is unhealthy | Post-deploy health check failed |
| `internal` | Unexpected internal CLI error | Bug; please report |

Machine-readable copy of this table is embedded in `popcorn commands --json` under `error_codes`.

---

## Exit codes

Semantic — agents can branch on these to decide retry vs bail without parsing output.

| Exit | Meaning | Typical action |
|---|---|---|
| `0` | Success | continue |
| `1` | Validation — bad input or invalid state | fix and retry |
| `2` | Auth — re-login required | run `popcorn auth login` |
| `3` | 4xx API error | request is wrong; do not retry |
| `4` | 5xx API error | retryable with backoff |
| `5` | Deploy succeeded but site is unhealthy | inspect; may self-heal |
| `130` | Interrupted (SIGINT / Ctrl+C) | stop |

Exit codes are also in `popcorn commands --json` under `exit_codes`.

---

## Pagination

Paginated commands include `data.pagination.next`. When there are more results, `next` is a dict of **CLI flag → value** pairs the agent feeds back to the same command to fetch the next page. When there are no more results, `next` is `null`.

```json
{
  "ok": true,
  "data": {
    "messages": [...],
    "has_more": true,
    "pagination": {
      "next": {"before": "019d8797-45fa-7015-bd1c-4694fc4cecb8"}
    }
  }
}
```

Agent loop pattern:

```bash
next_flags='{}'
while [ "$next_flags" != "null" ]; do
  resp=$(popcorn message list '#general' --limit 50 --json $(echo "$next_flags" | jq -r 'to_entries[] | "--\(.key) \(.value)"'))
  echo "$resp" | jq '.data.messages[]'
  next_flags=$(echo "$resp" | jq -c '.data.pagination.next // "null"')
done
```

Commands that emit `pagination.next` today: `message list`, `message search`, `message threads`, `workspace inbox`.

For commands where the backend does not return `has_more`, the CLI uses a safe heuristic: emit `next` when the returned page is at least `--limit` items long. Worst case the agent fetches one empty page and stops — the loop always converges.

---

## Streaming (NDJSON)

Streaming commands (`--watch`) emit **one envelope per line**, newline-terminated, stdout-flushed between writes. Each line is a self-contained `{"ok": true, "data": ...}` envelope — no prelude, no trailing summary.

```bash
popcorn message list '#general' --watch --json | while read line; do
  echo "$line" | jq -r '.data.content.parts[0].content // empty'
done
```

Commands that stream NDJSON: `message list --watch`.

The format ID is `ndjson`, surfaced in `popcorn commands --json` under `envelope.streaming.format`.

---

## Schema discovery

`popcorn commands --json` emits the full machine-readable CLI schema. Agents should read this instead of scraping `--help`.

```json
{
  "version": "1.0.0",
  "schema_version": 1,
  "envelope": {
    "success": {...},
    "error": {...},
    "notes": [...],
    "streaming": {...},
    "pagination": {...}
  },
  "exit_codes": {"ok": 0, "validation": 1, ...},
  "error_codes": [{"code": "validation", "description": "..."}, ...],
  "agent_mode": {"env_var": "POPCORN_AGENT", "description": "..."},
  "global_flags": [...],
  "commands": [
    {
      "name": "message",
      "category": "messages",
      "description": "...",
      "subcommands": [
        {"name": "list", "arguments": [...], "description": "..."},
        ...
      ]
    },
    ...
  ]
}
```

- `version` is the CLI version (semver).
- `schema_version` is the version of the *schema itself*; bumped only on breaking changes to the agent contract.
- `popcorn commands --json --groups=message,channel` filters to specific command groups.

---

## Authentication

### Browser flow (human)

```bash
popcorn auth login
```

Opens the browser; local callback server on port `28771` (hardcoded). Tokens stored at `~/.config/popcorn/auth.json` with `0600` permissions.

### Headless flow (agent)

```bash
echo "$POPCORN_TOKEN" | popcorn auth login --with-token
```

Token is provided via stdin to avoid shell-history exposure. Refresh is automatic on `401`; the CLI uses the stored refresh token without re-prompting.

### Proxy mode (VM sidecar)

```bash
export POPCORN_PROXY_MODE=1
export POPCORN_API_URL=http://sidecar:8091/popcorn
export POPCORN_WORKSPACE_ID=<ws-id>
export POPCORN_USER_ID=<user-id>
```

Skips auth entirely — no browser login, no token refresh. Sends `X-Actor-User-ID` and `X-Workspace-ID` instead of `Authorization`.

---

## Diagnostics

```bash
popcorn doctor --json
```

Returns a structured diagnostic report:

```json
{
  "status": "ok" | "issues",
  "version": "1.0.0",
  "python": "3.11.14",
  "platform": "darwin",
  "config": {"path": "~/.config/popcorn/auth.json", "exists": true, "permissions": "0600"},
  "profile": "prod",
  "auth": {"logged_in": true, "email": "...", "token_status": "valid", "expires_at": "..."},
  "workspace": {"id": "...", "name": "..."},
  "api": {"url": "https://api.popcorn.ai", "reachable": true, "latency_ms": 117, "status_code": 200},
  "env_vars": {"POPCORN_AGENT": "1", ...},
  "issues": []
}
```

`issues` is a list of human-readable strings. If `status == "ok"`, `issues` is empty. Agents debugging a user-reported problem should run `popcorn doctor --json` first — it answers most "why doesn't this work" questions without further probing.

---

## Raw API access

For endpoints not yet wrapped by a first-class command:

```bash
popcorn api /path                              # GET
popcorn api /path -X POST -d '{"foo": 1}'      # literal JSON body
popcorn api /path -X POST -d @-                # body from stdin
popcorn api /path -X POST -d @body.json        # body from file
popcorn api /openapi.json --raw                # raw response, no envelope
```

The `@-` and `@file` prefixes match `curl` and `gh api` conventions. Use `\@` to escape a literal `@` at the start of a JSON payload.

`--raw` bypasses the envelope; the endpoint's JSON is printed unmodified. Useful for bulk-exporting spec documents. Combine with `--json` for enveloped output.

---

## Versioning & stability guarantees

This CLI follows semantic versioning.

### Frozen at 1.0.0 (breaking-change triggers a major bump)

- Envelope shape: keys `ok`, `data`, `error`, `error_code`, `code`, `retryable`
- Stable `error_code` enum (members may be added, not removed or renamed)
- Exit code meanings
- Agent mode env var name: `POPCORN_AGENT`
- Pagination field name: `data.pagination.next` and its "flag → value" semantics
- NDJSON format for `--watch` streams
- `popcorn commands --json` top-level keys
- `POPCORN_ASSUME_YES` and `--yes` / `-y` semantics
- `popcorn doctor` top-level keys (`status`, `issues`, etc.)

### Not frozen (may change in minor releases)

- Specific payload contents inside `data` (new fields may be added; existing field names preserved)
- Wording of `error` messages (human-readable, not machine-parseable)
- Wording of `popcorn doctor` `issues` strings
- Human-readable (non-`--json`) output formatting
- Addition of new subcommands, flags, or error codes
- Addition of new top-level keys to `popcorn commands --json`

### Explicitly not covered

- Internal modules (`popcorn_core.*`) — private API, may change freely
- Python class names exposed in `code` field — implementation detail

### Schema versioning

The `schema_version` field in `popcorn commands --json` is bumped **only** when a change to the agent contract would break existing agents. CLI version bumps (`0.x`, `1.x`) are independent of schema version.
