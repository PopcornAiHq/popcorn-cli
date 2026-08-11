# Alert Tracker

A Popcorn channel template that ingests ops alerts, dedupes them into one row
per incident, tracks acknowledgement, nudges on silence, and posts a daily
rollup.

Producer-agnostic: CloudWatch, Alertmanager, GitHub Actions and hand-raised
alerts all normalize through the same ingest flow with no per-source parser.

## Install

```bash
popcorn channel create '#alerts'
popcorn flow import . --channel '#alerts' --dry-run   # preflight
popcorn flow import . --channel '#alerts'
popcorn webhook list '#alerts'                        # copy the URL
```

**This is an untyped bundle** — it declares no `app_type`, so installing it
*clears* any app_type already on the channel. Install it into a dedicated ops
channel, never into a channel running a real app. `--dry-run` warns you.

## Try it without a producer

```bash
popcorn flow run seed_test_alert --channel '#alerts' --wait
popcorn table rows alerts --channel '#alerts'
```

Or replay a captured payload:

```bash
curl -X POST <webhook-url> -H 'Content-Type: application/json' \
  -d @fixtures/cloudwatch-alarm.json
```

## What's in the bundle

| File | Role |
|---|---|
| `manifest.yaml` | the `alerts` table, two schedules, the intake webhook, channel parameters |
| `alert_webhook.yaml` | ingest: normalize → guard → dedup-upsert → post if new |
| `alert_tick.yaml` | every 5 min: nudge unacked, auto-resolve quiet alerts, refresh summary |
| `alert_apply.yaml` | ack / snooze / resolve — agent-runnable and CLI-runnable |
| `alert_digest.yaml` | daily rollup post |
| `seed_test_alert.yaml` | fire a canned alert through the real ingest path |
| `fixtures/*.json` | captured payloads, including a deliberately malformed one |

Fixtures are `.json` on purpose. A `.yaml` anywhere in the bundle would be
installed as a flow — the importer keys entries by basename.

## Tuning

`channel_parameters` in the manifest, applied by re-importing:

| Parameter | Default | Effect |
|---|---|---|
| `nudge_offset_minutes` | -30 | how long an alert may sit unacked before one nudge is posted |
| `auto_resolve_offset_hours` | -6 | silence after which a firing alert is auto-resolved |

Both are **negative**: they are passed straight to `foundation.workflow.offset`,
which takes signed durations, and the DSL cannot negate a reference.

The nudge is once per incident, not once per tick — `Nudged At` being
non-empty is what removes a row from the nudge set.

## Known limits

- **Snoozing needs a caller-supplied timestamp.** `alert_apply` takes
  `snooze_until` as ISO-8601 rather than "minutes", because the flow DSL has
  no arithmetic. The agent or CLI computes it.
- **The tick is fully deterministic** — no LLM call. Its sweep cutoffs come
  from `foundation.workflow.offset`. Note the offsets are configured NEGATIVE
  (`nudge_offset_minutes: -30`): the activity takes signed durations and the
  DSL cannot negate a reference. See `GOTCHAS.md` #21.
- **Ingest costs one LLM call per alert.** That is what buys source-agnostic
  normalization. At ops alert volume it is not a meaningful cost.
- **No SNS subscription handshake.** Pointing a raw SNS topic at the webhook
  will not self-confirm; the fixtures are notification *bodies*, not SNS
  envelopes.
