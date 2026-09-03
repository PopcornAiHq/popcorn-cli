# Alert Tracker — channel agent notes

This channel is an ops alert tracker. Alerts arrive by webhook and are deduped
into one row per incident. It is **read-mostly**: there is no triage flow.

## The data

One table, `alerts`. One row per incident, keyed by `Fingerprint`
(`source:resource:env`, derived at ingest and stable across re-fires).

The table has exactly two writers:

- `alert_webhook` — on each delivery: upserts the row, and on a genuinely new
  fingerprint posts to the channel and stamps `First Seen`
- `alert_tick` — every 5 minutes: nudges alerts nobody has acked, and
  auto-resolves alerts that have gone quiet

`Status` is `firing` → `resolved`, with `acked` as a human-owned side state.
Nothing derives it, so what you read is what happened.

`Seen At` and `Nudged At` accumulate — one timestamp appended per occurrence.
They are not counters; count the entries if you need a number.

## Answering questions

Read the table with a filter rather than scanning it — the store supports
`$lt`/`$gte`/`$in`/`$exists`, and ISO-8601 timestamps compare correctly
because the ordering is lexicographic.

- "what's on fire" → filter `Status: firing`
- "what's been ignored" → `Status: firing` plus `Nudged At: {$exists: true}`
- "what happened to X" → filter `Fingerprint: {$in: [...]}` and read
  `Seen At`, which holds one stamp per fire

## Acking

Acking is a **manual UI action**, not a flow: set a row's `Status` to `acked`.
That is meaningful because the nudge query filters on `Status: firing`, so
acking is what stops the reminders for that incident.

If a user asks you to ack something, say that it is a manual change and point
at the row. Do not patch it yourself.

## What not to do

- **Don't create rows.** Alerts come from the webhook; a hand-made row has no
  `Raw` payload and will never dedupe against the real one.
- **Don't set `Status` to a value outside firing/acked/resolved.** The column
  renders as a chip and an unknown value renders as nothing.
- **Don't resolve an alert because it looks old.** `alert_tick` already
  auto-resolves on silence. Closing one by hand while it is still firing hides
  a live problem.
- **Don't read `resolved` as "fixed".** Auto-resolve is a silence heuristic: a
  producer that stopped sending looks identical to a condition that cleared.
  A resolved row means "nothing has been heard since the cutoff", which is
  usually recovery and occasionally a broken producer.
