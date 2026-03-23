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

After publish returns, new phases:

```
Phase 8: Check publish response
  ├─ no verify_task_id → done (static or --skip-check)
  └─ has verify_task_id → enter poll loop

Phase 9: Poll loop
  Call deploy_verify_status() every 2 seconds
  Render progress:
    ⠋ Restarting site...
    ⠋ Checking health...
    ⠋ Fixing issues...
  Timeout: 5 minutes
  On timeout: warn, report last known status, continue to output

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
⚠ Fixed 2 issues before publishing:
  • server.js: added missing express import
  • package.json: added cors dependency
```

**Agent couldn't fully fix:**
```
Published to #my-site (v4) https://my-site.popcorn.site
⚠ 1 issue remains:
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
| Published, still unhealthy | 1 (EXIT_VALIDATION) |
| Poll timeout | 0 (warn, don't fail) |

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
  Assert: exit 1, error output

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
```

## Scope Boundary

This spec covers CLI changes only. Backend implementation (the async verify task runner, the verify-status endpoint, and the modified publish endpoint) is a separate effort in popcorn-backend. The CLI will degrade gracefully — if the backend doesn't return `verify_task_id`, the CLI behaves exactly as it does today.

## Graceful Degradation

If the backend hasn't been updated yet:
- `verify: true` in the publish payload is ignored (unknown fields are safe)
- No `verify_task_id` in response → CLI skips polling → today's behavior
- No code paths break

This allows the CLI to ship first, with the feature activating once the backend catches up.
