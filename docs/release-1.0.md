# Release Plan: 1.0.0 + release-please migration

The 1.0.0 release marks the point where the agent contract ([SPEC.md](../SPEC.md)) becomes public and stable. This is also the natural moment to switch from the current manual `make bump` workflow to automated, conventional-commit-driven releases via release-please.

## Timeline shape

```
  now                     1.0.0 tag              1.0.1+
   │                          │                     │
   │◄── 0.8.x manual bumps ──►│◄── release-please ──►│
   │                          │                     │
   │  pre-1.0 cleanup         │  cut 1.0.0 manually │  all future releases
   │  (this section)          │  (§ cut-over)       │  automated
```

---

## Phase 1 — Pre-1.0 cleanup

Complete before tagging 1.0.0. Each item is independently shippable on the current 0.8.x cadence.

### Contract review

- [ ] Walk SPEC.md section-by-section; compare against `popcorn commands --json` output. Anything promised in SPEC that isn't in the schema → fix one or the other.
- [ ] Diff each command's `--json` output against SPEC. Any field name or nesting that differs → reconcile now, not post-1.0.
- [ ] Confirm `error_code` enum coverage: run every command in an error path (bad input, wrong workspace, missing resource, expired auth, server down) and verify each returns a code from the stable set.
- [ ] Stability test: write a one-shot agent harness that calls ≥1 command per category and snapshots the envelope shapes. Re-run it after each 0.8.x bump to catch unintended breaks.

### Outstanding surface work

- [ ] `webhook deliveries` pagination. Currently deferred. Requires backend to expose a cursor or `has_more`. If the backend change lands, wire it up the same as the other paginated commands.
- [ ] Audit `--after` on `webhook deliveries` — the CLI accepts it but the OpenAPI spec for `/api/webhooks/deliveries` doesn't list `after` as a parameter. Either the spec is incomplete or the flag is a no-op. Verify and fix before 1.0.0.
- [ ] Re-run the "what's frozen" section of SPEC.md against code and confirm the promises hold.

### Docs

- [ ] Move SPEC.md promises into the codebase as docstring invariants where practical (e.g., `_json_ok` already enforces envelope shape — confirm the test suite pins it).
- [ ] Link SPEC.md from the top of README, from the agent-usage section, and from `popcorn commands --json` output (add a `spec_url` field).

---

## Phase 2 — Cutting 1.0.0 (one-time, manual)

Do this once. It's the last manual bump; everything after is release-please.

### Prep

1. Freeze the contract: announce a branch cut date. No breaking envelope/flag changes on `main` after this date.
2. Run full verification:
   ```bash
   make check                        # lint + typecheck + tests
   ./scripts/test-install.sh         # Docker-based install test in pip/pipx/uv
   popcorn doctor --json             # sanity
   popcorn commands --json | jq .    # shape sanity
   ```
3. Write CHANGELOG.md (the first formal one). Can be synthesized from git log since v0.1.0:
   ```bash
   git log v0.1.0..HEAD --pretty=format:'- %s' > /tmp/log
   # then edit into Unreleased → [1.0.0] sections, grouped by feat/fix/docs
   ```

### Cut

4. Bump:
   ```bash
   make bump v=1.0.0
   ```
5. Push and release:
   ```bash
   git push && git push --tags
   make release                      # gh release create v1.0.0
   uv build && uv publish            # PyPI
   ```
6. Verify installability:
   ```bash
   uvx popcorn-cli@1.0.0 version
   pipx install popcorn-cli==1.0.0 && popcorn version
   ```

---

## Phase 3 — Switch to release-please

For 1.0.1+.

### One-time setup

#### 1. Add the workflow

`.github/workflows/release-please.yml`:

```yaml
name: release-please

on:
  push:
    branches: [main]

permissions:
  contents: write
  pull-requests: write
  id-token: write        # required for PyPI trusted publishing

jobs:
  release-please:
    runs-on: ubuntu-latest
    outputs:
      released: ${{ steps.rp.outputs.release_created }}
      tag: ${{ steps.rp.outputs.tag_name }}
    steps:
      - uses: googleapis/release-please-action@v4
        id: rp
        with:
          config-file: release-please-config.json
          manifest-file: .release-please-manifest.json

  publish:
    needs: release-please
    if: needs.release-please.outputs.released == 'true'
    runs-on: ubuntu-latest
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ needs.release-please.outputs.tag }}
      - uses: astral-sh/setup-uv@v3
      - run: uv build
      - uses: pypa/gh-action-pypi-publish@release/v1
        # No api-token: trusted publishing authenticates via OIDC
```

#### 2. Release-please config

`release-please-config.json`:

```json
{
  "release-type": "python",
  "packages": {
    ".": {
      "package-name": "popcorn-cli",
      "version-file": "pyproject.toml",
      "include-component-in-tag": false,
      "changelog-sections": [
        {"type": "feat", "section": "Features"},
        {"type": "fix", "section": "Bug Fixes"},
        {"type": "perf", "section": "Performance"},
        {"type": "docs", "section": "Documentation", "hidden": false},
        {"type": "chore", "section": "Miscellaneous", "hidden": true}
      ]
    }
  },
  "bootstrap-sha": "<commit-sha-of-v1.0.0-tag>"
}
```

#### 3. Manifest seed

`.release-please-manifest.json`:

```json
{".": "1.0.0"}
```

Tells release-please where the project currently sits. It reads commits after this version to compute the next bump.

#### 4. PyPI trusted publishing

On [pypi.org](https://pypi.org/manage/account/publishing/), register a trusted publisher for `popcorn-cli`:

| Field | Value |
|---|---|
| Owner | `PopcornAiHq` |
| Repository | `popcorn-cli` |
| Workflow | `release-please.yml` |
| Environment | (leave blank, or use `pypi` if you configure one) |

Once configured, the `publish` job authenticates via OIDC — no API token in GitHub secrets, no rotation burden.

#### 5. Retire manual release mechanics

- Remove `make bump` from the documented release flow in CLAUDE.md (keep the target itself for emergencies).
- Delete `scripts/check-version-bump.sh` from `.pre-commit-config.yaml` — it's obsolete once release-please owns version bumps.
- Update CLAUDE.md § Versioning + § Publishing to describe the new flow:

  ```
  ## Versioning
  Use conventional-commit prefixes. release-please computes the next
  version automatically from commits since the last release.

    feat: new command or flag        → minor bump
    fix:  bug fix                    → patch bump
    feat!: or BREAKING CHANGE: ...   → major bump (never use before 2.0.0)

  ## Publishing
  Merge the release PR that release-please opens on main. CI tags the
  version, creates the GitHub release, and publishes to PyPI.
  ```

### Workflow after cut-over

```
Daily work:
  git commit -m "feat: add new doctor check"
  git push

Meanwhile, on main:
  release-please opens (or updates) a PR: "chore(main): release 1.0.1"
  with an auto-generated CHANGELOG.md entry

When ready to ship:
  Review the release PR → merge
  CI: creates tag v1.0.1, GitHub release, publishes to PyPI
  All within ~2 minutes of the merge
```

### Conventional commits — enforced scopes

The following prefixes will drive semver bumps after 1.0.0:

| Prefix | Bump | Notes |
|---|---|---|
| `feat:` | minor | New command, flag, or meaningful capability |
| `fix:` | patch | Bug fix |
| `perf:` | patch | Performance improvement |
| `docs:` | none | Documentation only |
| `refactor:` | none | No behavior change |
| `chore:` | none | Tooling, CI, deps |
| `feat!:` or `BREAKING CHANGE:` | **major** | Reserve for post-2.0 or dire need |

Everything already committed to this repo follows these conventions — no retraining needed.

---

## Phase 4 — Post-launch housekeeping

First 2 weeks after 1.0.0:

- [ ] Monitor the first release-please PR and confirm the automation produces what you expect. If the CHANGELOG is noisy, tune `changelog-sections` filters.
- [ ] Add a GitHub branch protection rule requiring conventional-commit prefix on the merge commit (or on PR title) so future contributors can't accidentally break semver inference.
- [ ] If agents start relying on SPEC.md in production, set up a GitHub Action that fails PRs modifying SPEC.md without an accompanying `schema_version` bump. Prevents accidental contract drift.

Ongoing:

- [ ] Any change to the agent contract must update SPEC.md and bump `schema_version` in the schema output. This is enforced by PR convention, not tooling — add a CI check if drift becomes a problem.
- [ ] New commands or flags don't bump `schema_version`; only changes to the *envelope*, *error_codes*, *exit_codes*, *pagination*, *streaming*, or *agent_mode* shape do.

---

## Risks & gotchas

- **PyPI trusted publishing requires the first release to be done manually with an API token**, then trusted publishing handles 1.0.1+. This plan uses manual publish for 1.0.0 and auto-publish from 1.0.1. That matches PyPI's recommended onboarding order.
- **release-please computes "next version" from commits since the last release tag.** If you have any commits between the 1.0.0 tag and the first release-please run that don't follow conventional commits, it may miss or miscategorize them. Keep commit discipline through the cut-over.
- **The release PR is re-generated on every push to main.** If you want to hold a release, don't merge the PR — but note that the PR will grow until merged. This is the intended UX.
- **`scripts/check-version-bump.sh` must be removed** from pre-commit before the first release-please-managed bump, or every non-release commit will get the warning and developers will stop reading it.
