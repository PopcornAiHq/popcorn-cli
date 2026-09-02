# Alert Tracker

A Popcorn channel template that ingests ops alerts, dedupes them into one row
per incident, nudges on silence, and auto-resolves alerts that go quiet.

Producer-agnostic: CloudWatch, Alertmanager, GitHub Actions and hand-raised
alerts all normalize through the same ingest flow with no per-source parser.

**Read-mostly by design.** The webhook is the only thing that creates rows and
the sweep is the only thing that closes them. There is no triage flow —
acking is a manual `Status` change in the UI, which works because the nudge
query filters on `Status: firing`.

## Install

**Not installable as it stands.** These bundles are reference material, not
registry entries — `popcorn flow import` is gone and nothing installs a local
directory (see `docs/TEMPLATE_AUTHORING.md` §2). To run it, land it in
popcorn-backend under `lib/temporal/flows/`, register it in `CHANNEL_TEMPLATES`,
deploy, publish from the intranet `/app-bundles` page, then:

```bash
popcorn channel create '#alerts' --template alerttracker
popcorn webhook list '#alerts'                      # copy the URL
```

**It is an untyped bundle** — it declares no `app_type`, so installing it
*clears* any app_type already on the channel. Use a dedicated channel, never
one running a real app. `template check` warns about this offline.

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
| `manifest.yaml` | the `alerts` table, the sweep schedule, the intake webhook, channel parameters |
| `alert_webhook.yaml` | ingest: normalize → guard → dedup-upsert → post if new |
| `alert_tick.yaml` | every 5 min: nudge unacked, auto-resolve quiet alerts |
| `seed_test_alert.yaml` | fire a canned alert through the real ingest path |
| `fixtures/*.json` | captured payloads, including a deliberately malformed one |

Fixtures are `.json` on purpose. A `.yaml` anywhere in the bundle would be
installed as a flow — the importer keys entries by basename.

## Tuning

`channel_parameters` in the manifest, applied by re-importing:

| Parameter | Default | Effect |
|---|---|---|
| `nudge_after_minutes` | 30 | how long an alert may sit unacked before one nudge is posted |
| `auto_resolve_hours` | 6 | silence after which a firing alert is auto-resolved |

The nudge is once per incident, not once per tick — `Nudged At` being
non-empty is what removes a row from the nudge set.

## Known limits

- **Auto-resolve is a silence heuristic, not a recovery signal.** A producer
  that stops sending is indistinguishable from a condition that cleared, so
  `resolved` means "nothing heard for `auto_resolve_hours`". That is the right
  default when a webhook is the only writer — a lost or never-sent "resolved"
  delivery would otherwise leave a row firing forever — but a genuinely stuck
  alert can be closed while the problem persists.
- **There is no triage flow.** Acking is a manual `Status` change in the UI.
  It works because the nudge query filters on `Status: firing`, so flipping a
  row to `acked` is what stops the reminders.
- **The tick is fully deterministic** — no LLM call. Its sweep cutoffs come
  from `foundation.math.offset`, which names the direction (`subtract`) so the
  durations above stay plain positive numbers. See `GOTCHAS.md` #21.
- **Ingest costs one LLM call per alert** — a deliberate trade, not a
  limitation. `foundation.fields.extract` reads fields deterministically and
  would be the better tool for a single known producer, but this bundle runs
  CloudWatch, Alertmanager, GitHub and hand-raised alerts through one flow,
  and two of the fields it needs (`severity`, `env`) are absent from a
  CloudWatch body entirely — so they have to be derived, not read. Pinning a
  bundle to one producer would flip that call. At ops alert volume the cost is
  not meaningful either way.

  [`../deploywatch/`](../deploywatch/) is that same decision answered the
  other way: one producer, every field stated in the payload, no model call
  anywhere in the bundle. Read the two together — the input decides which one
  you are writing.
- **No SNS subscription handshake.** Pointing a raw SNS topic at the webhook
  will not self-confirm; the fixtures are notification *bodies*, not SNS
  envelopes.
