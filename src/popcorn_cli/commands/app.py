"""`popcorn app` — check an app bundle out of a channel and inspect versions.

`app checkout` materializes the channel's bound bundle as files you can edit,
alongside a baseline recording which version they came from. `app list` shows
what exists: each app's product line, any fork line this workspace owns, and
what the channel is currently running.

Both take `--channel`, including `list`. That is not an oversight: the reads
these wrap require `conversation_id`, because it is the field the API
authorizes the caller against — see popcorn-backend#1801.

Handlers import `..cli` helpers inside the function body: cli.py imports this
package at module load to build the parser, so a module-level import cycles.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from popcorn_core import operations
from popcorn_core.app_checkout import (
    baseline_from_response,
    files_from_response,
    occupied,
    write_baseline,
    write_tree,
)

from ..registry import Argument, Command, Subcommand, register

_CHANNEL = Argument("channel", "Channel name (#alerts) or UUID", required=True)


def _render_list(data: dict) -> str:
    apps = data.get("apps") or []
    channel = data.get("channel")

    lines: list[str] = []
    if not apps:
        lines.append("No app bundles visible to this workspace.")
    else:
        lines.append(f"{'APP':<24} {'KIND':<8} {'LINE':<12} {'VERSION':<10} FLOWS")
        for item in apps:
            flows = ", ".join(item.get("flows") or []) or "—"
            lines.append(
                f"{item.get('app', ''):<24} "
                f"{item.get('kind', ''):<8} "
                f"{item.get('fork_name') or '—':<12} "
                f"{item.get('semver', ''):<10} "
                f"{flows}"
            )

    lines.append("")
    if channel:
        line = channel.get("fork_name") or "—"
        lines.append(
            f"This channel runs {channel.get('app')} "
            f"{channel.get('semver')} ({channel.get('kind')}, line {line})"
        )
    else:
        lines.append("This channel does not run an app bundle.")
    return "\n".join(lines)


def _app_list(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    data = operations.list_channel_apps(client, args.channel)
    _output(args, data, _render_list(data))


def _app_checkout(args: argparse.Namespace) -> None:
    from popcorn_core.errors import PopcornError

    from ..cli import _get_client, _output

    client = _get_client(args)
    resp = operations.get_channel_app_files(client, args.channel)
    files = files_from_response(resp)
    if not files:
        raise PopcornError(
            f"{resp.get('app') or 'this channel'} returned no files to check out",
            error_code="not_found",
        )

    # Default to ./<app> rather than '.', so a bare checkout in a working
    # directory cannot scatter bundle files over whatever is already there.
    directory = Path(args.directory) if args.directory else Path(resp.get("app") or "app")

    if occupied(directory) and not args.force:
        raise PopcornError(
            f"{directory} is not empty — pass --force to overwrite its bundle files",
            error_code="validation",
        )

    directory.mkdir(parents=True, exist_ok=True)
    written = write_tree(directory, files)
    baseline = baseline_from_response(resp, files)
    write_baseline(directory, baseline)

    data = {
        "directory": str(directory),
        "app": baseline.app,
        "kind": baseline.kind,
        "semver": baseline.semver,
        "base_version_id": baseline.base_version_id,
        "tree_digest": baseline.tree_digest,
        "files": written,
    }
    rendered = "\n".join(
        [
            f"Checked out {baseline.app} {baseline.semver} ({baseline.kind}) into {directory}",
            *(f"  {p}" for p in written),
            "",
            f"{len(written)} file{'s' if len(written) != 1 else ''}, "
            f"baseline version {baseline.base_version_id}",
            "",
            f"Next: popcorn template check {directory}",
        ]
    )
    _output(args, data, rendered)


register(
    Command(
        name="app",
        category="flows",
        description="App bundles — inspect versions and check one out for editing",
        subcommands=[
            Subcommand(
                "list",
                "Show each app's product and fork lines, and this channel's binding",
                _app_list,
                [_CHANNEL],
            ),
            Subcommand(
                "checkout",
                "Write the channel's bound bundle to disk, with a baseline",
                _app_checkout,
                [
                    _CHANNEL,
                    Argument(
                        "directory",
                        "Target directory (default: ./<app>)",
                        positional=True,
                        nargs="?",
                    ),
                    Argument(
                        "force",
                        "Overwrite bundle files in a non-empty directory",
                        action="store_true",
                    ),
                ],
            ),
        ],
    )
)
