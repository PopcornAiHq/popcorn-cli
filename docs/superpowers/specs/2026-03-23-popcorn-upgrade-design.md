# popcorn upgrade

**Date:** 2026-03-23
**Status:** Draft

## Problem

Users must manually remember how they installed `popcorn` and run the correct upgrade command. There's no built-in way to upgrade.

## Solution

Add `popcorn upgrade` that auto-detects the package installer and runs the correct upgrade command.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Auto-detect vs user specifies? | Auto-detect | Users shouldn't need to remember how they installed |
| Run or print command? | Run directly | Convenience; safe and reversible |
| Unknown installer? | Error with all 3 commands | Safest, no wrong guesses |
| Package source? | GitHub for now | Switch to PyPI later with one-line change |

## Installer Detection

Resolve the path of the running `popcorn` binary and inspect it:

```
sys.executable or shutil.which("popcorn") → resolve symlinks → check path components
  ├─ contains /uv/      → "uv"
  ├─ contains /pipx/    → "pipx"
  └─ neither            → None (unknown)
```

Detection heuristic:
- **uv:** binary lives under a path containing `/uv/` (e.g., `~/.local/share/uv/tools/popcorn-cli/...`)
- **pipx:** binary lives under a path containing `/pipx/` (e.g., `~/.local/share/pipx/venvs/popcorn-cli/...`)
- **fallback:** if neither matches, installer is unknown

## Upgrade Commands

| Installer | Command |
|-----------|---------|
| uv | `uv tool install --force git+https://github.com/PopcornAiHq/popcorn-cli.git` |
| pipx | `pipx install --force git+https://github.com/PopcornAiHq/popcorn-cli.git` |
| pip | `pip install --upgrade git+https://github.com/PopcornAiHq/popcorn-cli.git` |

When PyPI becomes the canonical source, these become:
- uv: `uv tool upgrade popcorn-cli`
- pipx: `pipx upgrade popcorn-cli`
- pip: `pip install --upgrade popcorn-cli`

This is a one-line constant change.

## CLI Interface

```
popcorn upgrade    # detect installer, run upgrade, report result
```

No flags needed.

## Flow

```
1. Record current version (importlib.metadata)
2. Detect installer from binary path
3. If unknown → print all 3 manual commands, exit 1
4. Print "Upgrading via {installer}..."
5. Run upgrade command as subprocess, stream stdout/stderr
6. If subprocess fails → print error + manual command, exit 1
7. Read new version (re-import or run popcorn --version)
8. If version changed → "✓ popcorn {old} → {new}"
9. If version same → "✓ popcorn {version} (already up to date)"
```

## Output

**Success (upgraded):**
```
Upgrading via uv...
✓ popcorn 0.5.5 → 0.5.6
```

**Success (already current):**
```
Upgrading via uv...
✓ popcorn 0.5.6 (already up to date)
```

**Unknown installer:**
```
Could not detect how popcorn was installed. Run one of:
  uv tool install --force git+https://github.com/PopcornAiHq/popcorn-cli.git
  pipx install --force git+https://github.com/PopcornAiHq/popcorn-cli.git
  pip install --upgrade git+https://github.com/PopcornAiHq/popcorn-cli.git
```

**Subprocess failure:**
```
Upgrading via uv...
Upgrade failed (exit code 1). Run manually:
  uv tool install --force git+https://github.com/PopcornAiHq/popcorn-cli.git
```

## Implementation

**Files:**
- `src/popcorn_cli/cli.py` — new `cmd_upgrade()` handler, `_detect_installer()` helper, argument registration
- `tests/test_upgrade.py` — new test file

**Functions:**

```python
GITHUB_URL = "git+https://github.com/PopcornAiHq/popcorn-cli.git"

def _detect_installer() -> str | None:
    """Detect how popcorn was installed. Returns 'uv', 'pipx', or None."""
    # Resolve binary path, check for /uv/ or /pipx/ in path components

def cmd_upgrade(args) -> None:
    """Upgrade popcorn to the latest version."""
    # 1. Get current version
    # 2. Detect installer
    # 3. Run subprocess
    # 4. Report result
```

**Exit codes:**
- 0: upgrade succeeded (or already up to date)
- 1: unknown installer or subprocess failure (EXIT_VALIDATION)

## Testing

```
test_detect_installer_uv
  Mock sys.executable to path containing /uv/ → returns "uv"

test_detect_installer_pipx
  Mock sys.executable to path containing /pipx/ → returns "pipx"

test_detect_installer_unknown
  Mock sys.executable to /usr/local/bin/popcorn → returns None

test_cmd_upgrade_success
  Mock _detect_installer → "uv", mock subprocess → exit 0
  Mock new version > old version
  Assert: "✓ popcorn 0.5.5 → 0.5.6" in output

test_cmd_upgrade_already_current
  Mock _detect_installer → "uv", mock subprocess → exit 0
  Mock new version == old version
  Assert: "already up to date" in output

test_cmd_upgrade_unknown_installer
  Mock _detect_installer → None
  Assert: prints all 3 commands, exit 1

test_cmd_upgrade_subprocess_failure
  Mock _detect_installer → "pipx", mock subprocess → exit 1
  Assert: "Upgrade failed" + manual command in output, exit 1
```
