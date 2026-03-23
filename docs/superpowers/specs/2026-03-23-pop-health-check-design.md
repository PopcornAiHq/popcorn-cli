# Pop Health Check & Auto-Fix

**Date:** 2026-03-23
**Status:** Draft
**Scope:** CLI changes in popcorn-cli, backend contract assumptions

## Problem

`popcorn pop` uploads and commits files without verifying the site works. A broken `server.js`, missing dependency, or failed build results in a published version that immediately breaks the live site. The `/update` command has full agent verification (restart, browser check, user flow testing), but `pop` has none.

## Solution

Add automatic health verification to `pop` for dynamic and build sites. When the site is unhealthy after deploy, dispatch the VM agent to fix it before finalizing. Always publish (even if unfixable), and clearly report what happened.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Auto-fix or report-only? | Auto-fix | Pop should publish a working version, not a broken one |
| If agent can't fix? | Publish anyway | Blocking feels worse; user can see what's broken and iterate |
| Default behavior? | Smart: check dynamic/build, skip static | Static sites can't break in detectable ways; avoid wasted time |
| Opt-out? | `--skip-check` flag | Escape hatch for speed when user knows it's fine |
| Where does logic live? | Backend (async task) + CLI (poll) | Backend already has restart, health check, and agent infrastructure |

## Backend Contract

### Publish Endpoint (Modified)

```
POST /api/conversations/publish
Body: {conversation_id, s3_key, context, force, verify: true}

Response (when verify=true and site is dynamic/build):
{
  "conversation_id": "...",
  "site_name": "...",
  "version": 3,
  "commit_hash": "abc1234",
  "verify_task_id": "uuid"
}

Response (when verify=true and site is static):
{
  "conversation_id": "...",
  "site_name": "...",
  "version": 3,
  "commit_hash": "abc1234",
  "verify": {"skipped": true, "reason": "static"}
}

Response (when verify is false/absent):
  (unchanged from today)
```

### Verify Status Endpoint (New)

```
GET /api/conversations/{conversation_id}/verify-status?task_id={verify_task_id}

Response:
{
  "status": "restarting" | "checking" | "fixing" | "done",
  "healthy": true | false | null,
  "site_type": "node" | "python" | "build" | "static",
  "fixes": [
    {"file": "server.js", "description": "added missing express import"},
    {"file": "package.json", "description": "added cors dependency"}
  ],
  "errors": ["server crashes on startup: Cannot find module 'foo'"],
  "version": 4,
  "commit_hash": "def5678"
}
```

**`healthy` field values:**
- `null` — task is still in progress (status is not "done")
- `true` — site is healthy (passed checks, or agent fixed it)
- `false` — site is unhealthy (agent tried but couldn't fully fix)

### Backend Async Verify Flow (VM-side)

```
Phase 1 — Restart (status: "restarting")
  restart_site(): install deps, build, start process

Phase 2 — Health Check (status: "checking")
  Dynamic server: curl http://127.0.0.1:{port}/
  Build-only: verify dist/index.html exists

Phase 3 — Fix (status: "fixing", only if unhealthy)
  Dispatch work item to agent queue
  Agent uses existing verification loop:
    restart, browser_goto, test user flows, read logs, fix, repeat

Phase 4 — Done (status: "done")
  healthy: true/false
  fixes: list of what agent changed
  errors: list of remaining issues (if any)
  version/commit_hash: final version (may be N+1 if agent committed fixes)
```

## CLI Changes

### Modified: `deploy_publish()` in `operations.py`

Add `verify` parameter. When true, include `"verify": True` in request payload.

```python
def deploy_publish(client, conversation_id, s3_key, context="", force=False, verify=False):
    data = {"conversation_id": conversation_id, "s3_key": s3_key}
    if context:
        data["context"] = context
    if force:
        data["force"] = True
    if verify:
        data["verify"] = True
    return client.post("/api/conversations/publish", data=data)
```

### Modified: `_publish_with_retry()` in `cli.py`

Thread `verify` parameter through to `deploy_publish()`. The retry wrapper at `cli.py:1000` calls `deploy_publish()` and must forward the new parameter.

```python
def _publish_with_retry(client, conversation_id, s3_key, context, force, json_mode, verify=False):
    # ... existing retry logic ...
    return operations.deploy_publish(client, conversation_id, s3_key, context, force=force, verify=verify)
```

### New: `deploy_verify_status()` in `operations.py`

```python
def deploy_verify_status(client, conversation_id, task_id):
    return client.get(
        f"/api/conversations/{conversation_id}/verify-status",
        params={"task_id": task_id},
    )
```

### Modified: `cmd_pop()` in `cli.py`

New argument:

```python
pop_p.add_argument("--skip-check", action="store_true", help="Skip health verification")
```

**`--skip-check` behavior:** When set, `verify` is not included in the publish payload. Without `--skip-check`, `verify: true` is always sent — the backend decides whether to skip (e.g., for static sites) and communicates this via the response shape.

After publish returns, new phases:

```
Phase 8: Check publish response
  ├─ no verify_task_id → done (static or --skip-check)
  └─ has verify_task_id → enter poll loop

Phase 9: Poll loop
  Call deploy_verify_status() every 2 seconds
  Render progress (always shown, not gated by --verbose):
    ⠋ Restarting site...
    ⠋ Checking health...
    ⠋ Fixing issues...
  Use _status() for progress (writes to stderr, respects --no-color)
  Timeout: 5 minutes (poll interval increases to 5s during "fixing" status)
  On timeout: warn, report last known status, continue to output
  On Ctrl+C: print "Published to #site (vN) — health check cancelled", exit 0

  Version display fallback:
    Use final version from verify-status response when available.
    On timeout, poll error, or Ctrl+C: fall back to version from original publish response.

  Poll error handling:
    Transient errors (network, 5xx): retry silently, count toward timeout
    404: treat as "backend doesn't support verify", stop polling, output normally
    Persistent errors (3+ consecutive): warn and stop polling, output normally

Phase 10: Report result (replaces current output phase)
```

### CLI Output

**Healthy (no issues):**
```
Published to #my-site (v3) https://my-site.popcorn.site
```

**Agent fixed issues:**
```
Published to #my-site (v4) https://my-site.popcorn.site
⚠ Fixed 2 issues (v3 → v4):
  • server.js: added missing express import
  • package.json: added cors dependency
```

**Agent couldn't fully fix:**
```
Published to #my-site (v4) https://my-site.popcorn.site
⚠ 1 issue remains after auto-fix (v3 → v4):
  • server crashes on startup: Cannot find module 'foo'
```

**Static site (no check needed):**
```
Published to #my-site (v3) https://my-site.popcorn.site
```

**--skip-check:**
```
Published to #my-site (v3) https://my-site.popcorn.site
```

**--json mode:**
```json
{
  "ok": true,
  "data": {
    "site_name": "my-site",
    "version": 4,
    "url": "https://my-site.popcorn.site",
    "verify": {
      "status": "done",
      "healthy": false,
      "site_type": "node",
      "fixes": [{"file": "server.js", "description": "added missing express import"}],
      "errors": ["server crashes on startup: Cannot find module 'foo'"]
    }
  }
}
```

### Exit Codes

| Scenario | Exit Code |
|----------|-----------|
| Published, healthy (or static/skipped) | 0 |
| Published, agent fixed it | 0 |
| Published, still unhealthy | 5 (EXIT_UNHEALTHY) |
| Poll timeout | 0 (warn, don't fail) |
| Poll error (backend doesn't support verify) | 0 (degrade gracefully) |

**New exit code:** `EXIT_UNHEALTHY = 5` in `errors.py`. This is distinct from `EXIT_VALIDATION` (bad input) and `EXIT_SERVER` (API failure). It signals: "the deploy succeeded but the site isn't working." CI scripts can distinguish between "bad arguments" (1), "auth failure" (2), "API error" (3/4), and "site unhealthy" (5).

**JSON note:** `"ok": true` is always set when the publish API call succeeded, even when `healthy: false`. The `ok` field reflects the API operation, not site health. Consumers should check `data.verify.healthy` for site status.

**JSON `verify` field contract:**
- `--skip-check` → `"verify"` key absent from `data`
- Static site → `"verify": {"skipped": true, "reason": "static"}`
- Verify completed → `"verify": {"status": "done", "healthy": bool, "site_type": "...", "fixes": [...], "errors": [...]}`
- Timeout/poll error → `"verify": {"status": "timeout", "healthy": null}` or `"verify": {"status": "error", "healthy": null}`

The `verify` dict is added to `output_data` before passing to `_output()`, so the existing `{"ok": true, "data": ...}` envelope handles it.

## Testing

### Unit Tests (CLI-side)

```
test_cmd_pop_verify_healthy
  Mock publish → {verify_task_id: "abc"}
  Mock verify-status → {status: "done", healthy: true}
  Assert: exit 0, no warning output

test_cmd_pop_verify_fixed
  Mock publish → {verify_task_id: "abc"}
  Mock verify-status progression: restarting → checking → fixing → done
  Final: {healthy: true, fixes: [{file: "server.js", description: "..."}]}
  Assert: exit 0, warning with fix list

test_cmd_pop_verify_still_broken
  Mock verify-status → {status: "done", healthy: false, errors: ["..."]}
  Assert: exit 5 (EXIT_UNHEALTHY), error output

test_cmd_pop_verify_timeout
  Mock verify-status → never reaches "done"
  Assert: exit 0, timeout warning

test_cmd_pop_skip_check
  Assert: verify not in publish payload
  Assert: no polling

test_cmd_pop_static_site
  Mock publish → {verify: {skipped: true, reason: "static"}}
  Assert: no polling, normal output

test_cmd_pop_verify_json_output
  Assert: JSON envelope includes verify block

test_cmd_pop_verify_progress_messages
  Assert: progress messages printed for each status transition

test_cmd_pop_verify_poll_transient_error
  Mock verify-status → 500, 500, then {status: "done", healthy: true}
  Assert: retries silently, exit 0

test_cmd_pop_verify_poll_404
  Mock verify-status → 404
  Assert: stops polling, outputs normally (graceful degradation), exit 0

test_cmd_pop_verify_poll_persistent_error
  Mock verify-status → 500 three times consecutively
  Assert: warns and stops polling, exit 0

test_cmd_pop_verify_version_display
  Mock publish → {version: 3, verify_task_id: "abc"}
  Mock verify-status → {status: "done", healthy: true, version: 4, fixes: [...]}
  Assert: output shows v4 (final version from verify, not original publish)
  Assert: fix message shows "v3 → v4"
```

## Scope Boundary

This spec covers CLI changes only. Backend implementation (the async verify task runner, the verify-status endpoint, and the modified publish endpoint) is a separate effort in popcorn-backend. The CLI will degrade gracefully — if the backend doesn't return `verify_task_id`, the CLI behaves exactly as it does today.

## Graceful Degradation

If the backend hasn't been updated yet:
- `verify: true` in the publish payload is ignored (unknown fields are safe)
- No `verify_task_id` in response → CLI skips polling → today's behavior
- No code paths break

This allows the CLI to ship first, with the feature activating once the backend catches up.
