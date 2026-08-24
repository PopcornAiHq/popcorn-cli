"""`popcorn app` — author an app bundle from a checkout of it.

```
app fork → app checkout → edit → template check → app publish → app apply
```

`fork` leads even though a checkout is what you edit: publishing needs the
channel bound to a version this workspace OWNS, so `publish` on a
product-bound channel cannot work (`ChannelNotOnForkError`).

Two groups of commands, split by what they act on:

- `list` and `fork` act on a CHANNEL, so they take `--channel`. `list` takes
  it too, which is not an oversight: the reads require `conversation_id`
  because that is the field the API authorizes against (popcorn-backend#1801).
- `checkout`, `publish`, `apply` and `status` act on a checkout DIRECTORY and
  read the channel out of its baseline. `--channel` stays accepted there for
  baselines written by 0.19.0, which predate the field.

Handlers import `..cli` helpers inside the function body: cli.py imports this
package at module load to build the parser, so a module-level import cycles.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from popcorn_core import operations
from popcorn_core.app_checkout import (
    BASELINE_FILE,
    Baseline,
    baseline_from_response,
    files_from_response,
    occupied,
    read_baseline,
    write_baseline,
    write_tree,
)
from popcorn_core.app_publish import (
    collect_tree,
    diff_tree,
    ignored_note,
    local_digest,
    manifest_version,
    parse_semver,
    preserved_note,
    publish_payload,
    require_bump,
)
from popcorn_core.errors import PopcornError
from popcorn_core.resolve import resolve_conversation

from ..registry import Argument, Command, Subcommand, register

_CHANNEL = Argument("channel", "Channel name (#alerts) or UUID", required=True)
# The same argument where the baseline supplies a default.
_CHANNEL_OPT = Argument("channel", "Channel to act on (default: the checkout's baseline)")
_DIRECTORY = Argument(
    "directory",
    "Checkout directory (default: .)",
    positional=True,
    nargs="?",
)


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
    from ..cli import _get_client, _output

    client = _get_client(args)
    # Resolved here rather than inside the operation because the baseline
    # stores it. resolve_conversation caches, so naming it twice is one
    # request, and it passes a UUID straight through.
    conv_id = resolve_conversation(client, args.channel)
    resp = operations.get_channel_app_files(client, conv_id)
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
    baseline = baseline_from_response(resp, files, conversation_id=conv_id)
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


# ---------------------------------------------------------------------------
# Working-copy commands
# ---------------------------------------------------------------------------


def _directory(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "directory", None) or ".")


def _require_baseline(directory: Path) -> Baseline:
    baseline = read_baseline(directory)
    if baseline is None:
        raise PopcornError(
            f"no {BASELINE_FILE} in {directory} — this is not an app checkout",
            error_code="not_found",
            hint="run: popcorn app checkout --channel '#your-channel'",
        )
    return baseline


def _channel_of(args: argparse.Namespace, baseline: Baseline) -> str:
    """The channel to act on: the flag if given, else the baseline's.

    A v1 baseline (popcorn-cli 0.19.0) has no channel in it, which is the one
    case that still needs the flag — named as such, because "pass --channel"
    without the reason reads like a missing feature.
    """
    if getattr(args, "channel", None):
        return str(args.channel)
    if baseline.conversation_id:
        return baseline.conversation_id
    raise PopcornError(
        f"{BASELINE_FILE} records no channel — it was written by an older "
        "popcorn (0.19.0 or earlier)",
        error_code="validation",
        hint="pass --channel, or re-run 'popcorn app checkout' to refresh it",
    )


def _fetch_base(client, conversation: str, baseline: Baseline) -> dict:
    """The channel's current tree, refusing when it is not what we edited.

    Three outcomes, and collapsing them into one "re-checkout" message would
    be wrong in two: the binding may be BEHIND us (our own publish is still
    installing), ahead/elsewhere (someone moved the channel), or the expected
    match.
    """
    resp = operations.get_channel_app_files(client, conversation)
    current_id = resp.get("version_id")
    if current_id == baseline.base_version_id:
        return resp

    current = str(resp.get("semver") or "")
    if _is_behind(current, baseline.semver):
        raise PopcornError(
            f"the channel still runs {resp.get('app')} {current}; "
            f"{baseline.semver} is published but its install has not landed "
            "yet",
            error_code="conflict",
            hint="wait for the install, then re-run — 'popcorn app status' shows both versions",
            retryable=True,
        )
    raise PopcornError(
        f"the channel moved to {resp.get('app')} {current} "
        f"(version {current_id}) since this checkout of {baseline.semver} "
        f"(version {baseline.base_version_id})",
        error_code="conflict",
        hint="re-run 'popcorn app checkout' and redo the edits on the current tree",
    )


def _is_behind(current: str, baseline_semver: str) -> bool:
    """Whether the channel's version is older than the baseline's.

    Unparseable either side means we cannot tell, and guessing "behind" would
    invite an endless retry — so say no and let the generic message stand.
    """
    try:
        return parse_semver(current) < parse_semver(baseline_semver)
    except PopcornError:
        return False


def _app_fork(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    data = operations.fork_channel_app(client, args.channel, args.name)

    status = data.get("status")
    headline = {
        "created": "Forked",
        "already_fork": "Already on this workspace's fork of",
        "adopting": "Adopting this workspace's existing fork of",
    }.get(str(status), f"{status}:")
    line = data.get("fork_name") or "default"
    rendered = [f"{headline} {data.get('app')} {data.get('semver')} (line {line})"]
    if data.get("message"):
        rendered.append(str(data["message"]))
    if status == "adopting":
        rendered.append(
            "The install is asynchronous — 'popcorn app list' shows when the channel has moved."
        )
    rendered += ["", "Next: popcorn app checkout --channel <channel>"]
    _output(args, data, "\n".join(rendered))


def _app_publish(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    directory = _directory(args)
    baseline = _require_baseline(directory)
    client = _get_client(args)
    conversation = _channel_of(args, baseline)

    # Product-bound is refused here rather than by the 409, because the fix is
    # a different command and the server's message cannot know that.
    if baseline.kind != "fork":
        raise PopcornError(
            f"{baseline.app} {baseline.semver} is a PRODUCT version — a "
            "publish lands on a fork line this workspace owns",
            error_code="conflict",
            hint="run 'popcorn app fork --channel <channel>', then re-run "
            "'popcorn app checkout' before publishing",
        )

    local = collect_tree(directory)
    version = manifest_version(local.files)
    resp = _fetch_base(client, conversation, baseline)
    diff = diff_tree(files_from_response(resp), local.files)
    if diff.empty:
        raise PopcornError(
            f"nothing to publish — {directory} matches {baseline.app} {baseline.semver}",
            error_code="validation",
        )
    require_bump(version, baseline.semver)

    payload = publish_payload(baseline.base_version_id, diff, args.changelog)
    result = operations.publish_channel_app(client, conversation, payload)

    # The working copy now corresponds to the PUBLISHED version, so the
    # baseline moves with it — otherwise the next edit needs a fresh
    # checkout, which is the loop this command exists to close. The channel
    # catches up when the install lands; until then _fetch_base reports the
    # gap rather than pretending it is not there.
    published = Baseline(
        app=str(result.get("app") or baseline.app),
        kind="fork",
        semver=str(result.get("semver") or version),
        base_version_id=int(result.get("version_id") or 0),
        tree_digest=local_digest(local.files),
        fork_name=baseline.fork_name,
        conversation_id=baseline.conversation_id,
    )
    write_baseline(directory, published)

    rendered = [
        f"Published {published.app} {published.semver} "
        f"(version {published.base_version_id})"
        + ("" if result.get("created", True) else " — already existed, no-op"),
        *diff.summary(),
        "",
    ]
    if local.ignored:
        rendered.append(ignored_note(local.ignored))
    if diff.preserved:
        rendered.append(preserved_note(diff.preserved))
    if result.get("install_workflow_id"):
        rendered.append(f"Installing on this channel: {result['install_workflow_id']}")
        rendered.append("Next: popcorn app status")
    else:
        rendered.append("Next: popcorn app apply")
    _output(args, {**result, "diff": diff.summary()}, "\n".join(rendered))


def _app_apply(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    directory = _directory(args)
    baseline = read_baseline(directory)
    conversation = (
        str(args.channel)
        if getattr(args, "channel", None)
        else _channel_of(args, _require_baseline(directory))
    )
    data = operations.apply_channel_app(client, conversation)

    status = str(data.get("status") or "")
    target = f"{data.get('app')} {data.get('target_semver') or '?'}"
    rendered = {
        "started": f"Applying {target} to this channel",
        "already_current": f"Already on {target} — nothing to do",
        "blocked_install_in_progress": (
            "Another install holds this channel's lock — re-run "
            "'popcorn app apply' once it finishes"
        ),
    }.get(status, f"{status}: {target}")
    lines = [rendered]
    if data.get("install_workflow_id"):
        lines.append(f"Workflow: {data['install_workflow_id']}")
    if baseline is not None and status == "started":
        lines.append("")
        lines.append("Next: popcorn app status")
    _output(args, data, "\n".join(lines))


def _app_status(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    directory = _directory(args)
    baseline = _require_baseline(directory)
    client = _get_client(args)
    conversation = _channel_of(args, baseline)

    local = collect_tree(directory)
    resp = operations.get_channel_app_files(client, conversation)
    base_files = files_from_response(resp)
    # Diffed against the CHANNEL's tree, not the baseline's digest: status is
    # the command you run when the two disagree, so it must not refuse the
    # way publish does.
    diff = diff_tree(base_files, local.files)
    channel_id = resp.get("version_id")
    in_sync = channel_id == baseline.base_version_id

    data = {
        "directory": str(directory),
        "app": baseline.app,
        "kind": baseline.kind,
        "baseline_semver": baseline.semver,
        "baseline_version_id": baseline.base_version_id,
        "channel_semver": resp.get("semver"),
        "channel_version_id": channel_id,
        "in_sync": in_sync,
        "dirty": local_digest(local.files) != baseline.tree_digest,
        "added": diff.added,
        "changed": diff.changed,
        "deleted": diff.deletes,
        "ignored": local.ignored,
        "preserved": diff.preserved,
    }

    lines = [
        f"{baseline.app} {baseline.semver} ({baseline.kind}) in {directory}",
    ]
    if in_sync:
        lines.append(f"Channel runs the same version ({channel_id}).")
    elif _is_behind(str(resp.get("semver") or ""), baseline.semver):
        lines.append(
            f"Channel still runs {resp.get('semver')} — the install of "
            f"{baseline.semver} has not landed yet."
        )
    else:
        lines.append(
            f"Channel moved to {resp.get('semver')} (version {channel_id}) — "
            "re-run 'popcorn app checkout'."
        )
    lines.append("")
    if diff.empty:
        lines.append("Working copy matches the channel's tree.")
    else:
        lines.append("Uncommitted edits:")
        lines += diff.summary()
    if local.ignored:
        lines.append("")
        lines.append(ignored_note(local.ignored))
    if diff.preserved:
        lines.append("")
        lines.append(preserved_note(diff.preserved))
    _output(args, data, "\n".join(lines))


register(
    Command(
        name="app",
        category="flows",
        description="App bundles — fork, check out, edit and publish one",
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
            Subcommand(
                "fork",
                "Give this workspace its own fork line of the channel's app",
                _app_fork,
                [
                    _CHANNEL,
                    Argument(
                        "name",
                        "Fork line name (default: the single line, or 'default')",
                    ),
                ],
            ),
            Subcommand(
                "publish",
                "Publish a checkout's edits as the next version on its fork line",
                _app_publish,
                [
                    _DIRECTORY,
                    Argument("changelog", "What changed, recorded on the version"),
                    _CHANNEL_OPT,
                ],
            ),
            Subcommand(
                "apply",
                "Bring the channel up to its fork line's head",
                _app_apply,
                [_DIRECTORY, _CHANNEL_OPT],
            ),
            Subcommand(
                "status",
                "Compare a checkout against the channel's current version",
                _app_status,
                [_DIRECTORY, _CHANNEL_OPT],
            ),
        ],
    )
)
