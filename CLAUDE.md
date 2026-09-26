# CLAUDE.md — popcorn-cli

CLI for the Popcorn API, installing the `popcorn` command. **Not published to PyPI** — it is installed and upgraded straight from this GitHub repo.

## This repository is public

`PopcornAiHq/popcorn-cli` is public; the backend repo it talks to is private.
Internal-only references must not be written here: private repo PR numbers,
backend source paths, real infrastructure identifiers, employee email
addresses, and production record ids. Issue-tracker ids are the one exception,
and only outside tracked files — see "Ticket ids" below.

**Cite behaviour, never the thing that proves it.** "The server rejects a
webhook update that changes its trigger flow" belongs here; the ticket number
and the private source file that implement that rule do not. A comment that
needs the citation to make sense is under-written — say the thing.

Fixture identifiers are synthetic, never copied from a live system:
`example-*` names and `00000000-0000-4000-8000-0000000000NN` uuids.

This cuts directly against the private repo's own conventions, which encourage
citing PR numbers and `lib/<domain>/…` paths as durable references. That is
correct there. Do not carry it across a `cd`.

`scripts/check-public-repo.sh` enforces the mechanical part of this, as a
pre-commit hook and as its own CI job — it scans the index for the patterns that
have zero false positives here. It catches an id or a path; it cannot catch a
paragraph that describes internal architecture, so the judgement above is still
yours.

**Commit messages count.** Every merge method `main` allows — squash, merge
commit, rebase — publishes the branch's commit messages, the pull request's
title, or both, so a private path in a feature-branch commit is published on
`main` for good. The same script checks each message as a `commit-msg` hook
and, in CI (`.github/workflows/public-guard.yml`, which re-runs when the pull
request is retitled), every commit in the pull request plus its title. A clone
set up before the hook existed needs `make install` again to get it. The PR
description is not scanned — it never reaches `main`, but it is public, so
keep it to the same rule.

**Ticket ids.** A Linear id (`KEW-NNNN`) may go in a commit message, a pull
request title or description, and a branch name — that is what Linear's GitHub
integration reads to link and close the issue, and a bare id discloses nothing
the change itself does not. It still never goes in a tracked file: code,
tests, comments, docs. To close the issue when the pull request merges, put a
closing magic word and the id in the **PR description**:

    Fixes KEW-NNNN

(`closes`, `resolves`, `completes` and `implements` work too.) Use
`Part of KEW-NNNN` or `Refs KEW-NNNN` to link without closing — for one step of
a larger ticket. A bare id in the PR title or a branch named from Linear's
"Copy git branch name" also links it, and merging such a PR moves the issue
on per the team's workflow settings. The guard allows ids in messages and
titles and still refuses them in the index scan.

**Audit with `git ls-files`, not `ls`, `grep -r`, `find` or `wc`.** Those walk
gitignored scratch directories and over-report, in the direction that
manufactures false alarms — a previous audit claimed private planning documents
were sitting in this repo when the directory holding them has never had a
tracked file. Confirm any individual hit with `git ls-files <path>` or
`git check-ignore -v <path>`.

Removing something from `HEAD` does not unpublish it. Commit messages, release
notes and merged diffs are already public; the check exists to stop the next one,
not to repair the last.

## Structure

```
popcorn-cli/
├── src/
│   ├── popcorn_core/          ← Shared lib (auth, client, config, resolve, operations)
│   └── popcorn_cli/           ← CLI (argparse, handlers, formatting)
├── tests/                     ← pytest
├── scripts/                   ← test-install.sh (Docker install tests), sync_flow_rules.py,
│                                 check-public-repo.sh (public-repo guard),
│                                 check_version.py + auto_tag.sh (release gates)
├── pyproject.toml             ← Single package config
├── Makefile                   ← fmt, lint, typecheck, test, check, dev, sync-rules
└── .pre-commit-config.yaml
```

## Development

```bash
make install    # uv sync + pre-commit install
make dev        # create bin/popcorn wrapper for local dev
make fmt        # ruff format
make lint       # ruff check --fix
make typecheck  # mypy
make test       # pytest
make check      # lint + typecheck + test (lint auto-fixes)
make ci         # what CI runs — same gates, but fails instead of auto-fixing
```

`make check` runs `ruff check --fix`, so a lint violation is silently repaired
rather than reported. `make ci` is the non-mutating form (`ruff check`,
`ruff format --check`) and is what `.github/workflows/ci.yml` executes on every
PR across Python 3.10–3.13. Run `make ci` before pushing if you want the same
answer CI will give.

## Key Details

- **Entry point:** `popcorn = "popcorn_cli.cli:main"`
- **Dependencies:** `httpx` (HTTP client), `pyjwt` (JWT decode)
- **Build system:** hatchling
- **Python:** >=3.10
- **Version:** runtime via `importlib.metadata` — update only in `pyproject.toml`

## Command architecture

New command families go in `src/popcorn_cli/commands/<name>.py` and are declared
once via `src/popcorn_cli/registry.py` — which derives argparse, dispatch, both
shell completions, `popcorn commands --json`, and the fuzzy-match list. Do **not**
add a family by hand-editing the completion generators or the schema builder.
Families still living in `cli.py` are mid-migration; see
`docs/architecture-commands.md`.

## Channel templates

The authoring guide is at <https://docs.popcorn.ai/guides/template-authoring.md>
(`docs/TEMPLATE_AUTHORING.md` here is a pointer to it). It describes the
platform rather than this CLI, which is why it does not live here. Its §2 is the thing to keep straight: a **new** app type
needs a server-side registration plus a deploy, neither of which the CLI can
do, but **editing** one is a pure CLI loop (`app fork` → `checkout` →
`publish`) with no deploy in it.

Two bundles back the guide's §6 contrast. **They are checker fixtures, not
reference templates** — they live under `tests/fixtures/bundles/` and are not
offered to authors to copy, because neither declares a `version:` (so neither
can publish) and both have drifted from what the platform ships. An author
gets canonical source from `popcorn app checkout`, which cannot go stale.

| Bundle | Point |
|---|---|
| `tests/fixtures/bundles/alerttracker/` | four producers; must *derive* severity/env, so one LLM call per delivery |
| `tests/fixtures/bundles/deploywatch/` | one producer (GitHub `deployment_status`); *extracts* every field by path, zero LLM calls. The worked `fields.extract` example |

The contrast between them is the guide's §6 and is deliberate — don't collapse
them into one bundle or make either multi-producer.

`examples/` keeps what is not bundle source and cannot be fetched from a
server: `alerttracker/GOTCHAS.md`, the evidence log behind most of the guide,
and the sample webhook payloads under each `fixtures/`.

The guide deliberately does **not** restate activity names, arguments, or
result schemas — those are served by `flow activities` and enforced by `flow
validate`, and duplicating them here would rot. Keep it that way when editing.

### `popcorn template check`

`src/popcorn_core/template_check.py` is an **offline** structural checker
(`popcorn template check <dir>`); the command lives in
`src/popcorn_cli/commands/template.py`. It exists because nothing in CI parsed
the shipped examples, and a broken `alert_tick` lived in one for a day.

It is scoped to what a server cannot tell you or will accept in silence:
cross-file agreement, the importer's flattening contract, undeclared column
writes, `output_schema` properties that are dereferenced but not `required`.
**It is not a second validator** — never teach it activity names or argument
schemas, which are the server's to own and would rot here exactly as the guide
says.

The line is *catalog vs grammar*. It must know the DSL's shape — a step is one
of the served `STEP_ACTIONS`, a block's inner ids are private, `$trigger` is a
closed key set, `collect:` publishes a second name — because without that it
cannot tell a reference from a typo. It must not know what
`foundation.store.upsert_rows` takes; which of its args carry column names is
served, in `flow_rules.ACTIVITY_ROLES`, and read from there.

**The shape is generated, not authored.** `src/popcorn_core/flow_rules.py` is a
snapshot of `GET /customer-flows/schema`, written by
`scripts/sync_flow_rules.py`; the checker imports it, `app_checkout` (for the
on-disk baseline) and nothing else beyond stdlib, so `template check` stays
offline — no server, no channel, no credentials, identical findings on every
machine, which is what `--strict` in CI has to guarantee.

```
the server's flow-document schema
   │  GET /api/customer-flows/schema
   ▼
scripts/sync_flow_rules.py  (make sync-rules / make check-rules,
   │                         or --from <saved body> after each prod deploy)
   │  renders, deterministically — no timestamp, so no diff means no change
   ▼
src/popcorn_core/flow_rules.py   ← GENERATED, do not edit
   │  import
   ▼
src/popcorn_core/template_check.py
```

Do not hand-edit the generated module, and do not re-vendor a rule it already
carries. `make check-rules` fetches and fails on any diff, including a
hand-edit; it needs credentials, so it cannot run in CI and is a
before-a-release check instead. A newly served rule makes the script **fail**
rather than skip, so a rule the checker does not consume forces a decision.

**A rule change arrives as a bot PR.** After each production deploy, the
platform's pipeline exports this payload for the deployed code, runs
`sync_flow_rules.py --from` against a checkout of this repo, and — when the
module changed — has the docs bot open a pull request on
`automation/sync-flow-rules` with the regenerated module and a patch bump
(it touches `src/`, so the version rules below apply to it like any other
PR). It is never auto-merged: read the diff, because a changed rule may need
a checker change or new longhand assertions in `tests/test_flow_rules.py`,
which will fail on the PR until someone adds them. Push those to the same
branch: once it carries a commit that is not the bot's, a later deploy no
longer force-pushes over it and comments on the PR instead.

Two rules were live divergences when this landed, both under-warns: the
reference grammar accepted `$a.`, `$a..b` and `$a.1b`, which the interpreter
rejects, and `max_block_depth` had no counterpart at all, so a block nested
past the cap checked clean and failed at install (`block-too-deep`).

**Code blocks are a third path classification** (`code/<block>/…`), served
since a server-side change. A block file keeps its whole path rather than
flattening to a basename, because neither reader keys it that way — which is
what stopped `template check` reporting a `basename-collision` between two
blocks' `main.py`, the entrypoint the runner's convention requires each Python
block to carry. The rule also makes a `.yaml` under a block block source rather
than a lost flow.
The two findings it adds — `code-file-outside-block` and
`code-block-name-invalid` — are paths `app publish` refuses outright.

**Agents are a fourth** (`agents/<name>/agent.yaml`, `prompt.md`,
`schemas/*.json`). The checker only stops misreading them — as flows with no
steps, nested flows, and basename collisions between every pair of agents. It
adds no finding of its own for them. `app publish` sends exactly the served
layout (`app_publish.is_agent_path`, which the checker also calls), and
anything else under `agents/` is filtered and reported as ignored, which
`path-not-published` repeats. Filtered rather than collected-for-refusal the
way `code/` is, because a half-formed agent cannot slip through: the server
parses every agent directory at publish and refuses one missing its required
files.

**Activity roles are served too** (`ACTIVITY_ROLES`, `STEP_ERROR_PROPERTIES`).
The column checks key off each activity's served `column_args` and the
output-schema check off its `output_schema_arg`. The hand-written lists these
replaced named activities the platform never had, so their checks never fired,
and missed an activity taking its schema under `schema`. The generator refuses
a role vocabulary (`holds`, `side`) it does not list, because a new value would
otherwise match nothing and read as clean.

A hidden entry below a block (`code/calc/.env`) gets the tree refused too, but
`_collect_files` drops every dotted path before the check runs, and
`app_publish.collect_tree` applies the same filter when reading a working copy,
so the CLI would never have uploaded it. That symmetry is the reason there is no
finding for it — not an oversight.

**The checkout baseline is the fourth input** (`_check_checkout_version`).
`.popcorn-app.json` names the version the working copy came from, so the
checker can predict `app_publish.require_bump` offline: `version-not-advanced`
is an error because a published version is immutable and the server refuses the
publish outright, `changelog-not-updated` a warning because the stale note
ships as content rather than blocking anything. Both are gated on the baseline
existing — bundle SOURCE that was never checked out (the platform's own
template tree, `tests/fixtures/bundles/`) has none, so neither check applies
there rather than failing open or closed, and the server side owns the
equivalent rule for its own tree. The changelog comparison additionally needs a
v3 baseline, which is the first that captured the served note; an older
checkout gets the version check and silence on the changelog. Its wording is
fork-aware, off `Baseline.kind`: only the product publish path reads the
manifest's `changelog:`, so on a fork line the note is bundle documentation and
`app publish -m` is what the registry records. Giving a fork author the product
answer is what made this check contradict the very next command they would run.
The baseline also sets the level of `clears-app-type`: a warning for bundle
source (an untyped ops bundle can mean it), an error in a checkout, which always
came from a typed version and which the server refuses to publish without
`app_type:` — `app publish` refuses the same tree before any request.

**Where it will not follow: `when:`.** Four rails, routed legacy-first (see the
guide's §4). Mirroring that offline means reimplementing the predicate parser,
so the checker validates the references inside a `when:` and asserts nothing
about its grammar. A near-miss reimplementation is worse than no check: the
"exactly one comparison" rule it used to enforce rejected 55 valid clauses.

Finding `code` values are a stable contract (CI and agents branch on them);
renaming one is a minor version bump.

Four test layers, and the gap at the bottom is deliberate:

| | Runs | Guards |
|---|---|---|
| `tests/test_fixture_bundles.py` | CI | every `tests/fixtures/bundles/*/`, so a new bundle is gated the moment it is added |
| `tests/test_template_check.py` | CI | one grammar feature per test, derived from what real templates do |
| `tests/test_flow_rules.py` | CI | every generated rule, asserted longhand, plus the generator's own failure modes |
| `tests/test_backend_templates.py` | **local only** | the bundles the platform actually ships, read from a local server-side checkout |

The last one needs `POPCORN_BACKEND_FLOWS` to point at a directory of those
bundles and skips otherwise, so **it does not run in CI** — vendoring copies
would rot within a release. It exists because the checker shipped with
~180 false positives against those templates while passing everything in this
repo: the fixture bundles use no block, no `collect:`, no expression-rail
`when:`, no `$trigger`, no `.md.j2` prompt. Run it after touching the checker.

## Auth

Clerk OAuth PKCE flow with two modes:
- **Browser:** opens auth page, local callback server on port 28771
- **Headless:** `echo $TOKEN | popcorn auth login --with-token`
- **Refresh:** automatic on 401, uses stored refresh_token

Config stored at `~/.config/popcorn/auth.json` (0600 permissions).

## Environments

Default: `https://api.popcorn.ai` (production)

Custom environments via env vars (for internal/dev use):
- `POPCORN_API_URL` — API base URL
- `POPCORN_CLERK_ISSUER` — Clerk OIDC issuer URL
- `POPCORN_CLERK_CLIENT_ID` — Clerk OAuth client ID

Multiple profiles are stored in the config file. Switch with `popcorn env <name>`.

**No internal URLs or credentials are shipped in this package.**

## Testing Installation

```bash
./scripts/test-install.sh    # Docker-based: tests pip, pipx, uv install
```

Builds the wheel and verifies it installs correctly with each package manager in isolated containers.

## `main` is protected

Branch protection is on, so the conventions below are enforced rather than
agreed:

| Setting | Effect |
|---|---|
| Require a pull request (0 approvals) | No direct push to `main`, without needing someone to approve your own work |
| Require status checks | `No internal-only references`, `Version is releasable`, and `test` on each supported Python |
| Require branches up to date | A branch whose base moved must update before merging |
| Force push / branch deletion | Both refused |
| Admin enforcement | **Off** — an admin can still force a fix through when `main` is broken |

"Require branches up to date" is what makes the version-collision check work.
Two branches cut from the same `main` can both bump to the same number, and git
merges that without a conflict because each side sets the same line to the same
value — so the second PR only sees the clash once its base includes the first
merge. The check cannot force that on its own.

⚠️ **The required-check list is a set of job names, and it rots silently.**
Renaming a job in `ci.yml` means the old context never reports again, and every
PR blocks on a check that can no longer run — with no hint as to why. Rename a
job and update the protection in the same change:

```bash
gh api repos/PopcornAiHq/popcorn-cli/branches/main/protection --jq '.required_status_checks.contexts'
```

Tags are not covered by branch protection, which is why the auto-tag workflow
below can push one.

## Versioning

**Bump the version in the PR that changes `src/`.** You choose the number;
nothing infers it. CI only refuses one that cannot be right.

- **Patch** (0.5.5 → 0.5.6): default for most changes — bug fixes, small features, refactors
- **Minor** (0.5.x → 0.6.0): larger features, new commands, breaking-ish changes.
  Pre-1.0, a breaking change rides here — removing a whole command family did
- **Major**: never, unless explicitly told. `scripts/auto_tag.sh` refuses to tag
  one, so a major that merges is a major that never releases

`scripts/check_version.py` fails a PR that changes `src/` without bumping, that
picks a number already released, or that goes backwards. It runs in CI and in
`make ci`. The existing pre-commit hook still *warns* about a missing bump; the
CI check is what holds for someone who never ran `pre-commit install`.

Edit `pyproject.toml` by hand in the PR and run `uv lock`; commit both.
`make bump` also *tags*, so it cannot be used on a feature branch — the tag
would point at a commit the squash-merge discards.

Version lives only in `pyproject.toml` — runtime reads it via `importlib.metadata`.

## Publishing

**Not on PyPI, and there is no `uv publish` step.** Distribution is the repo
itself — users install and upgrade from
`git+https://github.com/PopcornAiHq/popcorn-cli.git` (see README), and the
CLI's own self-upgrade hardcodes that URL (`cli.py — _GITHUB_URL`). So there is
no artifact to push anywhere; a release is just a tag.

**Merging releases.** Once CI passes on `main`, `.github/workflows/auto-tag.yml`
runs `scripts/auto_tag.sh`, which reads the version from `pyproject.toml` and —
if no tag exists for it — tags the merge commit, pushes, and creates the GitHub
release. A merge whose version is already tagged is a no-op, which is the right
answer for a docs or CI change.

It waits on CI rather than triggering on the push, because tagging a red `main`
publishes a release users self-upgrade into. And it creates the release itself
instead of letting the tag push fire `release.yml`: a tag pushed with the
default `GITHUB_TOKEN` does **not** trigger another workflow, so relying on that
would leave a reachable version with no release and no error.

`release.yml` still handles a tag pushed by hand, which is the path for a major
bump. `scripts/auto_tag.sh --dry-run` says what would happen without touching
anything.

`make release` builds a release from the latest tag by hand, and is only for
when the workflow did not run — running both against one tag makes the second
fail, since the release already exists.

## API Alignment

**The backend OpenAPI spec is the source of truth.** Always fetch and check it when adding or modifying commands:

```bash
popcorn api /openapi.json --raw > /tmp/popcorn-openapi.json
```

The spec is auto-generated from FastAPI's Pydantic models and route definitions. It gives you exact field names, types, HTTP methods, and required/optional status for every endpoint. Do not guess or assume — fetch the spec.

## Agent-Facing Contract

This CLI is designed to be consumed by LLM agents as well as humans. Treat the following as a **stable public contract** — breaking any of it is a minor version bump at minimum.

- **Agent mode:** `POPCORN_AGENT=1` implies `--json`, `--quiet`, `--no-color`, and `POPCORN_NO_UPDATE_CHECK=1`. Injected in `_hoist_global_flags` (`cli.py`).
- **Success envelope:** `{"ok": true, "data": ...}`. `_json_ok` (`cli.py`) strips any leaked top-level `ok` key from `data` so the CLI envelope is never shadowed by an upstream API response envelope.
- **Error envelope:** `{"ok": false, "error": "...", "error_code": "...", "code": "...", "retryable": bool, ...}`.
  - `error_code` is the **stable** machine-readable enum agents should branch on.
  - `code` is the Python exception class name (legacy, avoid branching on).
  - Enum values and their descriptions live in `popcorn_core.errors.ERROR_CODES`. `APIError.error_code` derives from HTTP status via `_api_status_to_error_code`.
  - When raising `PopcornError` for a specific failure (e.g. not found, conflict), pass `error_code="not_found"` so agents can branch cleanly.
- **Exit codes:** defined in `popcorn_core.errors` (`EXIT_OK`, `EXIT_VALIDATION`, `EXIT_AUTH`, `EXIT_CLIENT`, `EXIT_SERVER`, `EXIT_UNHEALTHY`, `EXIT_INTERRUPT`). Semantic — agents switch on these to decide retry vs bail.
- **Schema discovery:** `popcorn commands --json` emits the full schema including `exit_codes`, `error_codes`, `envelope`, `agent_mode`, `global_flags`, and every command's arg types. Update this when adding agent-facing surface (`cmd_commands` in `cli.py`).
- **Confirmation prompts:** interactive confirmations go through `_confirm(args, prompt)` in `cli.py`. It honors `--yes`/`-y` and `POPCORN_ASSUME_YES=1`, and **fails loudly** (raises `PopcornError`) in non-TTY mode otherwise — never silently no-op or hang. When adding a destructive op that needs confirmation, use `_confirm`, not `input()`.
  - `_confirm_force(args, prompt)` is the same rule keyed on `--force` instead, for an op that destroys work the caller may be the only holder of (`app checkout` over a non-empty directory). `-y` deliberately does **not** answer it: agents and scripts pass it by reflex and cannot notice what was lost. A prompt is one or the other, never both.
  - `app publish` confirms through `_confirm`, plus one rule of its own: in agent mode (`POPCORN_AGENT`) it is refused without `--yes` even on a TTY, and that refusal — like the non-TTY one — happens before any request. A publish reaches every channel on the fork line, and no server guard asks whether that was meant.
- **`api --data` body sources:** `_resolve_data_arg` accepts literal JSON, `@-` (stdin), or `@path` (file), matching `curl` and `gh api`. Agents piping large payloads should use `@-`.
- **Streaming (`--watch`):** goes through `_json_line` (not `_json_ok`) — one NDJSON envelope per line, no pretty-printing, flushed every write. Same `_strip_leaked_ok` applies. `_json_ok` / `_json_line` are the two allowed JSON-output paths; don't hand-roll envelopes.
- **Pagination:** paginated commands include `data.pagination.next` — a dict of CLI flag→value pairs the agent feeds back to the same command for the next page, or `null` when no more. Use `_attach_pagination(data, next_flags)` to emit the field. Applied to `message list` (cursor-based, `has_more`), `message search` (offset-based, `has_more`), `message threads` and `workspace inbox` (offset-based, heuristic `len == limit` — worst case the agent fetches one empty page). `webhook deliveries` is deferred until the API exposes a reliable cursor.
- **`popcorn doctor`:** returns a structured diagnostic report (auth state, API reachability + latency, config file permissions, relevant env vars, list of detected `issues`). `--json` emits the full dict — the canonical agent/support-debug entry point when a user reports "popcorn isn't working". When adding a new failure mode the CLI should diagnose, append to the `issues` list in `cmd_doctor`.

## Conventions

- Color output respects `NO_COLOR` env var and `--no-color` flag
- All API errors surfaced as `PopcornError` subclasses (no tracebacks for users)
- Channel name resolution cached 5 min (`#name` → UUID)
- Pre-commit runs ruff (format + lint), the version-bump reminder, and the
  public-repo reference check on every commit
