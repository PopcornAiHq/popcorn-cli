# Alert Tracker — authoring gotchas (Step 0′ evidence log)

Everything here was observed against **dev** on 2026-08-07 while building the
ingest slice. Feeds the Step 5 authoring guide.

The whole slice was authored, validated, installed and debugged **without the
`popcorn api` escape hatch** — `flow activities`, `flow validate`,
`flow import --dry-run`, `flow runs list/get`, `table rows/row/scalar` covered
every step. That was the point of Tasks 4–11.

---

## The two defects that only a live run could find

Static validation passed on the first try, and the flow was still wrong twice.
Both bugs were invisible to `flow validate` because they live in *runtime
semantics*, not references.

### 1. `First Seen` was overwritten on every re-fire

The flow wrote `First Seen` in the `upsert` row. Every column defaults to
`merge: replace` and the only other mode is `concat` — **there is no "keep the
existing value" policy** — so each merge overwrote it:

```
fire 1: First Seen 23:14:38   Last Seen 23:14:38
fire 2: First Seen 23:15:47   Last Seen 23:15:47   ← should not have moved
```

**Fix:** drop `First Seen` from the `upsert` row and set it in the `stamp`
step, which is already gated on `created == 1`. Written once, on creation.

Generalizes: **any "first time we saw X" column must be written by a
`created == 1`-gated step, never by the upsert itself.**

### 2. `agent.transform` fabricates data rather than failing

`{"garbage": true}` produced a complete, plausible, entirely invented alert:

```json
{"Fingerprint": "manual:unknown:dev", "Title": "Unknown Alert",
 "Source": "manual", "Severity": "info", "Env": "dev",
 "Resource": "unknown", "Status": "firing",
 "Details": "No recognizable alert payload provided."}
```

The run **Completed**. A junk row landed in the table.

An `output_schema` with `required:` and `enum:` does **not** reject bad input —
it *coerces* it. The model satisfies the contract by inventing values, and
`enum` just constrains which lie it tells. My own prompt made it worse by
saying "if you cannot find a resource, use 'unknown'", which licensed exactly
this.

**Fix:** add a `recognized: {type: boolean}` field to `output_schema`, instruct
the prompt to set it false and blank every other field for non-alerts, then
hard-stop:

```yaml
  - id: guard
    activity: foundation.workflow.fail
    when: $steps.normalize.output.recognized == false
    args:
      reason: Payload is not a recognizable ops alert; nothing was ingested.
      code: UnrecognizedAlertPayload
```

After: no row, and the run is `Failed`.

Generalizes: **every `agent.transform` that ingests untrusted input needs an
explicit recognition flag plus a `workflow.fail` guard.** Treat a required
output schema as a formatting contract, never as a validation gate. This is
direct evidence for spec open question 7 (`agent.transform` stability) — the
instability that matters is not field drift, it is confident fabrication.

---

## Contract corrections (plan text vs. reality)

| Plan said | Reality |
|---|---|
| `display: "…;tone=critical:black\|info:gray"` | `tone=` is not in the display grammar. The documented segments are `warn=`, `labels=`, `sub=`. Display args are advisory and not server-validated, so a bogus segment fails **silently** — it just never renders. Used `warn=critical`. |
| — | `merge: concat` requires `type: string`. `Seen At` cannot be a `datetime`. |
| — | `merge_key.any_of` columns must be indexed (`unique: true` satisfies it) **and** string-typed — the OR-probe only queries `RecordIndex.value_text`, so a non-string merge key silently never matches. |
| POST the same fixture twice to test dedup | **This does not test the flow.** An identical delivery body is deduplicated at the *webhook* layer: `{"status":"ok","deduplicated":true}`, HTTP 200, **no flow run at all**. To exercise `merge_on` you must vary the body while keeping the alarm identity (change `StateChangeTime` / `NewStateReason`, keep `AlarmName` + `Dimensions`). |

## Confirmed as documented

- `foundation.workflow.now` → `{unix, unix_str, iso}`. Use `.iso`; there is no `.now`.
- `foundation.channel.post` → a **bare string** message id. `$steps.post.output`, never `.message_id`.
- Array indexing uses **dots**: `$steps.upsert.output.ids.0`. Brackets are rejected.
- `foundation.store.upsert_rows` really does return `{ids, created, updated, schema_version_id}` — but its `result_schema` is `additionalProperties: true`, so **static validation cannot confirm it**. 15 of 143 activities are permissive this way; for those, `.output.anything` validates and may resolve to nothing. Verify against the runtime response model, not the catalog.
- An untyped bundle (no `app_type:`) leaves `app_type: null`. `flow import --dry-run` now warns before you commit.
- Fixtures must be `.json` — a `.yaml` anywhere in the zip becomes a flow.

## Rough edges worth filing

1. **`flow runs get --include-errors` does not name the failing step id.** It
   surfaces the reason and code (`UnrecognizedAlertPayload`, "Payload is not a
   recognizable ops alert") but the `guard` step id appears nowhere in the
   response. Criterion 8's "names the failing step" is only half met.
2. **`table schema` does not show a column's merge policy.** `Seen At` renders
   as a plain `string`; nothing indicates `merge: concat`. That is exactly the
   property an author needs to see, and defect #1 above was a merge-policy
   bug. Worth adding to the `table schema` renderer.
3. A freshly created channel is unresolvable by `#name` for ~5 minutes
   (negative resolution caching). Use the conversation UUID right after
   `channel create`.

---

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | `alerts` table + `alert_webhook` flow + `Alerts` webhook in `trigger_workflow` mode | **pass** — 18 columns, webhook bound to the installed flow id, `app_type` left `null` |
| 2 | One row, `firing`, one `Seen At`, populated fingerprint/severity/env, non-empty `Raw` | **pass** |
| 3 | Re-fire merges: one row, second `Seen At`, `Last Seen` advanced, `First Seen` unchanged, no second message | **pass after fix #1** |
| 8 | Garbage payload: no new row, run visible as failed | **pass after fix #2** |

### Verbatim final row

```json
{
  "Fingerprint": "cloudwatch:popcorn-dev-ops-api:dev",
  "Title": "ECS CPU High: popcorn-dev-ops-api CPU at 96.5% (threshold 85%)",
  "Source": "cloudwatch",
  "Severity": "critical",
  "Env": "dev",
  "Resource": "popcorn-dev-ops-api",
  "Status": "firing",
  "First Seen": "2026-08-07T23:17:13+00:00",
  "Last Seen": "2026-08-07T23:17:44+00:00",
  "Seen At": "2026-08-07T23:17:13+00:00\n2026-08-07T23:17:44+00:00",
  "Post Message Id": "019fde84-42c2-7399-b835-f4adb6b070b1"
}
```

`created`/`updated` behaved exactly as the spec predicts: `created == 1` only
for a genuinely new fingerprint, which is what gates the single channel post
per incident. The fingerprint the model derived
(`cloudwatch:popcorn-dev-ops-api:dev`) was stable across both fires.

## Environment

- Channel `#alerts-dev` = `567f1fcb-1a23-415c-91ec-502fe6c220a2`, workspace
  `Popcorn` (`ce0467aa-e67f-4081-b52d-a9ccfac56520`) on dev. Dedicated, as the
  untyped bundle requires.
- CLI v0.13.0.
- `fixtures/cloudwatch-alarm.json` is the CloudWatch **notification body**
  (what SNS puts inside `Message`), not the SNS envelope. Real SNS wiring —
  including the `SubscriptionConfirmation` handshake and the nested
  JSON-string `Message` — is deferred per the plan's "Not in this plan".
  Account id is redacted to `123456789012`; this bundle is destined for the
  public repo.

---

# Step 4 findings (2026-08-09)

Three platform constraints found while writing the remaining flows. All were
confirmed against the real validator or a live query, not by reading alone.

## 18. `when:` has only `==` and `!=`, and exactly one comparison

`backend:lib/temporal/dsl/spec.py:110` — the grammar is
`^(?P<lhs>\$ref)\s*(==|!=)\s*(?P<rhs>.+)$`. There is no `<`, `>`, `<=`, `>=`,
and `&&`/`||` are explicitly rejected as compound expressions. A step gate can
express "equals this value" and nothing else.

## 19. A column name containing a space is unreferenceable

`_REF_RE`'s path is `[A-Za-z_][A-Za-z0-9_.]*` — no spaces. Confirmed live:

```
$ popcorn flow validate probe1.yaml
steps[1](touch).args.text: malformed reference '$row.Last Seen'
  — not valid reference syntax
```

**But filters are JSON, not refs**, so `{"Last Seen": {"$lt": "..."}}` is fine.
The rule is therefore narrow: a column may keep its spaces if flows only ever
*write* it (YAML map keys) or *filter* on it; it must be space-free only if a
flow dereferences it as `$row.<name>`. Here that is exactly one column, so
`Post Message Id` became `PostMessageId` and the rest kept their spaces.

Renaming is not free on a live channel: the installer is additive and never
renames, so a rename ADDS a column and orphans the old one. Do it on a fresh
channel, or accept both.

## 20. The agent-store filter DSL is far more capable than the DSL itself

`backend:services/agent_store/filter_dsl.py` allows
`$eq · $ne · $gt · $gte · $lt · $lte · $in · $exists · $contains`, and ordering
comparisons on ISO-8601 strings are correct because they are lexicographic.
Verified live against `#alerts-dev`:

```
--filter '{"Last Seen":{"$lt":"2030-01-01T00:00:00+00:00"}}'  → 1 row
--filter '{"Last Seen":{"$lt":"2020-01-01T00:00:00+00:00"}}'  → 0 rows
```

**This is the workaround for gotcha 18.** Selection that the `when:` grammar
cannot express belongs in `list_rows`' `filter`, where the database does it
exactly. Push predicates into the filter; keep `when:` for equality gates.

## 21. Nothing in the DSL can do date arithmetic — the real gap

All 44 foundation activities were enumerated. `workflow.now` takes no
arguments and returns `{unix, unix_str, iso}`; there is no activity that adds
or subtracts a duration, and gotcha 12 already established there is no
arithmetic anywhere. So "older than N hours" cannot be computed by a flow.

The only in-repo precedent, `app.delivery.pool.prune`, does its age cutoff
inside a bespoke Python activity — an `app`-tier activity a portable bundle
cannot call (gotcha 10).

`alert_tick` therefore spends one `agent.transform` per run turning
`{now, nudge_after_minutes, auto_resolve_hours}` into two ISO cutoff strings,
guarded like every other transform (gotcha: fabrication). Everything after
that is exact: the cutoffs are only ever used as filter operands, so the
database performs the actual selection and the LLM's blast radius is one
string. A wrong cutoff affects one tick and self-corrects on the next.

**This is a platform gap, not a bundle quirk.** A `foundation.workflow.offset`
activity (`{iso, minutes|hours}` → shifted ISO) would make every time-window
flow fully deterministic and remove the last LLM call from the tick. It is the
strongest candidate for the "Related platform work" DSL spec — stronger than
typed object inputs, because this one has no workaround at all.

## 22. A declared-but-not-required `output_schema` property is genuinely optional

`details` was in `properties` but omitted from `required`. The model returned
it on one run and not the next, and the absence is a **hard failure**, not a
null:

```
ReferenceError: $steps.normalize.output.details: key not found
```

Rule: every property any downstream step references must be in `required`.
This is also the cleanest evidence yet for spec open question 7 — the model's
*field presence* varies run to run even when the prompt does not.

## 23. An `enum` that excludes the value your prompt demands makes the model think aloud

The guard prompt said "when `recognized` is false, set every other field to
the empty string", but `source`/`severity`/`env`/`state` all carry `enum`s
that do not contain `""`. The model spotted the contradiction and wrote its
reasoning into the response:

```
TransformBadOutput: llm: model returned non-JSON output:
'{"recognized":false,...,"state":"firing"}\n\nWait, I need to re-read the
instructions: when `recognized` is false, set every other field to the empty
string. But `source`, `severity`, `env`, and `sta'
  caused by: JSONDecodeError: Extra data: line 3 column 1 (char 146)
```

The malformed payload still produced no row, so the acceptance criterion
*looked* met — but by JSON-parse accident rather than by the designed guard.
Scoping the blanking instruction to the free-text fields made the intended
`UnrecognizedAlertPayload` guard fire instead.

Rule: keep prompt instructions and schema constraints consistent. A
contradiction does not fail cleanly; it leaks reasoning into the output.

## 24. Writing an undeclared column silently succeeds

After renaming the column to `PostMessageId` in the manifest, `alert_webhook`
still patched `Post Message Id`. The store accepts any key — records are
opaque JSON — so the write succeeded and produced a column the schema does not
declare and no `$ref` can reach. `table schema` showed `PostMessageId`; the
row showed `Post Message Id`. Nothing errored.

Rule: renaming a column means renaming every write site too, and the only
thing that catches a miss is reading a row back.

## 25. ~~`flow run <name>` does not resolve a flow by name~~ — FIXED in 0.16.0

Both halves of this are fixed; kept for the diagnosis, which was not obvious.

`popcorn flow list` showed `seed_test_alert`, but `flow run seed_test_alert`
returned `flow 'seed_test_alert' not found on this channel` — and that was the
SERVER's message, not the CLI's. The API does support by-name addressing
(`backend:services/api/customer_flows.py:1477`), but it resolves through the
channel_app binding, which only covers flows installed as a bound bundle. A
bundle installed ad-hoc by `flow import` is a UUID-addressed `customer_flows`
row, so its own name never matched.

Fixed client-side: `run_flow` now maps a non-UUID ref through `flow list`
before calling the API, and passes an unmatched name through untouched so the
server's channel_app resolution still works.

Separately, `flow run` did not supply `conversation_id`, so a flow declaring
it started fine and then died at runtime with
`ReferenceError: $inputs.conversation_id: key not found`. It is now defaulted
from `--channel`; an explicit value in `--inputs` still wins.

## 26. ~~`flow runs list --status failed` matches nothing~~ — RETRACTED

**This entry was wrong.** `--status failed` works correctly; the server
vocabulary is `all | running | failed | closed` and maps to
`ExecutionStatus = "Failed"` internally.

What actually happened: the response key is `executions`, not `runs`, and the
`--json` parse used to "verify" this read `.runs` and got an empty list. The
bug was in the check, not the CLI.

Kept rather than deleted as a reminder: a negative result from your own
scratch parser is not evidence until you have confirmed the shape it parses.
Read the keys first (`--json | python3 -c "print(list(d.keys()))"`).

## 27. `start_flow` is asynchronous — the parent completing proves nothing

`seed_test_alert` reports Completed the moment it launches `alert_webhook`.
The child can still fail afterwards. When driving a flow that starts another,
check the CHILD's run, not the parent's status.

---

## Step 4 acceptance criteria (live, `#alerts-full` on dev, 2026-08-09)

| # | Criterion | Result |
|---|---|---|
| 1 | Fresh import creates table + 2 schedules + 1 webhook | **pass** — 18 columns, `alert-tick` + `alert-digest`, `Alerts` webhook, `app_type` null |
| 2 | Fixture POST produces one correct `firing` row | **pass** (Step 0′, re-confirmed) |
| 3 | Re-fire folds into the same row | **pass** (Step 0′) |
| 4 | `alert_apply` ack stamps `Acked By`/`Acked At` | **pass** — only the targeted row moved; the other stayed `firing` |
| 5 | One tick nudges once; a second posts nothing | **pass** — `Nudged At` held exactly 1 entry after two ticks; acked row never nudged |
| 6 | Re-import is idempotent | **pass** — 5 flows / 1 webhook / rows preserved across 6 re-imports; `webhooks_skipped`, `schedules_updated`, `tables_patched` |
| 7 | ≥4 of 5 fixture shapes normalize to expectations | **pass, 4/5** — cloudwatch, alertmanager, manual exact; github disagrees on `Env` (recorded in `fixtures/expectations.json`) |
| 8 | Malformed fails without a row, failure identified | **pass** — `UnrecognizedAlertPayload`, row count unchanged at 5. The *step id* is still not named: the API exposes `activity_type` only (`foundation.workflow.fail`), never the DSL step id — see popcorn-cli#11 |

Three defects were found only by running it, none by validation:
the missing `required` entry (#22), the enum/prompt contradiction (#23), and
the un-renamed write site (#24). All three passed `flow validate` cleanly.

## Answer to spec open question 7

**`agent.transform` mapping is stable on fields the payload states, and
unstable on fields it omits.**

Across five shapes, every field present in the source payload was extracted
correctly and identically — severity, env, resource and source all round-trip
exactly for cloudwatch, alertmanager and manual. Fingerprints were stable
across re-fires.

The instability is entirely in *absence*:

- `github-actions-failure.json` has no environment; the model invented
  `Env: prod` by reading `head_branch: main`.
- `details` vanished from one run's output and failed the flow (#22).
- A contradictory instruction produced reasoning-as-output (#23).

So the deterministic-mapper (`code_name:`) work is **not** justified by field
drift — there is none worth engineering against. Reframe it: what a bundle
needs is not determinism but **the ability to require absence**, i.e. a way to
say "if the payload does not state `env`, fail rather than infer". Today the
only tool for that is the `recognized` flag plus a `workflow.fail` guard,
applied per-field — which is what this bundle does and what the docs teach.

The stronger platform ask remains gotcha #21: a `foundation.workflow.offset`
activity, which would remove the last LLM call from `alert_tick` entirely.


## 28. A missing key hard-fails reference resolution, and `on_error` cannot save it

`alert_tick` swept a row that had no `PostMessageId` (its `post` step had been
skipped) and the scheduled run died:

```
ReferenceError: $row.PostMessageId: key not found
```

The step carried `on_error: {policy: skip, retry: 1}` and it made no
difference — **reference resolution happens before the activity is invoked**,
so the step never reaches the point where its error policy applies. An error
policy protects against the activity failing, not against the arguments being
unresolvable.

This is the same failure mode as #22 (`details` missing from an
`output_schema`), which makes it a general rule rather than a store quirk:

> Never dereference a field that might be absent. Guarantee its presence
> upstream — in an `output_schema`'s `required`, or in a `list_rows` filter
> with `$exists: true`.

Fixed by giving the edit steps their own narrowed query rather than reusing
the sweep's row set. Note the ordering constraint that comes with it: the
`$exists` query filters `Status: firing`, so it must run BEFORE the patch step
that sets `Status: resolved`.

Worth knowing for diagnosis: activities commit individually, so a run that
dies at step 7 leaves steps 1-6 applied. The failing ticks had already
resolved rows before crashing, which is why the table looked half-swept.

**This bug was invisible for six hours.** It only fires once
`auto_resolve_hours` has elapsed and a sweep actually engages a row missing
the key — no amount of same-session testing would have surfaced it. Leave a
scheduled bundle running overnight before believing it.
