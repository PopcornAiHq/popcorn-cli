# Deploy Watch — channel agent notes

This channel tracks GitHub deployments. Deliveries arrive by webhook and are
merged into one row per deployment.

## The data

One table, `deployments`. One row per deployment, keyed by `DeploymentId` —
GitHub's `deployment.node_id`, copied verbatim from the payload.

Every column is read straight off the delivery. Nothing here is inferred,
normalized or summarized, so what you read is what GitHub sent.

`State` is the current state: `pending`, `queued`, `in_progress`, `success`,
`failure`, `error`. `State History` accumulates one entry per delivery, so a
row shows how the deployment progressed rather than only where it ended up.
It is not a counter; count the entries if you need a number.

`Version` is empty unless the deployer set `version` in GitHub's free-form
`deployment.payload`. An empty string means "not supplied", not "unknown".

## Answering questions

Read the table with a filter rather than scanning it — the store supports
`$lt`/`$gte`/`$in`/`$exists`, and ISO-8601 timestamps compare correctly
because the ordering is lexicographic.

- "what's deploying right now" → filter `State: in_progress`
- "did production go out today" → filter `Environment: production` plus a
  `Last Seen` bound
- "what failed" → filter `State: failure`

## What not to do

- **Don't write to this table.** `State` belongs to GitHub. A hand-set state
  is a lie that the next real delivery silently overwrites, and in the
  meantime it is indistinguishable from a reported one.
- **Don't create rows.** Deployments come from the webhook; a hand-made row
  has no `Raw` payload and no real `DeploymentId`, so it will never merge with
  the genuine deliveries for that deployment.
- **Don't infer a deployment failed because it went quiet.** A deployment
  stuck in `in_progress` may simply have a long-running job. `deploy_sweep`
  posts one nudge about it and deliberately does not change its state.
- **Don't treat `Environment` as a fixed set.** It is whatever the repo names
  its environments; `production`/`staging`/`dev` are just the ones the column
  renders as chips.
