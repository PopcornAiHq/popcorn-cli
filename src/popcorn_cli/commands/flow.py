"""`popcorn flow` — customer flows (Temporal automations per channel).

Handlers import their `..cli` helpers *inside* the function body: cli.py
imports this package at module load to build the parser, so a module-level
import would be a cycle.
"""

from __future__ import annotations

import argparse
import time
from typing import TYPE_CHECKING, Any

from popcorn_core import operations

from ..registry import Argument, Command, Subcommand, register

if TYPE_CHECKING:
    from popcorn_core.client import APIClient

_CHANNEL = Argument("channel", "Channel name (#general) or UUID", required=True)

# Temporal execution statuses that mean the run is over. Anything else is
# still in flight.
_TERMINAL_OK = {"COMPLETED"}
_TERMINAL_BAD = {"FAILED", "TIMED_OUT", "CANCELED", "TERMINATED"}
_POLL_SECONDS = 3
_DEFAULT_WAIT_SECONDS = 300


def _poll_until_closed(
    client: APIClient, channel: str, workflow_id: str, timeout: int
) -> dict[str, Any]:
    """Poll a flow run until it reaches a terminal status.

    Raises PopcornError on a failed run (so the shell sees a non-zero exit) or
    when `timeout` seconds elapse without a terminal status.
    """
    from popcorn_core.errors import PopcornError

    started = time.monotonic()
    while True:
        resp = operations.get_flow_run(client, channel, workflow_id, include_errors=True)
        run = resp.get("run") or resp
        status = (run.get("status") or "").upper()
        if status in _TERMINAL_OK:
            return dict(run)
        if status in _TERMINAL_BAD:
            raise PopcornError(
                f"Flow run {workflow_id} ended {status}",
                error_code="validation",
            )
        if time.monotonic() - started > timeout:
            raise PopcornError(
                f"Waiting for {workflow_id} timed out after {timeout}s "
                f"(last status {status or 'unknown'})",
                error_code="timeout",
            )
        time.sleep(_POLL_SECONDS)


# Bundle files that are not flows. A manifest is checked by `flow import
# --dry-run`, not here; config/strings are template data.
_NOT_A_FLOW = {"manifest.yaml", "config.yaml", "strings.yaml"}


def _fmt_list(values: list[Any]) -> str:
    return ", ".join(str(v) for v in values) if values else "-"


def _dry_run_lines(summary: dict[str, Any]) -> list[str]:
    """Render the preflight summary.

    `flows` arrives nested as create/update/**delete** — install prunes flows
    the bundle does not cover, so the deletions are the line the author most
    needs to see. Dumping the dict would bury them.
    """
    flows = summary.get("flows") or {}
    lines = [
        f"  flows to create:  {_fmt_list(flows.get('create') or [])}",
        f"  flows to update:  {_fmt_list(flows.get('update') or [])}",
        f"  flows to DELETE:  {_fmt_list(flows.get('delete') or [])}",
        f"  tables:           {_fmt_list(summary.get('tables') or [])}",
        f"  scalars:          {_fmt_list(summary.get('scalars') or [])}",
        f"  webhooks:         {summary.get('webhooks', 0)}",
    ]
    schedules = summary.get("schedules", 0)
    if summary.get("schedules_replace_existing"):
        lines.append(
            f"  schedules:        {schedules} (replaces the channel's existing schedules wholesale)"
        )
    else:
        lines.append(f"  schedules:        {schedules}")
    lines.append(f"  app_type:         {summary.get('app_type') or '(none)'}")
    return lines


def _install_lines(summary: dict[str, Any]) -> list[str]:
    """Render a real install's InstallSummary — flat snake_case lists.

    Only non-empty sections; a full InstallSummary is ~18 keys and most are
    empty on any given install.
    """
    lines = []
    template = summary.get("template")
    if template:
        lines.append(f"  template: {template}")
    for key in sorted(summary):
        if key == "template":
            continue
        value = summary[key]
        if not value:
            continue
        rendered = _fmt_list(value) if isinstance(value, list) else str(value)
        lines.append(f"  {key}: {rendered}")
    return lines


def _flow_import(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output, _status

    client = _get_client(args)
    dry = getattr(args, "dry_run", False)
    _status(f"{'Validating' if dry else 'Installing'} {args.directory}...")
    resp = operations.import_template(client, args.channel, args.directory, dry_run=dry)
    summary = resp.get("summary") or {}

    if dry:
        header = f"Would install into {args.channel}:"
        lines = _dry_run_lines(summary)
        # An untyped bundle CLEARS the channel's app_type — the one surprise
        # in the install that is not obviously reversible.
        if not summary.get("app_type"):
            _status(
                "Warning: this bundle declares no app_type, so installing it "
                "would CLEAR the channel's app_type."
            )
    else:
        header = f"Installed into {args.channel}:"
        lines = _install_lines(summary)

    _output(args, resp, "\n".join([header, *lines]))


def _flow_validate(args: argparse.Namespace) -> None:
    from pathlib import Path

    from popcorn_core.errors import PopcornError

    from ..cli import _get_client, _output

    client = _get_client(args)
    target = Path(args.path)
    if target.is_dir():
        files = [p for p in sorted(target.glob("*.y*ml")) if p.name not in _NOT_A_FLOW]
    else:
        files = [target]
    if not files:
        raise PopcornError(f"No flow YAML found at {args.path}", error_code="validation")

    results: list[dict[str, Any]] = []
    lines: list[str] = []
    bad = 0
    for path in files:
        resp = operations.validate_flow_yaml(client, args.channel, path.read_text())
        results.append({"file": str(path), **resp})
        # An invalid flow is a 200 with valid:false — branch on the field.
        if resp.get("valid"):
            steps = ", ".join(s.get("id", "?") for s in resp.get("steps", []))
            lines.append(f"  ok    {path}  [{steps}]")
        else:
            bad += 1
            lines.append(f"  FAIL  {path}")
            for issue in resp.get("issues", []):
                lines.append(f"          {issue}")

    header = f"Validated {len(files)} flow(s), {bad} invalid:"
    _output(args, {"results": results, "invalid": bad}, "\n".join([header, *lines]))
    if bad:
        raise PopcornError(f"{bad} flow(s) failed validation", error_code="validation")


def _flow_activities(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    resp = operations.list_activity_catalog(_get_client(args))
    activities = resp.get("activities", [])
    # The endpoint takes no filter params — narrow client-side.
    for key in ("tier", "status", "category"):
        want = getattr(args, key, None)
        if want:
            activities = [a for a in activities if a.get(key) == want]
    resp = {**resp, "activities": activities}
    lines = [f"Activities ({len(activities)}):"]
    for a in sorted(activities, key=lambda x: x.get("name", "")):
        summary = (a.get("description") or "").splitlines()
        lines.append(
            f"  {a.get('name', '?'):<42} {a.get('status', '?'):<10} "
            f"{summary[0][:60] if summary else ''}"
        )
    _output(args, resp, "\n".join(lines))


def _flow_list(args: argparse.Namespace) -> None:
    from ..cli import _attach_pagination, _get_client, _output

    client = _get_client(args)
    limit = getattr(args, "limit", None) or 50
    offset = getattr(args, "offset", None) or 0
    resp = operations.list_flows(client, args.channel, limit=limit, offset=offset)
    flows = resp.get("flows", [])
    next_flags = {"offset": str(offset + limit)} if resp.get("has_more") else None
    _attach_pagination(resp, next_flags)
    lines = [f"Flows in {args.channel} ({len(flows)}):"]
    for fl in flows:
        lines.append(f"  {fl.get('id', '?')}  {fl.get('name', '?')} (v{fl.get('version', '?')})")
    _output(args, resp, "\n".join(lines))


def _flow_get(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    resp = operations.get_flow(client, args.channel, args.flow_id)
    flow = resp.get("flow") or resp
    lines = [
        f"{flow.get('name', '?')} (v{flow.get('version', '?')})",
        f"  id: {flow.get('id', '?')}",
    ]
    if flow.get("description"):
        lines.append(f"  {flow['description']}")
    _output(args, resp, "\n".join(lines))


def _flow_run(args: argparse.Namespace) -> None:
    from popcorn_core.errors import PopcornError

    from ..cli import _get_client, _output, _read_json_object, _status

    client = _get_client(args)
    raw_inputs = getattr(args, "inputs", None)
    inputs = _read_json_object(raw_inputs, "--inputs") if raw_inputs else None
    resp = operations.run_flow(client, args.channel, args.flow_id, inputs=inputs)
    name = resp.get("flow_name", args.flow_id)
    lines = [
        f"Started flow '{name}' (v{resp.get('flow_version', '?')})",
        f"  workflow_id: {resp.get('workflow_id', '?')}",
        f"  run_id:      {resp.get('run_id', '-')}",
    ]

    if getattr(args, "wait", False):
        workflow_id = resp.get("workflow_id") or (resp.get("run") or {}).get("workflow_id")
        if not workflow_id:
            raise PopcornError(
                f"Run started but returned no workflow_id: {resp}",
                error_code="internal",
            )
        _status(f"Waiting for {workflow_id}...")
        run = _poll_until_closed(
            client,
            args.channel,
            workflow_id,
            timeout=getattr(args, "timeout_run", None) or _DEFAULT_WAIT_SECONDS,
        )
        resp = {**resp, "run": run}
        lines.append(f"  status:      {run.get('status', '?')}")

    _output(args, resp, "\n".join(lines))


def _flow_runs_list(args: argparse.Namespace) -> None:
    from ..cli import _attach_pagination, _get_client, _output

    client = _get_client(args)
    limit = getattr(args, "limit", None) or 50
    resp = operations.list_flow_runs(
        client,
        args.channel,
        status=getattr(args, "status", None),
        limit=limit,
        page_token=getattr(args, "page_token", None),
    )
    execs = resp.get("executions", [])
    token = resp.get("next_page_token")
    _attach_pagination(resp, {"page-token": token} if token else None)
    count = resp.get("count", len(execs))
    lines = [f"Flow runs in {args.channel} ({count}):"]
    for e in execs:
        lines.append(
            f"  {(e.get('status') or '?'):<10} {e.get('workflow_id', '?')}  "
            f"{e.get('workflow_type', '')}  {e.get('start_time', '')}"
        )
    _output(args, resp, "\n".join(lines))


def _flow_runs_get(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    resp = operations.get_flow_run(
        client,
        args.channel,
        args.workflow_id,
        run_id=getattr(args, "run_id", None),
        include_errors=getattr(args, "include_errors", False),
    )
    run = resp.get("run") or resp
    lines = [
        f"{run.get('status', '?')}  {run.get('workflow_id', '?')}",
        f"  type:    {run.get('workflow_type', '-')}",
        f"  run_id:  {run.get('run_id', '-')}",
        f"  started: {run.get('start_time', '-')}",
        f"  closed:  {run.get('close_time', '-')}",
    ]
    _output(args, resp, "\n".join(lines))


register(
    Command(
        name="flow",
        category="flows",
        description="Flow commands (activities, validate, import, list, get, run, runs list, runs get)",
        subcommands=[
            # First: the discovery entry point for a template author.
            Subcommand(
                "activities",
                "List the DSL activity catalog",
                _flow_activities,
                [
                    Argument(
                        "tier",
                        "Filter by tier (foundation, feature, app, system)",
                        type=str,
                        choices=["foundation", "feature", "app", "system"],
                    ),
                    Argument(
                        "status",
                        "Filter by status",
                        type=str,
                        choices=["release", "beta", "alpha", "deprecated"],
                    ),
                    Argument("category", "Filter by category (store, channel, …)", type=str),
                ],
            ),
            Subcommand(
                "import",
                "Install a template directory into a channel",
                _flow_import,
                [
                    Argument("directory", "Template bundle directory", positional=True),
                    _CHANNEL,
                    Argument(
                        "dry-run",
                        "Validate the bundle and report the plan; install nothing",
                        action="store_true",
                    ),
                ],
            ),
            Subcommand(
                "validate",
                "Validate flow YAML without installing",
                _flow_validate,
                [
                    Argument("path", "Flow YAML file, or a bundle directory", positional=True),
                    _CHANNEL,
                ],
            ),
            Subcommand(
                "list",
                "List flows in a channel",
                _flow_list,
                [
                    _CHANNEL,
                    Argument("limit", "Max results (default 50)", type=int),
                    Argument("offset", "Pagination offset", type=int),
                ],
            ),
            Subcommand(
                "get",
                "Get a flow definition",
                _flow_get,
                [
                    Argument("flow_id", "Flow UUID", positional=True),
                    _CHANNEL,
                ],
            ),
            Subcommand(
                "run",
                "Start a flow run",
                _flow_run,
                [
                    Argument("flow_id", "Flow UUID", positional=True),
                    _CHANNEL,
                    Argument(
                        "inputs",
                        "JSON object of flow inputs (use '@-' for stdin, '@path' for a file)",
                        type=str,
                    ),
                    Argument(
                        "wait",
                        "Poll until the run reaches a terminal status",
                        action="store_true",
                    ),
                    Argument(
                        "timeout-run",
                        f"Seconds to wait with --wait (default {_DEFAULT_WAIT_SECONDS})",
                        type=int,
                    ),
                ],
            ),
            Subcommand(
                "runs",
                "Inspect flow runs (list, get)",
                None,
                [],
                [
                    Subcommand(
                        "list",
                        "List flow runs in a channel",
                        _flow_runs_list,
                        [
                            _CHANNEL,
                            Argument(
                                "status",
                                "Filter by run status (default all)",
                                type=str,
                                choices=["all", "running", "failed", "closed"],
                            ),
                            Argument("limit", "Max results, 1-200 (default 50)", type=int),
                            Argument(
                                "page-token",
                                "Cursor from a previous response's pagination.next",
                                type=str,
                            ),
                        ],
                    ),
                    Subcommand(
                        "get",
                        "Get a flow run's detail",
                        _flow_runs_get,
                        [
                            Argument("workflow_id", "Temporal workflow ID", positional=True),
                            _CHANNEL,
                            Argument("run-id", "Specific run ID (optional)", type=str),
                            Argument(
                                "include-errors",
                                "Include error details in the run",
                                action="store_true",
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )
)
