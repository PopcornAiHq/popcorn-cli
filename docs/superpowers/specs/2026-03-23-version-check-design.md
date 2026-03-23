# Version Check + Auto-Update

**Date:** 2026-03-23
**Status:** Draft

## Problem

Users don't know when a new version is available. They must manually check and upgrade.

## Solution

On every CLI invocation, check if a newer version exists (cached, 5min TTL). If outdated and installer is detectable, auto-upgrade and re-exec. Also add `popcorn version --check` for explicit checks.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Check frequency | Every invocation, cached 5min | Balances freshness with speed |
| Version source | raw.githubusercontent.com pyproject.toml | No tags/releases exist; pyproject.toml is source of truth |
| Display timing | Before command output | User sees update happening before their command |
| Auto-update? | Yes, when installer detectable | Seamless experience |
| After upgrade? | Re-exec via os.execvp | Run command on new version, no stale code |
| Fetch failure? | Silent, run command | Version check should never break the CLI |
| Explicit check | `popcorn version --check` | Bypasses cache, informational only (no auto-update) |

## Version Source

```
GET https://raw.githubusercontent.com/PopcornAiHq/popcorn-cli/main/pyproject.toml
```

Parse `version = "X.Y.Z"` from the response body. Timeout: 2 seconds.

Switch to PyPI or GitHub releases API later when available.

## Cache

**File:** `~/.config/popcorn/version-check.json` (same directory as auth.json)

```json
{"latest_version": "0.5.7", "checked_at": 1711234567.0}
```

- **TTL:** 300 seconds (5 minutes)
- **Explicit check** (`--check`): bypasses TTL, fetches fresh, updates cache
- **Write failures:** silent (e.g., read-only filesystem)

## Auto-Update Flow

```
main() entry point
  ↓
_check_and_update()
  ↓
Read cache
  ├─ fresh + up to date → return (no-op)
  ├─ fresh + outdated   → auto-upgrade
  └─ stale/missing      → fetch latest version (2s timeout)
                           ├─ fails → return (silent)
                           └─ succeeds → write cache
                                        ├─ up to date → return
                                        └─ outdated   → auto-upgrade
  ↓
Auto-upgrade:
  _detect_installer()
  ├─ None → return (can't auto-update, skip silently)
  └─ detected → print "Updating popcorn {old} → {new}..."
                run subprocess (reuse _UPGRADE_COMMANDS)
                ├─ fails → print warning, return (run command with old version)
                └─ succeeds → print "✓ Updated"
                              os.execvp("popcorn", ["popcorn"] + sys.argv[1:])
```

## Explicit Check Flow

```
popcorn version --check
  ↓
Fetch latest version (bypass cache, 2s timeout)
  ├─ fails → "popcorn {version} (could not check for updates)"
  └─ succeeds → write cache
                ├─ up to date → "popcorn {version} (up to date)"
                └─ outdated   → "popcorn {version} ({latest} available — run: popcorn upgrade)"
```

## Skip Conditions

Auto-update is skipped (silently) when:
- `popcorn upgrade` is the current command (avoid infinite loop)
- `popcorn version --check` is the current command (explicit check is informational)
- Installer is undetectable (unknown install method)
- Fetch fails (network, timeout, parse error)
- Subprocess upgrade fails (print warning, continue)
- `POPCORN_NO_UPDATE_CHECK` env var is set (CI/scripts opt-out)

## CLI Interface

```
popcorn version             # print version (existing)
popcorn version --check     # fetch latest, print comparison, update cache
```

## Output

**Auto-update (before command, to stderr):**
```
Updating popcorn 0.5.5 → 0.5.7...
✓ Updated
<normal command output on new version>
```

**Auto-update fails (to stderr):**
```
Update to 0.5.7 failed — run: popcorn upgrade
<normal command output on old version>
```

**Explicit check (up to date):**
```
popcorn 0.5.6 (up to date)
```

**Explicit check (outdated):**
```
popcorn 0.5.6 (0.5.7 available — run: popcorn upgrade)
```

**Explicit check (fetch fails):**
```
popcorn 0.5.6 (could not check for updates)
```

## Implementation

**Files:**
- `src/popcorn_cli/cli.py` — `_fetch_latest_version()`, `_read_version_cache()`, `_write_version_cache()`, `_check_and_update()`, modify `cmd_version()` to support `--check`, call `_check_and_update()` in `main()`
- `tests/test_upgrade.py` — add version check tests

**Functions:**

```python
def _fetch_latest_version(timeout: float = 2.0) -> str | None:
    """Fetch latest version from GitHub. Returns version string or None on failure."""

def _read_version_cache() -> tuple[str | None, float]:
    """Read cached version. Returns (version, checked_at) or (None, 0)."""

def _write_version_cache(version: str) -> None:
    """Write version + timestamp to cache file. Silent on failure."""

def _check_and_update() -> None:
    """Check for updates and auto-upgrade if outdated. Called from main()."""
```

**Exit codes:**
- Auto-update: no exit code impact (re-exec on success, continue on failure)
- `--check`: always exits 0

## Testing

```
test_fetch_latest_version_success
  Mock httpx.get → pyproject.toml content with version = "0.5.7"
  Assert: returns "0.5.7"

test_fetch_latest_version_timeout
  Mock httpx.get → TimeoutException
  Assert: returns None

test_fetch_latest_version_parse_error
  Mock httpx.get → garbage content
  Assert: returns None

test_read_write_version_cache
  Write cache, read back
  Assert: version and timestamp match

test_read_version_cache_missing
  Assert: returns (None, 0)

test_read_version_cache_corrupt
  Write garbage to cache file
  Assert: returns (None, 0)

test_check_and_update_up_to_date
  Mock cache fresh + same version
  Assert: no upgrade, no output

test_check_and_update_outdated_upgrades
  Mock cache stale, fetch returns newer version, detect installer → "uv"
  Mock subprocess → success
  Assert: prints "Updating..." message
  Assert: os.execvp called with original args

test_check_and_update_outdated_unknown_installer
  Mock outdated, detect installer → None
  Assert: no upgrade attempted, no output

test_check_and_update_fetch_fails
  Mock cache stale, fetch → None
  Assert: no upgrade, no output, command proceeds

test_check_and_update_skips_upgrade_command
  Set sys.argv to ["popcorn", "upgrade"]
  Assert: _check_and_update returns without checking

test_check_and_update_env_var_opt_out
  Set POPCORN_NO_UPDATE_CHECK=1
  Assert: _check_and_update returns without checking

test_version_check_explicit
  Mock fetch → "0.5.7", current = "0.5.5"
  Assert: prints "0.5.5 (0.5.7 available — run: popcorn upgrade)"
  Assert: cache updated

test_version_check_explicit_up_to_date
  Mock fetch → "0.5.5", current = "0.5.5"
  Assert: prints "0.5.5 (up to date)"

test_version_check_explicit_fetch_fails
  Mock fetch → None
  Assert: prints "could not check for updates"
```
