"""`popcorn schedule` — a channel's live scheduled flows (read-only).

Read-only on purpose. The write half of the API
(`create`/`update`/`delete`) is real and equally permissioned, but pausing
and re-cadencing a channel's schedules is owned by the `set_app_mode` bundle
flow, which pairs every change with a note marker (`auto-paused: app_mode
off` / `auto-resumed: set_app_mode`) that channel archive/unarchive reads to
decide what it may resume. A CLI write that skipped those markers would land
outside that scheme, so exposing one is a decision in its own right rather
than the other half of this command — see KEW-2307.

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

_CHANNEL = Argument("channel", "Channel name (#general) or UUID", required=True)


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
    resp = operations.list_scheduled_flows(client, args.channel)
    items = resp.get("scheduled_flows") or []
    count = resp.get("count", len(items))
    lines = [f"Schedules in {args.channel} ({count}):"]
    for item in items:
        paused = "  [paused]" if item.get("paused") else ""
        next_run = item.get("next_run_at") or "-"
        lines.append(
            f"  {(item.get('slug') or '?'):<18} {(item.get('flow_id') or '?'):<20} "
            f"{_cadence(item):<36} next {next_run}{paused}"
        )
    _output(args, resp, "\n".join(lines))


def _schedule_get(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    resp = operations.get_scheduled_flow(client, args.channel, args.schedule)
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
        # channel archive/unarchive), and usually the only on-the-wire
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
        description="Scheduled-flow commands (list, get)",
        subcommands=[
            Subcommand(
                "list",
                "List a channel's live scheduled flows",
                _schedule_list,
                [_CHANNEL],
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
                    _CHANNEL,
                ],
            ),
        ],
    )
)
