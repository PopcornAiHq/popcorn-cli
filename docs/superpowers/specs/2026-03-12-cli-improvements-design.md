# Popcorn CLI — Error Handling, Resilience & New Commands

**Date:** 2026-03-12
**Scope:** 9 improvements to the Popcorn CLI covering error handling, deploy flow enhancements, new commands, and tarball ignore support.

---

## 1. Surface VM Errors from Publish

**Problem:** `deploy_publish` errors show generic `detail` field (e.g. "Publish failed on VM") instead of the actual VM error (e.g. "No changes to commit").

**Design:**
- In `cmd_pop`, catch `APIError` from `deploy_publish` specifically.
- Parse `error.body` for nested VM error: `body.error`, `body.vm_error`, `body.upstream_error`.
- Human mode: `"Publish failed: No changes to commit"`
- JSON mode: `{"error": "Publish failed on VM", "vm_error": "No changes to commit", ...full body}`
- If no nested error found, fall back to current behavior.

**Files:** `cli.py` (cmd_pop error handling), `errors.py` (expose body on APIError if not already).

---

## 2. Retry on 502

**Problem:** Transient 502s from the publish endpoint cause unnecessary failures.

**Design:**
- Add retry logic scoped to `deploy_publish` only — not global.
- 3 retries with exponential backoff: 1s, 2s, 4s.
- Only retry on HTTP 502. All other status codes pass through immediately.
- Human mode: log `"Retrying publish (attempt 2/3)..."` to stderr on each retry.
- JSON mode: retries are silent, only final result/error emitted.

**Implementation:** A `_retry_on_502` wrapper function in `operations.py` applied to the publish call. Avoids modifying `APIClient` globally.

**Files:** `operations.py` (retry wrapper around deploy_publish).

---

## 3. Defensive Response Validation

**Problem:** API response shape changes cause raw `KeyError`/`TypeError` instead of clear errors.

**Design:**
- New module `popcorn_core/validation.py` with helper:
  ```python
  def extract(response: dict, *keys: str, label: str) -> Any
  ```
- Traverses nested keys: `extract(resp, "conversation", "id", label="deploy_create")`
- On missing key: raises `APIError(f"Unexpected response from {label}: missing '{key}'. Got: {truncated_response}")`
- Applied to all deploy flow response accesses: `deploy_create`, `deploy_presign`, `deploy_publish`.
- Also applied to high-risk nested accesses: `get_whoami`, `get_inbox`.

**Files:** New `popcorn_core/validation.py`, updates to `operations.py` and `cli.py`.

---

## 4. Dead Channel Detection

**Problem:** Stale `.popcorn.local.json` pointing to a deleted channel causes confusing errors during deploy.

**Design — injected into `cmd_pop` before tarball creation:**

```
.popcorn.local.json exists with conversation_id?
  ├─ yes → GET /api/conversations/info/{id}
  │         ├─ 200 + site provisioned → proceed normally
  │         ├─ 404 or no site → stale config detected
  │         │    ├─ --force flag → auto-delete .popcorn.local.json, proceed as fresh deploy
  │         │    ├─ interactive TTY → prompt: "Channel no longer exists. Create new? [Y/n]"
  │         │    │    ├─ Y → delete config, proceed as fresh deploy
  │         │    │    └─ n → abort
  │         │    └─ non-interactive (no TTY, piped) → abort with clear error
  │         └─ other error → surface it, abort
  └─ no → fresh deploy (current behavior)
```

- JSON mode: `{"error": "Stale channel configuration", "stale_config": true, "conversation_id": "..."}`
- New helper `_validate_channel(client, conversation_id)` in `cli.py`.

**Files:** `cli.py` (cmd_pop, new validation helper).

---

## 5. Name Collision with Random Suffix

**Problem:** 409 on `deploy_create` just errors out instead of helping the user.

**Design — triggered when `deploy_create` returns 409:**

```
deploy_create("my-site") → 409
  └─ loop up to 5 times:
      generate random 4-char lowercase suffix (e.g. "xkqm")
      deploy_create("my-site-xkqm")
        ├─ 200 → success
        └─ 409 → try next suffix
  └─ all 5 failed → error: "Could not find available name"
```

- Suffix: `random.choices(string.ascii_lowercase, k=4)` joined.
- Human mode: `"'my-site' is taken. Created as 'my-site-xkqm' instead."`
- JSON mode success: include `"suggested_name": "my-site-xkqm"` in response.
- JSON mode failure: `{"error": "...", "attempted_names": [...]}`

**Files:** `cli.py` (cmd_pop create logic).

---

## 6. `popcorn status [channel]`

**Purpose:** Show site deployment status.

**Channel resolution:**
- If `channel` arg provided → resolve via normal channel resolution.
- If omitted → read `conversation_id` from `.popcorn.local.json`.
- If neither → error.

**API flow:**
```
GET /api/conversations/{id}/site/status
  ├─ 200 → display
  └─ 404/error → fallback to GET /api/conversations/info
```

**Human output:**
```
Site:      my-site
URL:       https://my-site.popcorn.ai
Version:   3
Commit:    abc1234
Deployed:  2026-03-12 14:30 UTC by user@example.com
```

**JSON mode:** Pass through raw API response.

**New operations:** `get_site_status(client, conversation_id)` — tries `/site/status`, catches 404, falls back to `get_conversation_info`.

**Files:** `operations.py` (new function), `cli.py` (new command handler + parser registration).

---

## 7. `popcorn log [channel]`

**Purpose:** Show version history for a site.

**Channel resolution:** Same as status — arg or `.popcorn.local.json`.

**API flow:**
```
GET /api/conversations/{id}/site/log?limit=N
  ├─ 200 → display
  └─ 404/error → "Version history not available yet"
```

**Human output:**
```
v3  abc1234  Update landing page    user@example.com  2026-03-12 14:30
v2  def5678  Fix typo in header     user@example.com  2026-03-11 10:15
v1  ghi9012  Initial deploy         user@example.com  2026-03-10 09:00
```

**Flags:** `--limit N` (default 10).

**New operations:** `get_site_log(client, conversation_id, limit)`.

**Shared helper:** `_resolve_conversation_id(args, client)` in `cli.py` — used by both status and log.

**Files:** `operations.py` (new function), `cli.py` (new command handler + parser + shared helper).

---

## 8. `--force` Flag for Pop

**Purpose:** Skip "no changes to commit" check on the VM.

**Design:**
- Add `--force` flag to `pop` subparser.
- `deploy_publish` in `operations.py` gets optional `force: bool = False`, included in request body when `True`.
- Also triggers auto-delete of stale config (interaction with item 4).

**Files:** `operations.py` (deploy_publish parameter), `cli.py` (pop subparser + passing force through).

---

## 9. `.popcornignore` Support

**Problem:** No way to exclude project-specific files from the deploy tarball.

**Design:**

**Always-excluded (hardcoded, no override):**
- `.git/`
- `node_modules/`
- `.popcorn.local.json`
- `.popcornignore`

**User exclusions:** `.popcornignore` in project root, parsed by `pathspec` with `gitwildmatch` style.

**Flow:**
```
create_tarball(project_dir)
  ├─ Load .popcornignore if exists → pathspec.PathSpec
  ├─ Combine with hardcoded excludes
  ├─ git mode: git ls-files output → filter through pathspec
  └─ non-git mode: walk directory → filter through pathspec
```

- New dependency: `pathspec` in `pyproject.toml`.
- New helper `_load_ignore_patterns(root: Path) -> pathspec.PathSpec` in `archive.py`.
- Both git and non-git code paths filter through the same PathSpec.
- Patterns relative to project root (same as `.gitignore`).

**Edge cases:**
- Empty/missing `.popcornignore` → only hardcoded excludes.
- Git mode: `.popcornignore` is additive on top of `.gitignore`.

**Files:** `archive.py` (ignore loading + filtering), `pyproject.toml` (new dep).

---

## Dependencies & New Files

| Item | New files | Modified files | New deps |
|------|-----------|----------------|----------|
| 1 | — | `cli.py`, `errors.py` | — |
| 2 | — | `operations.py` | — |
| 3 | `popcorn_core/validation.py` | `operations.py`, `cli.py` | — |
| 4 | — | `cli.py` | — |
| 5 | — | `cli.py` | — |
| 6 | — | `operations.py`, `cli.py` | — |
| 7 | — | `operations.py`, `cli.py` | — |
| 8 | — | `operations.py`, `cli.py` | — |
| 9 | — | `archive.py`, `pyproject.toml` | `pathspec` |

## Testing Strategy

- Items 1-3: Unit tests mocking API responses with missing/nested fields, 502 sequences.
- Items 4-5: Unit tests for stale config detection, 409 retry loop with random suffixes.
- Items 6-7: Unit tests for new operations + CLI handlers, fallback behavior on 404.
- Item 8: Unit test verifying `force` param passed through to publish payload.
- Item 9: Unit tests with sample `.popcornignore` files, verify file exclusion in both git and non-git modes.
