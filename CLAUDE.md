# CLAUDE.md — popcorn-cli

CLI for the Popcorn API, installing the `popcorn` command. It is not on PyPI:
users install and upgrade straight from this GitHub repo.

## This repository is public

`PopcornAiHq/popcorn-cli` is public; the backend it talks to is private. Don't
write internal-only references here: private-repo PR numbers, backend source
paths, real infrastructure identifiers, employee emails, production record ids,
internal URLs or credentials.

Cite behaviour, not the thing that proves it. "The server rejects a webhook
update that changes its trigger flow" belongs here; the ticket and the private
source file behind it do not. A comment that needs the citation to make sense
is under-written. This is the opposite of the backend repo's convention, which
encourages PR numbers and `lib/<domain>/…` paths — don't carry that across a
`cd`.

Fixture identifiers are synthetic: `example-*` names and
`00000000-0000-4000-8000-0000000000NN` uuids.

`scripts/check-public-repo.sh` enforces the mechanical part: it scans the index
(pre-commit and CI), each commit message (`commit-msg` hook), and in
`.github/workflows/public-guard.yml` every commit in the PR plus its title. It
catches ids and paths, not a paragraph describing internal architecture — that
judgement is still yours.

- **Commit messages and PR titles are published.** Every merge method `main`
  allows puts branch commit messages or the PR title on `main` for good. The PR
  description isn't scanned but is public, so the same rule applies. A clone set
  up before the `commit-msg` hook existed needs `make install` again.
- **Linear ids** (`KEW-NNNN`) may go in commit messages, PR titles and
  descriptions, and branch names — Linear's GitHub integration reads them there.
  They never go in tracked files. To close the issue on merge, put `Fixes
  KEW-NNNN` in the PR description; use `Part of KEW-NNNN` to link without
  closing.
- **Audit with `git ls-files`, not `ls`/`grep -r`/`find`.** Those walk
  gitignored scratch directories and produce false alarms — a previous audit
  "found" private planning docs in a directory that never had a tracked file.
  Confirm a hit with `git ls-files <path>` or `git check-ignore -v <path>`.
- Removing something from `HEAD` does not unpublish it; the guard stops the next
  leak, it doesn't repair the last.

## Development

`make help` lists targets. The two that matter:

- `make check` runs `ruff check --fix`, so lint violations are silently repaired
  rather than reported.
- `make ci` is the non-mutating form and is what `.github/workflows/ci.yml` runs
  (public-repo guard, version check, lint, format check, mypy, pytest across
  Python 3.10–3.13). Run it before pushing to get CI's answer.

`scripts/test-install.sh` builds the wheel and installs it with pip, pipx and uv
in Docker.

## Command architecture

New command families go in `src/popcorn_cli/commands/<name>.py` and are declared
once through `src/popcorn_cli/registry.py`, which derives argparse, dispatch,
both shell completions, `popcorn commands --json`, and the fuzzy-match list.
Don't add a family by hand-editing the completion generators or the schema
builder. Some families keep their handlers in `cli.py`, late-bound by name
through `commands/_late.py`; see `docs/architecture-commands.md` before touching
the registry.

**Projects, not channels; apps, not bundles.** User-facing text says project
and app. The server still says channel and conversation, so API paths, JSON
field names, the DSL's `$channel.*` and `foundation.channel.*`, and channel
type values (`workspace_channel`) keep the old word — don't rename those. The
old command and flag spellings still work through hidden aliases; see
`docs/architecture-commands.md` § Renamed families and flags.

The registry does not generate the top-level `popcorn --help` listing — that's a
hand-written epilog in `cli.py — build_parser`. Update it when you add, remove
or rename a subcommand.

When adding or changing a command, check the backend's OpenAPI spec for field
names, types and required/optional status rather than guessing:
`popcorn api /openapi.json --raw`.

## Agent-facing contract

`SPEC.md` is the contract agents consume: envelopes, `error_code` values, exit
codes, pagination, streaming, `popcorn commands --json`, `popcorn doctor`.
Breaking any of it is at least a minor bump; update `SPEC.md` in the same PR.
The implementation rules that keep it true:

- JSON output goes through `cli.py — _json_ok` (one envelope) or `_json_line`
  (NDJSON for `--watch`). Both strip a leaked upstream `ok` key; don't hand-roll
  an envelope.
- Raise `PopcornError` subclasses, never let a traceback reach the user. Pass a
  specific `error_code=` (values in `popcorn_core.errors.ERROR_CODES`) so agents
  can branch; `code` is the class name and is legacy.
- Paginated output uses `_attach_pagination(data, next_flags)`.
- New agent-facing surface belongs in `cmd_commands`'s schema; a new failure
  mode worth diagnosing goes in `cmd_doctor`'s `issues`.
- Confirmations go through `_confirm(args, prompt)`, never `input()`. It honours
  `--yes`/`POPCORN_ASSUME_YES=1` and raises in non-TTY mode rather than hanging
  or no-opping.
  - `_confirm_force` is the same rule keyed on `--force`, for an op that
    destroys work only the caller may hold (`app checkout` over a non-empty
    directory). `-y` deliberately doesn't answer it: agents pass `-y` by reflex
    and can't notice what was lost. A prompt uses one or the other, never both.
  - `app publish` additionally refuses in agent mode without `--yes` even on a
    TTY, before any request, because a publish reaches every project on the fork
    line and no server guard asks whether that was meant.

## Apps (formerly channel templates)

The authoring guide lives at <https://docs.popcorn.ai/guides/template-authoring.md>;
`docs/TEMPLATE_AUTHORING.md` is a pointer to it, kept so references resolve
(section numbers match). It describes the platform, not this CLI. Don't copy
guide content into this repo, and don't restate activity names, arguments or
result schemas anywhere — `flow activities` serves them and `flow validate`
enforces them.

`tests/fixtures/bundles/{alerttracker,deploywatch}/` are checker fixtures, not
reference templates: neither declares a `version:` and both have drifted from
what the platform ships. Authors get real source from `popcorn app checkout`.
The pair backs the guide's §6 contrast (four producers deriving fields via LLM
vs one producer extracting every field by path), so don't merge them or make
`deploywatch` multi-producer. `examples/` holds only what can't be fetched from
a server; see `examples/README.md`.

### `popcorn app validate`

`popcorn template check` is its old name, kept as a hidden alias (`Command.hidden`
in the registry) because callers outside this repo still use it.

`src/popcorn_core/template_check.py` is the offline structural checker behind
it; `app validate` adds the server's publish checks in a fork checkout when
logged in, and says when it skipped them. The module docstring explains the
scope; the rules to keep when changing it:

- **Grammar, not catalog.** It models the DSL's shape (step actions, block
  scoping, `$trigger` keys, `collect:`) but never what an activity takes — which
  args carry column names or a result schema comes from the served
  `flow_rules.ACTIVITY_ROLES`. It deliberately doesn't model `when:` grammar; a
  near-miss reimplementation once rejected dozens of valid clauses.
- **The checker stays offline**: imports only stdlib, `flow_rules`, and
  `app_checkout`, so its findings are identical on every machine. The server
  call lives in the command (`commands/app.py — _run_server_checks`), and its
  findings carry code `publish-refused`.
- **Never copy a server rule into it.** A rule the server enforces at publish
  (the manifest's table rules, for one) is reported by the server call;
  restating it here is how the two drift.
- **Finding `code` values are a stable contract** (CI and agents branch on
  them); renaming one is a minor bump.

`src/popcorn_core/flow_rules.py` is generated from `GET /customer-flows/schema`
by `scripts/sync_flow_rules.py`. Don't hand-edit it or re-vendor a rule it
carries. `make check-rules` fails on any drift but needs member credentials, so
it's a before-release check, not CI.

Rule changes normally arrive as a bot PR on `automation/sync-flow-rules` after a
production deploy, with the regenerated module and a patch bump. It's never
auto-merged: read the diff, since a changed rule may need a checker change or
new longhand assertions in `tests/test_flow_rules.py` (which fail until added).
Push fixes to the same branch — once it has a non-bot commit, later deploys
comment instead of force-pushing over it.

After touching the checker, run `tests/test_backend_templates.py` with
`POPCORN_BACKEND_FLOWS` pointing at a local checkout of the platform's shipped
bundles. It skips without that variable and never runs in CI, yet it is the
only test that sees real templates — the checker once passed everything here
while producing ~180 false positives against them. A silent skip can also mean
the variable points somewhere stale.

## `main` is protected

PRs required (0 approvals), no force push, branches must be up to date, admin
enforcement off. Required checks: `No internal-only references`, `Version is
releasable`, and `test (3.x)` per Python version.

- "Up to date" is what makes the version-collision check work: two branches can
  bump to the same number and git merges it without conflict, so the second PR
  only sees the clash once its base includes the first merge.
- Required checks are job names and rot silently. Renaming a job in `ci.yml`
  leaves every PR blocked on a context that never reports. Update the protection
  in the same change (`gh api repos/PopcornAiHq/popcorn-cli/branches/main/protection`).

## Versioning

Bump the version in `pyproject.toml` in every PR that changes `src/`, then run
`uv lock` and commit both. It's the only place the version lives (runtime reads
`importlib.metadata`).

- **Patch**: default — fixes, small features, refactors.
- **Minor**: new commands, larger features, breaking changes (pre-1.0, removing
  a command family rode a minor).
- **Major**: only when explicitly told. `scripts/auto_tag.sh` refuses to tag
  one, so a merged major never releases; it needs a hand-pushed tag.

`scripts/check_version.py` (CI and `make ci`) fails a PR that changes `src/`
without a bump, reuses a released number, or goes backwards. Don't use `make
bump` on a feature branch — it also tags, and the tag would point at a commit
the squash-merge discards.

## Releasing

There's no publish step; a release is a tag. Users install from
`git+https://github.com/PopcornAiHq/popcorn-cli.git`, and self-upgrade hardcodes
that URL (`cli.py — _GITHUB_URL`).

After CI passes on `main`, `.github/workflows/auto-tag.yml` runs
`scripts/auto_tag.sh`: if the `pyproject.toml` version has no tag, it tags the
merge commit and creates the GitHub release; otherwise it's a no-op (right for
docs/CI-only merges). It waits on CI because tagging a red `main` ships a
release users self-upgrade into. It creates the release itself because a tag
pushed with `GITHUB_TOKEN` doesn't trigger `release.yml`. `--dry-run` shows what
it would do.

`release.yml` handles a hand-pushed tag (the major-bump path). `make release`
is only for when the workflow didn't run — running both on one tag makes the
second fail.
