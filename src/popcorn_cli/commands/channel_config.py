"""`popcorn channel-config` — read and edit a channel's config.

```
channel-config show → params set / unset → integrations set / unset
```

`show` is the useful one: it prints the config next to what the channel's
flows actually reference, and the diff between them. That diff is the lint the
CLI never had for a bundle under iteration — `--strict` turns it into an exit
code.

**Every per-key edit is a read-modify-write.** `PUT
/channel-config/parameters` replaces the whole `channel_parameters` section,
so `params set` GETs, merges and PUTs; see `popcorn_core.channel_config`. That
inherits the backend's last-write-wins, which the command help states rather
than hides.

The CLI has no opinion about WHERE config lives. Bundle channels keep it on
`channel_app.config` and legacy channels in S3, the API branches, and every
command here goes through the endpoints so the migration cannot strand it.

Handlers import `..cli` helpers inside the function body: cli.py imports this
package at module load to build the parser, so a module-level import cycles.
"""

from __future__ import annotations

import argparse

from popcorn_core import operations
from popcorn_core.channel_config import (
    fatal_findings,
    merge_parameters,
    parameters_of,
    parse_assignments,
    remove_parameters,
)
from popcorn_core.errors import EXIT_UNHEALTHY, PopcornError

from ..registry import Argument, Command, Subcommand, register

_CHANNEL = Argument("channel", "Channel name (#alerts) or UUID", required=True)


def _render_show(data: dict) -> str:
    lines: list[str] = []

    if data.get("config_error"):
        lines.append(f"⚠ config is malformed: {data['config_error']}")
        lines.append("")
    elif not data.get("config_found"):
        lines.append("This channel has no config yet.")
        lines.append("")

    params = data.get("channel_parameters") or {}
    lines.append("PARAMETERS")
    if params:
        width = max(len(k) for k in params)
        for key in sorted(params):
            lines.append(f"  {key:<{width}}  {_short(params[key])}")
    else:
        lines.append("  (none)")

    integrations = data.get("integrations") or {}
    lines.append("")
    lines.append("INTEGRATIONS")
    if integrations:
        for name in sorted(integrations):
            entry = integrations[name] or {}
            who = entry.get("provider_account") or entry.get("connected_user") or "—"
            lines.append(f"  {name}  {entry.get('provider') or '—'}  {who}")
    else:
        lines.append("  (none)")

    flows = data.get("flows") or []
    lines.append("")
    lines.append("FLOWS")
    if flows:
        for flow in flows:
            if flow.get("error"):
                lines.append(f"  {flow.get('name')}  ⚠ {flow['error']}")
                continue
            usage = flow.get("usage") or {}
            refs = sorted(usage.get("parameters") or [])
            needs = sorted(usage.get("required_integrations") or {})
            detail = ", ".join(f"$channel.{r}" for r in refs) or "no $channel refs"
            if needs:
                detail += f" | integrations: {', '.join(needs)}"
            lines.append(f"  {flow.get('name')}  {detail}")
    else:
        lines.append("  (none attached)")

    comparison = data.get("comparison") or {}
    lines.append("")
    lines.append("DIFF")
    rows = [
        ("missing_parameters", "referenced but not set — runs fail at resolve"),
        ("missing_integrations", "needed but not connected — flows refuse to start"),
        ("provider_mismatches", "connected account contradicts a flow's provider"),
        ("unused_parameters", "set but nothing reads them (legal)"),
        ("unused_integrations", "connected but nothing uses them (legal)"),
    ]
    any_row = False
    for field, explanation in rows:
        values = comparison.get(field) or []
        if not values:
            continue
        any_row = True
        lines.append(f"  {field}: {', '.join(values)}")
        lines.append(f"    {explanation}")
    if not any_row:
        lines.append("  Config and flows agree.")
    return "\n".join(lines)


def _short(value: object, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _channel_config_show(args: argparse.Namespace) -> None:
    import sys

    from ..cli import _get_client, _output

    client = _get_client(args)
    data = operations.inspect_channel_config(client, args.channel)
    fatal = fatal_findings(data.get("comparison") or {})
    _output(args, {**data, "fatal": fatal}, _render_show(data))

    if args.strict and fatal:
        # Non-zero only for what actually breaks a run. Exiting on an unused
        # parameter would make --strict useless on any shared config.
        sys.exit(EXIT_UNHEALTHY)


def _params_set(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    updates = parse_assignments(args.assignment)

    if args.replace:
        section = updates
        note = "Replaced the parameters section"
    else:
        # Read-modify-write: the endpoint replaces the whole section, so
        # sending only the new keys would delete every other parameter.
        current = parameters_of(operations.inspect_channel_config(client, args.channel))
        section = merge_parameters(current, updates)
        note = f"Set {', '.join(sorted(updates))}"

    data = operations.replace_channel_parameters(client, args.channel, section)
    written = parameters_of(data)
    rendered = "\n".join(
        [
            note,
            *(f"  {k} = {_short(written[k])}" for k in sorted(written)),
            "",
            f"{len(written)} parameter{'s' if len(written) != 1 else ''} now set",
        ]
    )
    _output(args, data, rendered)


def _params_unset(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    current = parameters_of(operations.inspect_channel_config(client, args.channel))
    remaining, missing = remove_parameters(current, args.key)
    if len(remaining) == len(current):
        raise PopcornError(
            f"nothing to unset — {', '.join(args.key)} "
            f"{'is' if len(args.key) == 1 else 'are'} not set",
            error_code="not_found",
        )

    data = operations.replace_channel_parameters(client, args.channel, remaining)
    written = parameters_of(data)
    lines = [f"Unset {', '.join(k for k in args.key if k not in missing)}"]
    if missing:
        # Reported, not raised: the end state is what was asked for.
        lines.append(f"  (already absent: {', '.join(missing)})")
    lines += ["", f"{len(written)} parameter{'s' if len(written) != 1 else ''} remain"]
    _output(args, data, "\n".join(lines))


def _integrations_set(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    data = operations.set_channel_integration(client, args.channel, args.name, args.integration_id)
    entry = (data.get("integrations") or {}).get(args.name) or {}
    rendered = "\n".join(
        [
            f"$channel.integrations.{args.name} → "
            f"{entry.get('provider') or 'account'} "
            f"{entry.get('provider_account') or args.integration_id}",
            "",
            "Next: popcorn channel-config show --channel <channel>",
        ]
    )
    _output(args, data, rendered)


def _integrations_unset(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    data = operations.unset_channel_integration(client, args.channel, args.name)
    rendered = "\n".join(
        [
            f"Removed the {args.name} binding",
            "The underlying OAuth grant is untouched — detach it from the "
            "integrations surface, not here.",
        ]
    )
    _output(args, data, rendered)


def _accounts(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    data = operations.list_own_integrations(client)
    items = data.get("integrations") or []
    if not items:
        rendered = "You have no connected accounts."
    else:
        lines = [f"{'ID':<38} {'PROVIDER':<14} ACCOUNT"]
        for item in items:
            lines.append(
                f"{item.get('id', '')!s:<38} "
                f"{item.get('provider') or '—'!s:<14} "
                f"{item.get('provider_account') or item.get('email') or '—'}"
            )
        lines += [
            "",
            "Use an ID with: popcorn channel-config integrations set "
            "--channel <channel> --name <name> --integration-id <id>",
        ]
        rendered = "\n".join(lines)
    _output(args, data, rendered)


_NAME = Argument("name", "Integration name, as $channel.integrations.<name>", required=True)

register(
    Command(
        name="channel-config",
        category="flows",
        description="Channel config — inspect it against the flows, and edit it",
        subcommands=[
            Subcommand(
                "show",
                "Config, the flows' $channel.* usage, and the diff between them",
                _channel_config_show,
                [
                    _CHANNEL,
                    Argument(
                        "strict",
                        "Exit 5 when a finding would make a run fail",
                        action="store_true",
                    ),
                ],
            ),
            Subcommand(
                "params",
                "Set or unset channel_parameters (read-modify-write)",
                None,
                [],
                subcommands=[
                    Subcommand(
                        "set",
                        "Set key=value, keeping the other parameters",
                        _params_set,
                        [
                            _CHANNEL,
                            Argument(
                                "assignment",
                                "key=value (repeatable); values parse as JSON when they can",
                                positional=True,
                                nargs="+",
                            ),
                            Argument(
                                "replace",
                                "Make these the ONLY parameters, dropping the rest",
                                action="store_true",
                            ),
                        ],
                    ),
                    Subcommand(
                        "unset",
                        "Remove parameters, keeping the rest",
                        _params_unset,
                        [
                            _CHANNEL,
                            Argument(
                                "key",
                                "Parameter name (repeatable)",
                                positional=True,
                                nargs="+",
                            ),
                        ],
                    ),
                ],
            ),
            Subcommand(
                "integrations",
                "Bind or unbind a named integration",
                None,
                [],
                subcommands=[
                    Subcommand(
                        "set",
                        "Point a name at one of YOUR connected accounts",
                        _integrations_set,
                        [
                            _CHANNEL,
                            _NAME,
                            Argument(
                                "integration-id",
                                "One of your account ids (see 'channel-config accounts')",
                                required=True,
                            ),
                        ],
                    ),
                    Subcommand(
                        "unset",
                        "Remove a named binding (leaves the OAuth grant)",
                        _integrations_unset,
                        [_CHANNEL, _NAME],
                    ),
                ],
            ),
            Subcommand(
                "accounts",
                "Your connected accounts, with the ids 'integrations set' needs",
                _accounts,
                [],
            ),
        ],
    )
)
