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
    from pathlib import Path

    from popcorn_core.client import APIClient

_CHANNEL = Argument("channel", "Channel name (#general) or UUID", required=True)
# The same argument where an app checkout can supply it. `flow validate` needs
# a channel only because the API authorizes against one — the parse and static
# validation behind it are workspace-free — so making an author name a channel
# they already checked out from is friction with nothing behind it.
_CHANNEL_OPT = Argument("channel", "Channel name or UUID (default: the checkout's)")

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
    from popcorn_core.errors import EXIT_TIMEOUT, PopcornError

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
                # Not EXIT_VALIDATION — the request was fine, the deadline just
                # elapsed and the run is likely still going. Agents need to tell
                # "wait longer / poll again" apart from "your input was wrong",
                # in the exit code and in the JSON envelope alike.
                exit_code=EXIT_TIMEOUT,
                retryable=True,
            )
        time.sleep(_POLL_SECONDS)


# Bundle files that are not flows. The manifest is `template check`'s job,
# not this validator's; config/strings are template data. Mirrors the
# backend's own reserved set -- `strings.yaml` is a fourth reserved name
# alongside manifest/AGENT/README, and every shipped template has one.
_NOT_A_FLOW = {"manifest.yaml", "config.yaml", "strings.yaml"}


def _flow_import(args: argparse.Namespace) -> None:
    """Fenced: always raises with the real publish path.

    Registered so `flow import` explains itself rather than dying as an unknown
    subcommand, and so `--channel`/`--dry-run` still parse -- an author who
    typed the old command gets the message, not an argparse error about it.

    No client is built and no auth is required: the endpoint is gone for
    everyone, so needing a login to be told so would be its own dead end.
    """
    from popcorn_core.errors import PopcornError

    raise PopcornError(operations.TEMPLATE_INSTALL_REMOVED, error_code="validation")


def _validate_channel(args: argparse.Namespace, target: Path) -> str:
    """The channel to validate against: the flag, else the checkout's baseline.

    `POST /customer-flows/validate` requires a `conversation_id` purely as
    authorization — declaring the param is what makes a non-admin prove
    channel membership — so the value is never read by the validation itself.
    In the fork-and-revise loop the author always has a channel: they checked
    the bundle out of one, and `app publish`/`apply`/`status` already read it
    back out of the baseline. This makes validate consistent with them.
    """
    from popcorn_core.app_checkout import BASELINE_FILE, read_baseline
    from popcorn_core.errors import PopcornError

    if getattr(args, "channel", None):
        return str(args.channel)

    directory = target if target.is_dir() else target.parent
    baseline = read_baseline(directory)
    if baseline is not None and baseline.conversation_id:
        return baseline.conversation_id

    raise PopcornError(
        f"no --channel given and no {BASELINE_FILE} in {directory}",
        error_code="validation",
        hint="pass --channel, or run this from an 'popcorn app checkout' directory",
    )


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

    channel = _validate_channel(args, target)

    results: list[dict[str, Any]] = []
    lines: list[str] = []
    bad = 0
    for path in files:
        resp = operations.validate_flow_yaml(client, channel, path.read_text())
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


def _schema_type(spec: dict[str, Any]) -> str:
    """A one-word type for an argument, from its JSON Schema fragment.

    `anyOf` is how pydantic renders an optional, so `str | None` arrives as a
    two-branch union; showing "string" and letting `required` carry the
    optionality reads better than "string|null".
    """
    if "type" in spec:
        return str(spec["type"])
    branches = [b.get("type") for b in spec.get("anyOf", []) if b.get("type") != "null"]
    return str(branches[0]) if branches else "?"


def _activity_detail(entry: dict[str, Any]) -> str:
    """The long form for a single activity — what `--name` is for.

    A one-line row answers "does this exist"; the question that sends someone
    to the catalog is "what do I pass it", so this leads with the arguments.
    The raw JSON Schema is one `--json` away for anything this elides.
    """
    args_schema = entry.get("args_schema") or {}
    props = args_schema.get("properties") or {}
    required = set(args_schema.get("required") or [])

    lines = [
        f"{entry.get('name', '?')}",
        f"  {entry.get('tier', '?')}/{entry.get('category', '?')}  "
        f"{entry.get('status', '?')}  v{entry.get('version', '?')}",
    ]
    if entry.get("description"):
        # rstrip: a docstring's blank separator line would otherwise render
        # as two spaces, and an argument with no description as a trailing run.
        lines += ["", *(f"  {ln}".rstrip() for ln in entry["description"].splitlines())]

    if props:
        lines += ["", "  Arguments:"]
        for key in sorted(props, key=lambda k: (k not in required, k)):
            spec = props[key] or {}
            flag = "required" if key in required else "optional"
            desc = (spec.get("description") or "").splitlines()
            lines.append(
                f"    {key:<26} {_schema_type(spec):<9} {flag:<9} "
                f"{desc[0][:60] if desc else ''}".rstrip()
            )
    elif args_schema:
        lines += ["", "  Arguments: none"]

    if entry.get("result_description"):
        lines += ["", "  Returns:", f"    {entry['result_description']}"]
    return "\n".join(lines)


def _flow_activities(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    # Filters go to the server, which owns the taxonomy and rejects an unknown
    # value with a message naming the valid ones. Narrowing the response here
    # would turn a typo back into zero rows reading as "no such activities".
    resp = operations.list_activity_catalog(
        _get_client(args),
        name=getattr(args, "name", None),
        tier=getattr(args, "tier", None),
        status=getattr(args, "status", None),
        category=getattr(args, "category", None),
        view="summary" if getattr(args, "summary", False) else None,
    )
    activities = resp.get("activities", [])

    if getattr(args, "name", None):
        # An unregistered name is a 404 from the server, so reaching here with
        # a name means exactly one row.
        _output(args, resp, _activity_detail(activities[0]))
        return

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
    # Name->id resolution and the conversation_id default both live in
    # operations.run_flow, which already resolves the conversation.
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


def _failure_lines(failure: dict[str, Any] | None, indent: str) -> list[str]:
    """Render a FailureInfo and its `cause` chain, outermost first.

    The API unwinds `cause` recursively and the root is usually the line that
    actually explains the failure, so the whole chain is worth printing.
    """
    lines: list[str] = []
    node: dict[str, Any] | None = failure
    prefix = ""
    while isinstance(node, dict):
        kind = node.get("type")
        label = f"{kind}: " if kind else ""
        lines.append(f"{indent}{prefix}{label}{node.get('message', '')}")
        node = node.get("cause")
        prefix = "caused by: "
        indent += "  "
    return lines


def _run_detail_lines(run: dict[str, Any]) -> list[str]:
    """Render one flow run: header, in-flight activities, failures.

    `--include-errors` populates `error_history`; without rendering it here the
    flag was a no-op outside `--json`. Note the API gives no DSL step id — an
    activity is identified only by `activity_type`, because the interpreter
    schedules activities without setting Temporal's activity_id.
    """
    lines = [
        f"{run.get('status', '?')}  {run.get('workflow_id', '?')}",
        f"  type:    {run.get('workflow_type', '-')}",
        f"  run_id:  {run.get('run_id', '-')}",
        f"  started: {run.get('start_time', '-')}",
        f"  closed:  {run.get('close_time', '-')}",
    ]

    pending = run.get("current_activities") or []
    if pending:
        lines.append(f"  in flight ({len(pending)}):")
        for a in pending:
            lines.append(
                f"    {a.get('activity_type', '?')!s:<32} "
                f"{a.get('state', '?')}  "
                f"attempt {a.get('attempt', '?')}/{a.get('maximum_attempts', '?')}"
            )
            # The failure that caused the current retry — the reason a run is
            # stuck, and otherwise invisible.
            if a.get("last_failure"):
                lines.extend(_failure_lines(a["last_failure"], "      "))

    if run.get("failure"):
        lines.append("  failure:")
        lines.extend(_failure_lines(run["failure"], "    "))

    history = run.get("error_history") or []
    if history:
        lines.append(f"  activity failures ({len(history)}):")
        for e in history:
            meta = "  ".join(str(v) for v in (e.get("time"), e.get("type")) if v is not None)
            lines.append(
                f"    {e.get('activity_type', '?')!s:<32} "
                f"attempt {e.get('attempt', '?')}"
                f"{'  ' + meta if meta else ''}"
            )
            if e.get("message"):
                lines.append(f"      {e['message']}")

    return lines


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
    _output(args, resp, "\n".join(_run_detail_lines(run)))


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
                        "name",
                        "Exact wire name — prints that activity's arguments",
                        type=str,
                    ),
                    Argument(
                        "summary",
                        "Drop the JSON schemas (~500 KB to ~45 KB)",
                        action="store_true",
                    ),
                    # tier and status keep their choices so a typo fails
                    # offline; the server validates them again. `category`
                    # deliberately has none — a domain exists exactly when an
                    # activity is registered under it, so any list here would
                    # be a stale copy. The server answers 400 naming the live
                    # set instead.
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
                "Removed — prints where bundles install from now",
                _flow_import,
                [
                    Argument("directory", "Template bundle directory", positional=True),
                    _CHANNEL,
                    Argument(
                        "dry-run",
                        "Accepted and ignored; the command is removed",
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
                    _CHANNEL_OPT,
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
