# Popcorn CLI — Error Handling, Resilience & New Commands

**Date:** 2026-03-12
**Scope:** 9 improvements to the Popcorn CLI covering error handling, deploy flow enhancements, new commands, and tarball ignore support.

---

## 1. Surface VM Errors from Publish

**Problem:** `deploy_publish` errors show generic `detail` field (e.g. "Publish failed on VM") instead of the actual VM error (e.g. "No changes to commit").

**Design:**
- In `cmd_pop`, catch `APIError` from `deploy_publish` specifically.
- `APIError.body` is `str | None`. Parse it with `json.loads(error.body)` guarded by try/except (body may be non-JSON or None).
- Look for nested VM error in parsed body: keys `error`, `vm_error`, `upstream_error`.
- Human mode: `"Publish failed: No changes to commit"` (first found nested error).
- JSON mode: `{"error": "Publish failed on VM", "vm_error": "No changes to commit", ...parsed body}`.
- If body is not JSON or no nested error found, fall back to current behavior (just the APIError message).

**Files:** `cli.py` (cmd_pop error handling).

---

## 2. Retry on 502

**Problem:** Transient 502s from the publish endpoint cause unnecessary failures.

**Design:**
- Retry logic lives in `cli.py` wrapping the `deploy_publish` call — not in `operations.py` (which is pure business logic, no I/O or logging).
- 3 retries with exponential backoff: 1s, 2s, 4s.
- Only retry when `APIError.status_code == 502`. All other errors pass through immediately.
- Human mode: log `"Retrying publish (attempt 2/3)..."` to stderr on each retry.
- JSON mode: retries are silent, only final result/error emitted.

**Implementation:** A `_publish_with_retry(client, conversation_id, s3_key, context, force, json_mode)` helper in `cli.py` that wraps the operations call with retry logic.

**Files:** `cli.py` (retry wrapper in cmd_pop flow).

---

## 3. Defensive Response Validation

**Problem:** API response shape changes cause raw `KeyError`/`TypeError` instead of clear errors.

**Design:**
- New module `popcorn_core/validation.py` with helper:
  ```python
  def extract(response: dict, *keys: str, label: str) -> Any
  ```
- Traverses nested keys: `extract(resp, "conversation", "id", label="deploy_create")`
- On missing key: raises `PopcornError(f"Unexpected response from {label}: missing '{key}'. Got: {truncated_response}")`.
- Uses `PopcornError` (not `APIError`) because the API returned 200 — this is a response shape mismatch, not an HTTP error.
- Applied to all deploy flow response accesses in `cli.py`: `deploy_create` → `["conversation"]["id"]`, `deploy_presign` → `["upload_url"]`, `["upload_fields"]`, `["s3_key"]`, `deploy_publish` → `["conversation_id"]`, `["site_name"]`.
- Also applied to high-risk nested accesses: `get_whoami` → `["user"]`, `["workspace"]`, `get_inbox` → `["activity"]["activities"]`.

**Files:** New `popcorn_core/validation.py`, updates to `cli.py`.

---

## 4. Dead Channel Detection

**Problem:** Stale `.popcorn.local.json` pointing to a deleted channel causes confusing errors during deploy.

**Design — injected into `cmd_pop` after reading `.popcorn.local.json` but before tarball creation:**

```
.popcorn.local.json exists with conversation_id?
  ├─ yes → GET /api/conversations/info?conversation_id={id}
  │         ├─ 200 + response["conversation"]["site"] is non-null → proceed normally
  │         ├─ 404 or response["conversation"]["site"] is null → stale config
  │         │    ├─ --force flag → auto-delete .popcorn.local.json, set conversation_id=None, proceed as fresh deploy
  │         │    ├─ interactive TTY → prompt: "Channel no longer exists. Create new? [Y/n]"
  │         │    │    ├─ Y → delete config, set conversation_id=None, proceed
  │         │    │    └─ n → abort
  │         │    └─ non-interactive (no TTY, piped) → abort with clear error
  │         └─ other error → surface it, abort
  └─ no → fresh deploy (current behavior)
```

- Uses query parameter style `?conversation_id={id}` matching existing API conventions (see `operations.py:208`).
- "Site provisioned" check: `response["conversation"]["site"]` is non-null.
- JSON mode: `{"error": "Stale channel configuration", "stale_config": true, "conversation_id": "..."}`
- New helper `_validate_channel(client, conversation_id)` in `cli.py` returning `True` (valid) or `False` (stale). Catches `APIError` with 404 status.

**Files:** `cli.py` (cmd_pop, new validation helper).

---

## 5. Name Collision with Random Suffix

**Problem:** 409 on `deploy_create` just errors out instead of helping the user.

**Design — restructures the existing 409 handling in `cmd_pop`:**

The current code catches `APIError` with 409 and re-raises as `PopcornError`. The new logic replaces this:

```
deploy_create("my-site") → 409 (caught as APIError, status_code=409)
  └─ loop up to 5 times:
      generate random 4-char lowercase suffix (e.g. "xkqm")
      deploy_create("my-site-xkqm")
        ├─ 200 → success, update site_name variable to "my-site-xkqm"
        └─ 409 → try next suffix
  └─ all 5 failed → error: "Could not find available name"
```

- Suffix: `"".join(random.choices(string.ascii_lowercase, k=4))`.
- On success: `site_name` is reassigned to the suffixed name so that `_write_local_json` and all downstream output use the correct name.
- Human mode: `"'my-site' is taken. Created as 'my-site-xkqm' instead."` printed to stderr before continuing.
- JSON mode success: include `"suggested_name": "my-site-xkqm"` in final response.
- JSON mode failure: `{"error": "Could not find available name", "attempted_names": ["my-site-xkqm", ...]}`

**Files:** `cli.py` (cmd_pop create logic — replaces existing 409 handler).

---

## 6. `popcorn status [channel]`

**Purpose:** Show site deployment status.

**Channel resolution:**
- If `channel` arg provided → resolve via normal channel resolution.
- If omitted → read `conversation_id` from `.popcorn.local.json`.
- If neither → error: `"No channel specified and no .popcorn.local.json found"`.

**API flow:**
```
POST /api/conversations/site-status {"conversation_id": id}
  ├─ 200 → display status
  └─ 404/error → fallback to GET /api/conversations/info?conversation_id={id}
```

Uses POST with query body (matching existing API conventions) rather than path-parameter GET.

**Fallback behavior:** When `/site-status` returns 404, fall back to `get_conversation_info`. Display available fields from the conversation object (name, type, created_at). For missing deployment fields, show `"—"` placeholders. Print a note: `"Detailed status not available (endpoint pending)"`.

**Human output:**
```
Site:      my-site
URL:       https://my-site.popcorn.ai
Version:   3
Commit:    abc1234
Deployed:  2026-03-12 14:30 UTC by user@example.com
```

Or in fallback mode:
```
Site:      my-site
URL:       —
Version:   —
Commit:    —
Deployed:  —
(Detailed status not available)
```

**JSON mode:** Pass through raw API response (either endpoint).

**New operations:** `get_site_status(client, conversation_id)` in `operations.py` — POSTs to `/api/conversations/site-status`, catches `APIError` with 404, falls back to `get_conversation_info`.

**Files:** `operations.py` (new function), `cli.py` (new command handler + parser registration).

---

## 7. `popcorn log [channel]`

**Purpose:** Show version history for a site.

**Channel resolution:** Same as status — arg or `.popcorn.local.json`.

**API flow:**
```
POST /api/conversations/site-log {"conversation_id": id, "limit": N}
  ├─ 200 → display version history
  └─ 404/error → "Version history not available yet"
```

Uses POST with body (matching existing API conventions).

**Human output:**
```
v3  abc1234  Update landing page    user@example.com  2026-03-12 14:30
v2  def5678  Fix typo in header     user@example.com  2026-03-11 10:15
v1  ghi9012  Initial deploy         user@example.com  2026-03-10 09:00
```

**Flags:** `--limit N` (default 10).

**New operations:** `get_site_log(client, conversation_id, limit)` in `operations.py`.

**Shared helper:** `_resolve_conversation_id(args, client)` in `cli.py` — reads `channel` arg or falls back to `.popcorn.local.json`. Used by both status and log commands.

**Files:** `operations.py` (new function), `cli.py` (new command handler + parser + shared helper).

---

## 8. `--force` Flag for Pop

**Purpose:** General "skip checks and prompts" flag for the pop command. Covers two behaviors:

1. **Publish:** passes `force: true` in the publish payload, which tells the VM to skip the "no changes to commit" check.
2. **Stale config (item 4 interaction):** auto-deletes stale `.popcorn.local.json` instead of prompting.

Both behaviors are related to "just push through regardless" — combining under one flag is intentional.

**Design:**
- Add `--force` / `-f` flag to `pop` subparser.
- `deploy_publish` in `operations.py` gets optional `force: bool = False` parameter, included in request body when `True`.
- `cmd_pop` passes `args.force` to both the channel validation helper and the publish call.

**Files:** `operations.py` (deploy_publish parameter), `cli.py` (pop subparser + passing force through).

---

## 9. `.popcornignore` Support

**Problem:** No way to exclude project-specific files from the deploy tarball.

**Design:**

**Always-excluded (hardcoded):**
- `.git/` — excluded by `git ls-files` in git mode; hardcoded in non-git mode
- `node_modules/` — excluded by `.gitignore` typically in git mode; hardcoded in non-git mode
- `.popcorn.local.json` — hardcoded in both modes (current behavior)
- `.popcornignore` — hardcoded in both modes (new)

**User exclusions:** `.popcornignore` in project root, parsed by `pathspec` library with `gitwildmatch` style.

**Flow:**
```
create_tarball(project_dir)
  ├─ Load .popcornignore if exists → pathspec.PathSpec
  ├─ git mode:
  │    git ls-files → file list (already respects .gitignore)
  │    → filter out hardcoded excludes (.popcorn.local.json, .popcornignore)
  │    → filter through .popcornignore pathspec (additive on top of .gitignore)
  └─ non-git mode:
       walk directory → exclude .git/, node_modules/ (hardcoded)
       → filter out .popcorn.local.json, .popcornignore
       → filter through .popcornignore pathspec
```

- New dependency: `pathspec` in `pyproject.toml`. Trade-off: adds ~50KB install weight but provides correct gitignore semantics (negation with `!`, `**/` recursive, etc.) that would be error-prone to reimplement.
- New helper `_load_ignore_patterns(root: Path) -> pathspec.PathSpec | None` in `archive.py`.
- Both code paths filter file lists through the same matcher.
- Patterns relative to project root (same as `.gitignore`).

**Edge cases:**
- Empty/missing `.popcornignore` → only hardcoded excludes apply (same as current behavior).
- Git mode: `.popcornignore` is additive on top of `.gitignore` — if `.gitignore` already excludes a file, `.popcornignore` won't re-include it.

**Files:** `archive.py` (ignore loading + filtering), `pyproject.toml` (new dep).

---

## Dependencies & New Files

| Item | New files | Modified files | New deps |
|------|-----------|----------------|----------|
| 1 | — | `cli.py` | — |
| 2 | — | `cli.py` | — |
| 3 | `popcorn_core/validation.py` | `cli.py` | — |
| 4 | — | `cli.py` | — |
| 5 | — | `cli.py` | — |
| 6 | — | `operations.py`, `cli.py` | — |
| 7 | — | `operations.py`, `cli.py` | — |
| 8 | — | `operations.py`, `cli.py` | — |
| 9 | — | `archive.py`, `pyproject.toml` | `pathspec` |

## Testing Strategy

- Items 1-3: Unit tests mocking API responses with missing/nested fields, 502 sequences, non-JSON error bodies.
- Items 4-5: Unit tests for stale config detection (404, null site, valid), 409 retry loop with random suffixes, site_name reassignment.
- Items 6-7: Unit tests for new operations + CLI handlers, fallback behavior on 404, shared resolution helper.
- Item 8: Unit test verifying `force` param passed through to publish payload, and `--force` auto-deleting stale config.
- Item 9: Unit tests with sample `.popcornignore` files, verify file exclusion in both git and non-git modes, hardcoded excludes always applied.
