# examples/

What is here is **not** bundle source, and there is no bundle here to copy.

- `alerttracker/GOTCHAS.md` — a log of what actually happened when the
  alerttracker bundle was built and run against live traffic: merge policies
  that silently overwrote a column on re-fire, an LLM step that fabricated a
  plausible row rather than failing. No introspection produces any of it. Two
  findings are struck through rather than deleted, because the record of
  having been wrong is part of the evidence.
- `*/fixtures/*.json` — sample webhook payloads to POST at a project while
  testing, including a deliberately malformed one.

## Where the bundles went

The two bundles that used to sit here are checker fixtures, at
`tests/fixtures/bundles/`. They were never safe to copy: neither declares a
`version:`, which a publish requires, and both had drifted from the bundles the
platform ships.

To get real app source, ask the server for it — a checkout is always the
deployed version:

```bash
popcorn project create '#scratch' --app alerttracker
popcorn app fork --project '#scratch'
popcorn app checkout --project '#scratch'
```

`docs/TEMPLATE_AUTHORING.md` is the guide; §2 covers that loop.
