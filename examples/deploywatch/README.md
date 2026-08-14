# Deploy Watch

A Popcorn channel template that tracks GitHub deployments — one row per
deployment, updated as its status changes, with **no LLM call anywhere in the
bundle**.

It exists as the counterpart to [`../alerttracker/`](../alerttracker/). Both
ingest a webhook; they reach opposite conclusions about how, and the reason is
worth more than either bundle.

## The one idea

> Extraction reads what is there. Derivation invents what is not.

`foundation.fields.extract` names fields by path. No model, no prompt, and an
absent path is a loud `ExtractPathNotFound` rather than a plausible guess. It
is the right tool exactly when the payload *states* what you need.

A GitHub `deployment_status` body states all of it:

| Field | Path | alerttracker's equivalent |
|---|---|---|
| identity | `deployment.node_id` | derived — `source:resource:env`, composed by a model |
| environment | `deployment_status.environment` | derived — a CloudWatch body has no environment key |
| state | `deployment_status.state` | derived — normalized across four producers |
| actor | `deployment_status.creator.login` | absent from most alert bodies |

So this bundle extracts and alerttracker derives, and neither is doing it
wrong. Pin a bundle to one producer that states its fields and the model call
disappears; take four producers and need a `severity` nobody sends, and it
comes back. That is the whole decision.

## Install

```bash
popcorn channel create '#deploys'
popcorn template check .                              # offline, no channel needed
popcorn flow import . --channel '#deploys' --dry-run  # preflight
popcorn flow import . --channel '#deploys'
popcorn webhook list '#deploys'                       # copy the URL
```

**This is an untyped bundle** — it declares no `app_type`, so installing it
*clears* any app_type already on the channel. Install it into a dedicated
channel, never into one running a real app. `--dry-run` warns you.

Then point a GitHub webhook at the URL with the **Deployment statuses** event
selected (repo → Settings → Webhooks).

## Try it without GitHub

```bash
popcorn flow run seed_test_deploy --channel '#deploys' --wait
popcorn flow run seed_test_deploy --channel '#deploys' --wait \
  --inputs '{"state":"success"}'
popcorn table rows deployments --channel '#deploys'
```

Two runs, same default `deployment_id`, different `state` — one row, `State`
replaced, `State History` holding both. Or replay a captured payload:

```bash
curl -X POST <webhook-url> -H 'Content-Type: application/json' \
  -d @fixtures/deployment-status-in-progress.json
```

## What's in the bundle

| File | Role |
|---|---|
| `manifest.yaml` | the `deployments` table, the sweep schedule, the intake webhook |
| `deploy_webhook.yaml` | ingest: extract → dedup-upsert → post if new → stamp |
| `deploy_sweep.yaml` | every 5 min: nudge once about anything stuck `in_progress` |
| `seed_test_deploy.yaml` | fire a canned delivery through the real ingest path |
| `fixtures/*.json` | captured payloads, including one that is not a deployment at all |

Fixtures are `.json` on purpose. A `.yaml` anywhere in the bundle would be
installed as a flow — the importer keys entries by basename. `popcorn template
check` fails the bundle if one slips in.

## What the fixtures are for

| Fixture | Demonstrates |
|---|---|
| `deployment-status-in-progress.json` + `deployment-status-success.json` | same `node_id` → one row, `State History` accumulating |
| `deployment-status-failure.json` | the `warn=failure` display segment rendering |
| `deployment-status-with-version.json` | `deployment.payload.version` present — the non-default branch of `defaults:` |
| `not-a-deployment.json` | a GitHub ping. `fields.extract` fails it with every missing path listed, and invents nothing |

That last one is the contrast worth running. Post it here and the flow fails
cleanly. Post junk at `alerttracker` and, without its explicit `recognized`
guard, the model would have fabricated an alert to satisfy `required` — which
is exactly what it did before the guard existed.

## Tuning

`channel_parameters` in the manifest, applied by re-importing:

| Parameter | Default | Effect |
|---|---|---|
| `stuck_after_minutes` | 45 | how long a deployment may sit `in_progress` before one nudge is posted |

The nudge is once per deployment, not once per sweep — `Nudged At` being
non-empty is what removes a row from the query.

## Known limits

- **`state` is not an enum.** GitHub has added deployment states before
  (`in_progress`, `queued`), and an enum in the `output_schema` would fail the
  whole delivery on a state we have not seen. Unknown states store fine and
  render plain.
- **The sweep never writes `State`.** A deployment's state is GitHub's to
  report; unlike `alert_tick`, this sweep only nudges. Nothing here
  auto-resolves.
- **No signature verification.** The bundle does not check
  `X-Hub-Signature-256`; the webhook URL is the only secret.
- **One row per deployment, not per status.** If you need the full status
  stream rather than the latest state plus a history string, merge on the
  status `node_id` instead — but then nothing dedupes GitHub's redeliveries.
