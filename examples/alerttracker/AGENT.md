# Alert Tracker — channel agent notes

This channel is an ops alert tracker. Alerts arrive by webhook, are deduped
into one row per incident, and are triaged by you or by a human.

## The data

One table, `alerts`. One row per incident, keyed by `Fingerprint`
(`source:resource:env`, derived at ingest and stable across re-fires).

`Status` is the field that matters: `firing` → `acked` → `resolved`, with
`snoozed` as a side branch. It is written at transition by whatever caused it —
the webhook writes `firing`, `alert_apply` writes the rest, and `alert_tick`
writes `resolved` when an alert goes quiet. Nothing derives it, so what you
read is what happened.

`Seen At` and `Nudged At` accumulate — one timestamp appended per occurrence.
They are not counters; count the entries if you need a number.

## Triaging

Run the `alert_apply` flow. Never patch rows directly: `alert_apply` also
edits the original channel post so the history stays truthful.

```json
{"action": "ack",     "fingerprints": ["cloudwatch:popcorn-dev-ops-api:dev"]}
{"action": "resolve", "fingerprints": ["cloudwatch:popcorn-dev-ops-api:dev"]}
{"action": "snooze",  "fingerprints": ["..."], "snooze_until": "2026-08-09T18:00:00+00:00"}
```

`snooze_until` must be an ISO-8601 timestamp that **you** compute. The flow
cannot do date arithmetic — if a user says "snooze for two hours", work out
the wall-clock time yourself and pass it.

`fingerprints` takes several ids, so "ack all the dev alarms" is one call, not
a loop. Look them up first with a filtered read of the table rather than
guessing at fingerprint spelling.

## What not to do

- Don't create rows. Alerts come from the webhook; a hand-made row has no
  `Raw` payload and will never dedupe against the real one.
- Don't set `Status` to a value outside firing/acked/snoozed/resolved. The
  column renders as a chip and an unknown value renders as nothing.
- Don't resolve an alert just because it looks old. `alert_tick` already
  auto-resolves on silence; resolving by hand while it is still firing hides
  a live problem.
