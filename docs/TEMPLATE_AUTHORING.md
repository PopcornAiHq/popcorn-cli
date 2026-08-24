# Authoring a channel template

A **channel template** is a directory of YAML that turns an empty Popcorn
channel into an application: tables to hold state, flows to do work, schedules
and webhooks to invoke them.

> **Which path you are on decides how fast you can iterate.** A *new* app type
> is still a backend PR: installable templates are a fixed set checked into the
> backend repo and published to the registry from inside the VPC. But *editing*
> an app that already exists is now a pure CLI loop — `popcorn app fork`,
> `checkout`, `publish` — with no deploy in it. Both paths are §2.
> `popcorn flow import` is gone and neither path replaces it.
>
> Everything else in this guide applies to both: the grammar, the manifest
> semantics, and `popcorn template check` are about the bundle itself, not
> about how it gets installed.

This guide is what you need that the API cannot tell you. It deliberately does
**not** list activities or their arguments — those come from the server and
would rot here:

```bash
popcorn flow activities --tier foundation      # what can I call?
popcorn flow activities --json                 # full arg + result schemas
popcorn flow validate my_flow.yaml             # is this reference real?
popcorn template check ./mytemplate            # does the bundle hold together?
```

`flow validate` is the authority on a reference. When this guide and the
validator disagree, the validator is right and this guide has a bug.

`template check` answers a different question, offline and with no channel:
will the importer install what you think, and do the files agree with each
other? Everything it reports passes `flow validate` cleanly — a fixture named
`.yaml`, a write to an undeclared column, a schedule naming a flow that is not
there. Run both; neither subsumes the other.

Two complete, live-verified templates are checked in:

| Bundle | Producers | Per-delivery cost |
|---|---|---|
| [`examples/alerttracker/`](../examples/alerttracker/) | four, unrelated | one LLM call — it must *derive* severity and env |
| [`examples/deploywatch/`](../examples/deploywatch/) | one (GitHub) | none — it *extracts* fields the payload states |

The gap between them is §6, and it is the single most consequential choice in
a webhook-backed template. `alerttracker`'s `GOTCHAS.md` is the raw evidence
log behind most of the rules below.

---

## 1. What a bundle is

```
mytemplate/
├── manifest.yaml        tables, schedules, webhooks, config, scalars
├── AGENT.md             notes injected into the channel agent's prompt
├── README.md            human docs (not installed as anything)
├── some_flow.yaml       one flow per file
└── fixtures/*.json      sample payloads — NOT installed (see §2)
```

Only `manifest.yaml` is meaningful on its own; everything else is optional. A
bundle with one flow and no manifest is a legal template.

## 2. How a bundle gets installed

**Two paths, and which one you are on depends on whether the app already
exists.** Creating a new app type is still a backend deploy. *Changing* one is
now entirely a CLI loop.

### 2a. A new app type — backend deploy

Only the first step is yours to write:

```
your bundle dir                                    (author here)
      │
      ├─▶ popcorn-backend  lib/temporal/flows/<name>/
      │   + register the name in templates.py — CHANNEL_TEMPLATES
      │
      ├─▶ deploy the backend                       (workers must exist first)
      │
      ├─▶ intranet /app-bundles → Publish          (runs inside the VPC)
      │
      └─▶ popcorn channel create '#chan' --template <name>
```

Publish **after** the deploy. A bundle whose flows call a new activity must not
become installable before the workers that can run it exist; nothing enforces
that ordering for you.

There is still no endpoint that takes a *new* app you have locally:
`publish_registry_template` reads from disk and `channel create --template`
resolves against the hardcoded `CHANNEL_TEMPLATES`, so a genuinely new
`app_type` needs the PR.

### 2b. Changing an app that exists — `popcorn app`

A channel already running a bundle can be edited from the CLI, with no deploy
and no intranet visit:

```bash
popcorn app fork --channel '#chan'      # this workspace's own fork line
popcorn app checkout --channel '#chan'  # the bound version, as files
# ... edit, then bump version: in manifest.yaml
popcorn template check ./<app>
popcorn app publish ./<app> --changelog "what changed"
popcorn app status ./<app>              # has the install landed?
```

`fork` comes first and is not optional: a publish lands on a fork line **this
workspace owns**, so publishing from a channel still bound to the shared
product version is refused. `app publish` also starts the install that moves
your channel onto the new version.

Three things about this loop that are easy to get wrong:

- **`version:` in `manifest.yaml` must advance every publish.** Bundle versions
  only ever move forward; the CLI refuses a reused or lower number locally
  rather than letting the registry 409 at you.
- **A publish is not scoped to the channel you tested on.** Every other channel
  on the same fork line catches up on its own nightly auto-update tick.
- **One fork line per (workspace, app).** There is no parallel-experiment path
  without naming a second line (`app fork --name`).

`popcorn app list --channel '#chan'` shows the product line, any fork line this
workspace owns, and what the channel currently runs.

### The app list is filtered by your release track

`popcorn app list` and `popcorn channel templates` show what **your workspace's
release track** can see — alpha, beta or stable, per-workspace and defaulting
to stable. An app released only to alpha is invisible and uninstallable for a
workspace enrolled on stable.

**The API says nothing about why**, deliberately: enrollment is internal, and a
test enforces that it does not leak. So `channel create --template X` returns a
neutral 400 for an app your track cannot see, indistinguishable from a typo.
The CLI cannot diagnose this and does not try. If an app you know shipped is
missing from the list, that is the first thing to suspect — ask rather than
debug.

### What the reader does with your files

Two readers, one classification path: the registry reads your directory off
disk, and `read_zip` reads an uploaded archive (retained for a future upload
transport). Both then:

- **Reserves four names**: `manifest.yaml`, `AGENT.md`, `README.md`, and
  `strings.yaml` (client copy — `config.yaml` is a legacy manifest alias).
  Every template the backend ships has a `strings.yaml`.
- **Treats every other `.yaml` / `.yml` as a flow.** This is why fixtures must
  be `.json` — a sample payload named `.yaml` can be installed as a flow.
- **Descends `prompts/` and `templates/` only**, one level, seeding
  `$channel.prompts.<stem>` and `$channel.templates.<stem>`.
- **Silently skips everything else**, including dotfiles and `__MACOSX`.

Where they diverge — **keep the bundle flat and it never matters:**

| | registry (on-disk) | `read_zip` (archive) |
|---|---|---|
| `flows/a.yaml` | **ignored** — the dir is not descended | flattened to `a.yaml` |
| two files, same basename | both kept (different paths) | second one wins |
| entry over 1 MiB | no limit | rejected |

A nested flow is silently *dropped* by one reader and silently *flattened* by
the other. Neither tells you. `template check` flags the nesting.

**Flow identity is the `name:` inside the YAML, not the filename.** Install
upserts by name. Renaming `name:` creates a second flow and leaves the first
one installed — and install **prunes** flows the bundle no longer covers, so a
rename that you meant as a rename reads as one delete plus one create.

## 3. Manifest keys

Every key is optional. The install semantics differ per key and getting this
wrong is how you wipe a live channel's state.

| Key | Semantics | Notes |
|---|---|---|
| `display_name`, `description` | catalog copy | for the template picker |
| `version` | bundle semver | **required to publish**; shape-validated when present |
| `app_type` | sets the channel's app | **see the warning below** |
| `tables` | **additive reconcile** | columns are added and attributes fixed, never dropped or renamed |
| `channel_parameters` | upsert, **types preserved** | read as `$channel.<name>` |
| `scalars` | **UPSERT on every install** | never put flow-written runtime state here |
| `default_scalars` | **write once, first install only** | safe place for an operator-owned switch |
| `schedules` | **REPLACE wholesale** | omitted = leave alone; `[]` = delete all |
| `webhooks` | **create-if-missing** | never updated or deleted |
| `triggers`, `connections`, `documents`, `status_kinds` | replace, omit-vs-empty | `[]` means "replace with nothing" |

The `*_declared` distinction runs through all of them: **an absent key leaves
the channel alone; a present-but-empty key means "replace with nothing."**
`schedules: []` deletes every schedule. Omitting `schedules:` does not.

> **An untyped bundle CLEARS the channel's `app_type`.** A manifest with no
> `app_type:` key strips whatever was there, which changes the client's whole
> interface paradigm. Never import an untyped bundle into a channel running a
> real app. `--dry-run` warns you.

### Runtime state must not appear under `scalars:`

`scalars:` upserts on *every* install. If a flow writes a `last_swept_at` key
and the manifest also declares it, every re-import resets it. Declare only
install-time configuration; let flows create their own runtime keys.
`template check` warns (`runtime-state-in-scalars`) when it sees a flow write
a scalar the manifest declares.

### Schedules address their own channel

Schedule `inputs` support two substitutions applied at install:
`<channel-conversation-id>` and `<workspace-id>`. That is how a portable
bundle passes its own channel's id into a scheduled flow.

```yaml
schedules:
  - flow: alert_tick
    slug: alert-tick
    interval: 300          # seconds; or cron: "0 8 * * *" + timezone:
    overlap: skip
    jitter: 30
    inputs:
      conversation_id: <channel-conversation-id>
```

## 4. Flow grammar

```yaml
name: my_flow             # identity — not the filename
version: 1
description: >
  What this does and why it is shaped this way.

inputs:
  conversation_id: { type: string }
  thing: { type: string, required: false, default: "" }

steps:
  - id: fetch
    activity: foundation.store.list_rows
    when: $inputs.thing != ''
    on_error: { policy: skip, retry: 1 }
    args: { ... }

  - id: each
    activity: foundation.store.patch_row
    foreach: $steps.fetch.output.rows
    as: row
    max_parallel: 1
    collect: patched
    args:
      record_id: $row._record_id

outputs:
  ids: $steps.fetch.output.rows
```

### Reference roots

| Root | Reads |
|---|---|
| `$inputs.*` | the run's declared inputs |
| `$steps.<id>.output.*` | a prior step's result |
| `$steps.<id>.<collect>` | a `foreach` step's collected list, under the name `collect:` gave it |
| `$channel.*` | channel config — parameters, scalars, and the seeded `integrations` / `integration_list` |
| `$trigger.*` | what triggered the run |
| `as:` name | the current item inside a `foreach` |

`$trigger` carries exactly `thread_id`, `message_id`, `conversation_id`,
`thread_root`, `contact_id`, `workflow_id`, `run_id` — a closed set, so a typo
in it is caught offline. `$channel` is not: its keys come from a per-channel
config the bundle cannot see, which is why an unrecognized one is a warning.

**A `foreach` alias shadows every global root.** The interpreter resolves
aliases first, deliberately, so `as: channel` or `as: trigger` keeps working.

**Index arrays with dots, never brackets**: `$steps.x.output.ids.0`. Brackets
are rejected as malformed and would be treated as a literal string.

**A reference path is `[A-Za-z_][A-Za-z0-9_.]*` — no spaces.** This matters
more than it sounds; see §5.

### `when:` has four rails, and the seam between two of them bites

Routing is **legacy-first**: a string the legacy parser accepts stays legacy
forever, even if it contains new-syntax tokens.

| Form | Rail | Semantics |
|---|---|---|
| `$ref` | legacy | truth-tested |
| `$ref == value`, `$ref != value` | legacy | **lenient** equality; a skipped step is `None` and compares unequal |
| anything with `&&` `\|\|` `<` `<=` `>` `>=` `!` `(` `)`, or a second comparison | expression | **strict, typed** |
| a plain literal | legacy | truthy as-is |

The expression grammar is `!`, `&&`, `||`, `==`, `!=`, `<`, `<=`, `>`, `>=`,
parentheses; operands are `$ref`s, numbers, `true`/`false`, and quoted strings.
Precedence is parens → `!` → comparisons → `&&` → `||`. Bounds: depth ≤ 8, ≤ 32
conditions, ≤ 8 terms per chain, literals ≤ 1024 chars. No functions, no
arithmetic, no `${}` interpolation.

> **The seam.** A standalone `$a == 1` is *lenient* legacy equality; the same
> comparison inside `$a == 1 && $b != ''` is *strict and typed* on the
> expression rail. Adding a second condition can therefore change how the first
> one compares. And a string that routes to the expression rail but fails to
> parse is an **error** — never a fallback to literal truthiness.

`template check` does not police any of this: mirroring the routing rule
offline means reimplementing the predicate parser, and a near-miss
reimplementation reports valid clauses as broken (the earlier "one comparison
only" rule rejected 55 valid clauses across the shipped backend templates). It
checks the references inside a `when:` and leaves the grammar to `flow
validate`, which calls the real parser.

**Even so, prefer a query filter to a gate when you are selecting rows.** The
agent-store filter DSL runs in the database rather than after the fact:

```yaml
filter:
  Status: firing                                  # shorthand for $eq
  Last Seen: { $lt: $steps.cutoffs.output.iso }   # $gt $gte $lt $lte
  Nudged At: { $exists: false }                   # $in, $contains too
```

Ordering comparisons on ISO-8601 strings are correct because they are
lexicographic — in a filter *or* on the expression rail. The difference is
where the work happens: a filter selects rows in the database, while a `when:`
gate can only skip a step you already decided to run.

### `foreach`

`foreach:` takes a list, `as:` names the item, `collect:` names the result
list, `max_parallel:` bounds concurrency. A step-level `when:` on a `foreach`
step is **re-evaluated per item** in that item's scope, so `when: $row.Status
== 'firing'` filters items rather than skipping the whole step.

### A step is exactly one of four things

`activity:`, `sleep_seconds:`, `await_approval:`, or a nested `steps:` block.
Exactly one — the model rejects a step with two, or none.

```yaml
  - id: maybe                     # a BLOCK: `when:` gates the whole group
    when: $steps.check.output.ok
    steps:
      - id: inner
        activity: foundation.channel.post
        args: { ... }
    outputs:
      posted: $steps.inner.output   # the ONLY thing outside can read
```

A block's scoping is the part worth internalizing:

```
outer step  ──▶ sees $steps.maybe.output.posted        ✅ (a declared output)
            ──▶ sees $steps.maybe.output.anything_else ❌
            ──▶ sees $steps.inner.*                    ❌ (private to the block)

inner step  ──▶ sees $inputs, $channel, $trigger, and every
                enclosing step at every level           ✅
```

Inner ids are private, so two blocks may reuse the same id. `outputs:` is
evaluated in the block's inner scope after its steps ran, and is legal only
alongside `steps:`. Omit it and the block publishes nothing — the same as a
skipped step.

`sleep_seconds:` is a durable timer (survives worker restarts, holds no worker
capacity) and cannot be combined with `foreach`. Neither can `await_approval:`,
which is a single blocking per-workflow gate — fanning it out would key every
iteration to the same signal.

### Errors and retries

Retry is flow-owned. **No `on_error` means up to 4 attempts** — any
non-idempotent step must set `retry: 0`. `policy: skip` continues the flow;
`policy: fail` stops it.

### There is no arithmetic

Nothing in the DSL adds, subtracts, counts, or compares magnitudes. There is
no expression syntax: you cannot write `$a + $b`, and you cannot even negate a
reference — `-$channel.minutes` is parsed as a literal string and fails type
validation. Consequences:

- **Never design a counter column.** Accumulate with a `merge: concat` string
  column and let readers count entries.
- **A sign cannot be applied by a step.** If an activity takes a *signed*
  value, the sign must be baked into the configured data — no step can flip
  it. Prefer an activity that names the direction as its own argument, which
  is why `math.offset` takes `direction: subtract` rather than a negative
  duration.

**Time windows are the exception, and they have a real activity.**
`foundation.math.offset` shifts a timestamp by a duration and returns
`workflow.now`'s `{unix, unix_str, iso}` shape, so its output drops straight
into a filter:

```yaml
  - id: cutoff
    activity: foundation.math.offset
    args:
      iso: $steps.now.output.iso     # omit to shift from now
      direction: subtract            # durations stay non-negative
      hours: 6

  - id: stale
    activity: foundation.store.list_rows
    args:
      filter:
        Last Seen: { $lt: $steps.cutoff.output.iso }
```

Pass `iso` explicitly when several cutoffs must derive from the same instant.
`examples/alerttracker/alert_tick.yaml` does exactly this, and is fully
deterministic as a result — it previously spent an LLM call per run on the
subtraction.

## 5. Table schemas

Declared under `tables:` in the manifest; reconciled additively on every
import.

```yaml
tables:
  alerts:
    columns:
      - { name: Fingerprint, type: string, unique: true }
      - { name: Seen At, type: string, merge: concat }
      - { name: Raw, type: json, internal: true }
    merge_key:
      any_of: [Fingerprint]
      on_conflict: merge
```

`type` is `string | number | boolean | datetime | json`. `format` validates on
write; `display` only hints at rendering and is **never validated** — a
misspelled display arg fails silently by simply not rendering.

Governance flags (`pii`, `restricted`, `internal`, `passthrough`,
`masked_read`) are independent booleans. `internal: true` hides a column from
the user-facing UI — right for raw payloads and bookkeeping ids.

### Merge policy

Every column is `merge: replace` (last write wins). The **only** other mode is
`merge: concat`, which appends to a string column with a separator.

There is no "keep the existing value" policy. So a "first time we saw this"
column must never be written by the upsert — write it in a separate step gated
on `created == 1`, or every re-fire overwrites it.

`merge: concat` requires `type: string`. A timestamp history cannot be
`datetime`.

Check what is actually installed with:

```bash
popcorn table schema alerts --channel '#chan'
#   Seen At    string   [concat]
```

### Merge keys

`merge_key.any_of` columns must be **indexed** (`unique: true` satisfies it)
and **string-typed** — the OR-probe only queries the text index, so a
non-string merge key silently never matches.

### Name columns without spaces if a flow reads them

A column name may contain spaces if flows only ever *write* it (YAML map keys)
or *filter* on it (JSON keys). It must be space-free if any flow
**dereferences** it, because `$row.Last Seen` is not parseable:

```
steps[1](touch).args.text: malformed reference '$row.Last Seen'
```

Renaming later is not free — the installer is additive and never renames, so a
rename *adds* a column and orphans the old one.

**And the store accepts undeclared columns.** Writing `Post Message Id` when
the schema says `PostMessageId` silently succeeds and produces a column no
`$ref` can reach. Renaming a column means renaming every write site; only
reading a row back catches a miss.

## 6. Reading fields off an object input

The DSL cannot reach sub-fields of an `object` input — `$inputs.payload.name`
is statically unreachable, because an input declaration has no way to describe
an object's properties. To read fields off a webhook payload you must first
obtain a **typed** shape, and the only mechanism is an activity that declares
its output schema at the call site.

Two of those exist. **Reach for the deterministic one first.**

### `foundation.fields.extract` — when the fields are simply there

Names fields by path. No model, no prompt, and an absent path is an error
rather than a guess:

```yaml
  - id: fields
    activity: foundation.fields.extract
    args:
      data: $inputs.payload
      mapping:
        alarm: AlarmName
        service: Trigger.Dimensions.1.value    # dots for nesting AND indices
      defaults:
        note: ""                               # only for genuinely optional fields
      output_schema:
        type: object
        required: [alarm, service]
        properties:
          alarm: { type: string }
          service: { type: string }
```

`$steps.fields.output.alarm` then resolves statically, so `flow validate`
checks it. [`examples/deploywatch/`](../examples/deploywatch/) is a whole
bundle built this way — a single producer, every field read by path, and not
one model call in it.

Three behaviours worth knowing:

- **An unresolvable path fails the step** (`ExtractPathNotFound`), listing
  every bad path at once. That is the point — it is how a flow *requires* a
  field to be present rather than accepting something invented in its place.
  Posting a GitHub ping at `deploywatch`'s intake webhook returns exactly
  this, and writes nothing:

  ```
  ExtractPathNotFound: fields.extract could not resolve:
    creator <- deployment_status.creator.login;
    deployment_id <- deployment.node_id;
    environment <- deployment_status.environment; …
  ```

  Compare `agent.transform` given the same junk: it fabricates a row. The
  guard is structural here rather than a step you must remember to write.
- **`null` is a value, not an absence.** A stored null passes through; only a
  genuinely missing path errors.
- **The result is validated against `output_schema`** (`ExtractSchemaViolation`),
  so a value contradicting the type you declared fails here rather than in
  whichever later step consumes it.

### `foundation.agent.transform` — when the answer must be derived

Still the right tool when the payload does not *state* what you need — one
flow normalising several unrelated producers, a severity no field carries, a
human-readable title, prose. Judgement, not lookup.

The dividing line is worth applying literally, because it decides your risk:
extraction reads what is there, derivation invents what is not. A CloudWatch
alarm body has no `severity` and no `env` key at all, so those must be derived;
its `AlarmName` and `NewStateValue` are right there and should not be.

The two shipped examples are the same decision answered both ways, and the
input decides it, not taste:

| | `deploywatch` | `alerttracker` |
|---|---|---|
| producers | one | four, unrelated |
| identity | `deployment.node_id`, stated | `source:resource:env`, composed |
| environment | `deployment_status.environment`, stated | absent from a CloudWatch body — derived |
| tool | `fields.extract` | `agent.transform` |
| cost | none | one LLM call per delivery |
| junk input | fails, names every missing path | invents a plausible alert unless a `recognized` guard stops it |

Narrowing a bundle to one producer is therefore not just a scope decision —
it is what makes the deterministic tool available at all.

When you do need it, three rules, each paid for in a live failure:

**1. A required output schema is a formatting contract, not a validation
gate.** Given junk, the model *invents* a plausible object to satisfy
`required`, and `enum` merely constrains which lie it tells. Add an explicit
recognition flag and hard-stop on it:

```yaml
  - id: guard
    activity: foundation.workflow.fail
    when: $steps.normalize.output.recognized == false
    args: { reason: ..., code: UnrecognizedPayload }
```

**2. Every property you dereference must be in `required:`.** A declared-but-
optional property is genuinely optional — the model returned `details` on one
run and omitted it on the next, and a missing key is a hard failure:
`ReferenceError: $steps.normalize.output.details: key not found`.

**3. Keep the prompt and the schema consistent.** Telling the model to blank a
field whose `enum` excludes `""` is a contradiction, and it does not fail
cleanly — the model reasons about the conflict *in its output* and breaks JSON
parsing.

Finally, know which kind of transform you are writing. Mapping fields is
risky; **writing prose is not**. A digest whose wording drifts costs nothing,
because nothing downstream parses it.

## 7. The authoring loop

Three loops now, and picking the right one is most of the speed. The **inner**
loop is offline and runs as often as you like:

```bash
popcorn template check .                             # no channel, no server
popcorn flow activities --tier foundation            # what can I call?
popcorn flow validate my_flow.yaml --channel <id>    # per file, fast
```

`flow validate` needs a channel only as an auth/context handle — any channel
you can reach will do; it never writes.

The **middle** loop is the fork loop from §2b, and it is the one to reach for
whenever the app already exists. No deploy, no intranet, seconds per turn:

```bash
popcorn app publish ./<app> --changelog "..."        # mint the next version
popcorn app status ./<app>                           # has the install landed?
popcorn channel-config show --channel <id> --strict  # is the channel wired up?
popcorn flow runs list --channel <id>
```

`channel-config show` is worth running the first time a bundle installs: it
diffs every `$channel.*` reference your flows make against what the channel
actually has, and `--strict` exits non-zero on the three findings that make a
run fail — a referenced parameter that is not set, a declared integration that
is not connected, and a connected account whose provider contradicts a
declaration. The two `unused_*` findings are informational; a shared config
legitimately carries keys one flow does not read.

The **outer** loop is only for a NEW app type, and costs a backend deploy plus
a publish (§2a), so get the inner one clean first:

```bash
# ... land the bundle in popcorn-backend, deploy, publish from /app-bundles

popcorn channel templates                            # is my version installable?
popcorn channel create '#chan' --template mytemplate # note the UUID — see below

popcorn webhook list <id>                            # copyable URL
curl -X POST <url> -d @fixtures/sample.json

popcorn flow runs list --channel <id>
popcorn flow runs get <workflow-id> --channel <id> --include-errors
popcorn table rows alerts --channel <id>
popcorn table schema alerts --channel <id>
```

Because the outer loop is expensive, a bundle that installs but is wrong costs
a whole deploy cycle to correct — which is the argument for `template check`
and `flow validate` being pedantic, and for exercising boundaries (below) the
first time you get a real channel rather than the third.

That argument is weaker than it was: once the app exists, a wrong bundle costs
a `app publish` rather than a deploy. It is not gone, though — the *first*
version of a new app still has to be right, and a fork publish still moves
every other channel on the line.

A freshly created channel is **not resolvable by `#name` for ~5 minutes**
(negative resolution caching). Use the conversation UUID immediately after
creating it.

`flow run` accepts a flow **name or UUID**, and defaults `conversation_id`
into the inputs from `--channel`:

```bash
popcorn flow run alert_tick --channel <id> --wait
```

Pass `--inputs` for a flow's own arguments; an explicit `conversation_id`
there always wins, so a flow can still target another conversation.

```bash
popcorn flow run seed_test_alert --channel <id> \
  --inputs '{"severity":"critical","env":"prod"}' --wait
```

### Checks will not save you

Every defect found while building the example bundle passed `flow validate`
cleanly, because they were runtime semantics rather than bad references: a
merge policy overwriting a first-seen timestamp, an LLM inventing a row, an
optional schema property going missing, a write to an undeclared column.

`template check` was written from that list and now catches the last two —
plus the whole class of cross-file mistakes a per-file validator cannot see:

```bash
popcorn template check .            # errors exit non-zero
popcorn template check . --strict   # warnings do too — this is the CI form
```

It cannot catch the first two. A merge policy is only wrong relative to what
you meant, and no offline tool knows an LLM is about to invent a row.

**So install it and run it.** `seed_test_alert.yaml` and
`seed_test_deploy.yaml` exist purely so a bundle can be exercised without a
real producer.

And exercise the **boundary**, not the happy path. A sweep that resolves
everything Completes just as cheerfully as a correct one; the only proof a
cutoff works is that a row just inside it moves and a row just outside it does
not. Both example bundles were verified that way.

Watch for these when reading results:

- Many activities have permissive result schemas (`additionalProperties:
  true`). For those, `$steps.x.output.anything` **passes validation and
  resolves to nothing at runtime**. Verify against the real response, not the
  catalog.
- `start_flow` is asynchronous. A parent that launches a child reports
  Completed immediately; check the **child's** run.
- Posting the same webhook body twice does not test your merge logic — the
  *webhook layer* dedupes identical deliveries and no flow runs at all. Vary
  the body while keeping the identity fields.
- A field that might be **absent** must never be dereferenced. A missing key
  is a hard `ReferenceError` that fails the run, and `on_error` does not
  rescue it — reference resolution happens before the activity is invoked.
  Guarantee presence upstream: in an `output_schema`'s `required`, or with
  `$exists: true` in the query that produced the rows.

## 8. Gotchas, condensed

1. `$inputs.<object>.field` is statically unreachable — get a typed shape
   first, with `fields.extract` when the fields are there and
   `agent.transform` when the answer must be derived.
2. `workspace_id` is never an input; it rides the auth context.
3. `scalars` UPSERT every install, `schedules` REPLACE wholesale,
   `default_scalars` write once. Runtime state belongs in none of them.
4. Flow identity is `name:`, not the filename.
5. Zips flatten to basenames, except `prompts/` and `templates/`.
6. Webhook-triggered flows get `{conversation_id, payload, headers,
   source_hint, delivery_id, webhook_id}`, and only in `trigger_workflow` mode.
7. `<channel-conversation-id>` / `<workspace-id>` are substituted in schedule
   inputs.
8. No `on_error.retry` means up to 4 attempts.
9. Table changes are additive — never dropped, never renamed.
10. `app.*` activities are private to shipped apps. Author against
    `tier: foundation|feature`, `status: release`.
11. Scalars are strings on the wire; `channel_parameters` keep their types.
12. **No arithmetic anywhere**, and no negating a reference. Never design a
    counter; use `foundation.math.offset` for time windows.
13. **An untyped bundle CLEARS `app_type`.**
14. A `.yaml` anywhere in the zip becomes a flow. Fixtures are `.json`.
15. Permissive output schemas validate any path.
16. Index arrays with dots, never brackets.
17. `when:` is one `==`/`!=` comparison. Real predicates go in `filter`.
18. A column name with a space cannot be dereferenced.
19. The store accepts undeclared columns silently.
20. Every `output_schema` property you reference must be `required`.
21. A missing key is a hard `ReferenceError`, and `on_error` cannot rescue it —
    resolution precedes invocation. Guarantee presence in the query
    (`$exists: true`) or the schema (`required`).
