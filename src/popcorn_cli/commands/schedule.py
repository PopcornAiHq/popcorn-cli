"""`popcorn schedule` — a project's live scheduled flows: read, and run now.

There is no definition write to wrap. A schedule is app content: the
manifest's `schedules:` list declares it and the installer reconciles the
project's Temporal schedules to that list on every install. Changing a
cadence therefore means editing the manifest and publishing a version, the
same path as any other change to what a project does. The user-token
`create`/`update`/`delete` this command was once expected to grow into were
deleted server-side outright, not deprecated. The agent surface keeps a live
`update` PATCH owned by the `set_app_mode` bundle flow, which the next
install re-applies over, so a CLI write on top of it would be transient even
where it was reachable.

`trigger` is the one write, and it is not a definition change: it fires one
run of a declared schedule now, with the inputs the schedule already stores.
The server refuses a schedule the bound manifest does not declare.

Handlers import their `..cli` helpers *inside* the function body: cli.py
imports this package at module load to build the parser, so a module-level
import would be a cycle.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from popcorn_core import operations

from ..registry import Argument, Command, Subcommand, register

_PROJECT = Argument("project", "Project name (#general) or UUID", required=True)


def _humanize_seconds(seconds: int) -> str:
    """`180` → `3m`, `900` → `15m`, `3600` → `1h`, `90` → `90s`."""
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _cadence(item: dict[str, Any]) -> str:
    """One-line cadence for a schedule, whichever of the two forms it uses.

    A schedule carries an interval or a cron expression, never both — the
    importer rejects a declaration with two cadences.
    """
    interval = item.get("interval_seconds")
    if interval:
        return f"every {_humanize_seconds(int(interval))}"
    cron = item.get("cron_expr")
    if cron:
        tz = item.get("timezone") or "UTC"
        return f"cron {cron} ({tz})"
    return "no cadence"


def _schedule_list(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    resp = operations.list_scheduled_flows(client, args.project)
    items = resp.get("scheduled_flows") or []
    count = resp.get("count", len(items))
    lines = [f"Schedules in {args.project} ({count}):"]
    for item in items:
        paused = "  [paused]" if item.get("paused") else ""
        next_run = item.get("next_run_at") or "-"
        lines.append(
            f"  {(item.get('slug') or '?'):<18} {(item.get('flow_id') or '?'):<20} "
            f"{_cadence(item):<36} next {next_run}{paused}"
        )
    _output(args, resp, "\n".join(lines))


def _schedule_trigger(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    resp = operations.trigger_scheduled_flow(
        client, args.project, args.schedule, getattr(args, "overlap_policy", None)
    )
    schedule_id = resp.get("schedule_id") or args.schedule
    workflow_id = resp.get("workflow_id")
    if resp.get("skipped_overlap"):
        lines = [
            f"Not run: {schedule_id} already has a run in flight, and its overlap "
            "policy dropped this one.",
            "Pass --overlap-policy allow_all to run it anyway.",
        ]
    elif workflow_id:
        lines = [
            f"Triggered {schedule_id}",
            f"  workflow_id  {workflow_id}",
            f"  run_id       {resp.get('run_id') or '-'}",
            "",
            f"Follow it: popcorn flow runs get {workflow_id} --project '{args.project}'",
        ]
    else:
        # Null ids are "not observed", not a failure: a buffer_* policy
        # deferred the run, or Temporal was slow to record it.
        lines = [
            f"Triggered {schedule_id}, but no run was seen starting yet — it may be "
            "deferred behind a running one, or slow to record.",
            f"Check with: popcorn schedule get {args.schedule} --project '{args.project}'",
        ]
    _output(args, resp, "\n".join(lines))


def _schedule_get(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    resp = operations.get_scheduled_flow(client, args.project, args.schedule)
    item = resp.get("scheduled_flow") or {}
    interval = item.get("interval_seconds")
    cadence = _cadence(item)
    if interval:
        cadence += f" (interval_seconds {interval})"
    rows = [
        ("schedule_id", item.get("schedule_id")),
        ("flow", item.get("flow_id")),
        ("cadence", cadence),
        ("timezone", item.get("timezone")),
        ("paused", "yes" if item.get("paused") else "no"),
        # Written by whatever last changed this schedule (`set_app_mode`,
        # project archive/unarchive), and usually the only on-the-wire
        # explanation of why a live cadence differs from the manifest's.
        ("note", item.get("note") or "-"),
        ("overlap", item.get("overlap_policy")),
        ("jitter", f"{item.get('jitter_seconds') or 0}s"),
        ("next run", item.get("next_run_at") or "-"),
        ("last run", item.get("last_run_at") or "-"),
        (
            "actions",
            f"{item.get('num_actions', 0)} fired, "
            f"{item.get('num_actions_skipped_overlap', 0)} skipped (overlap), "
            f"{item.get('num_actions_missed_catchup_window', 0)} missed (catchup), "
            f"{item.get('running_count', 0)} running",
        ),
        ("inputs", json.dumps(item.get("inputs") or {}, sort_keys=True)),
    ]
    header = f"{item.get('slug') or '?'} ({item.get('flow_id') or '?'})"
    lines = [header] + [f"  {label:<12} {value}" for label, value in rows]
    _output(args, resp, "\n".join(lines))


register(
    Command(
        name="schedule",
        category="flows",
        description="Scheduled-flow commands (list, get, trigger)",
        subcommands=[
            Subcommand(
                "list",
                "List a project's live scheduled flows",
                _schedule_list,
                [_PROJECT],
            ),
            Subcommand(
                "get",
                "Show one scheduled flow's cadence and run counters",
                _schedule_get,
                [
                    Argument(
                        "schedule",
                        "Schedule slug, flow id, or full schedule_id",
                        positional=True,
                    ),
                    _PROJECT,
                ],
            ),
            Subcommand(
                "trigger",
                "Run a declared schedule now, with its stored inputs",
                _schedule_trigger,
                [
                    Argument(
                        "schedule",
                        "Schedule slug, flow id, or full schedule_id",
                        positional=True,
                    ),
                    _PROJECT,
                    Argument(
                        "overlap-policy",
                        "Overlap policy for this run only (default: the schedule's "
                        "own); allow_all runs it even while another is in flight",
                    ),
                ],
            ),
        ],
    )
)
