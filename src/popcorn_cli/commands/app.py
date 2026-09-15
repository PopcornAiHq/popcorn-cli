"""`popcorn app` — author an app bundle from a checkout of it.

```
app fork → app checkout → edit → template check → app publish
```

`apply` is NOT a step in that loop. `publish` starts the install itself and it
converges on its own; `apply` is the retry for the cases where it did not —
the channel was locked, another install held it, or the install failed. Run it
when `app status` says the channel is still behind its line, not by habit.

`fork` leads even though a checkout is what you edit: publishing needs a
checkout of a version this workspace OWNS, so `publish` from a product
checkout cannot work (`PublishBaseNotForkError`). `checkout --fork` does both
in one command, since the pair is almost always run together; `fork` stays a
command of its own, and a checkout WITHOUT it stays the way to read what a
channel runs without touching it.

A checkout is the fork line's HEAD, not what the channel happens to run. A
publish is a line operation and must be based on the head (popcorn-backend
#1985); the two differ only while the channel lags its line — a head whose
install has not landed, or failed — and that is the case where a checkout of
the bound tree used to leave the line stuck. `checkout` says so when it
happens; `status` shows both versions.

Two groups of commands, split by what they act on:

- `list`, `lines` and `fork` act on a CHANNEL, so they take `--channel`.
  `list` takes it too, which is not an oversight: the reads require
  `conversation_id` because that is the field the API authorizes against
  (popcorn-backend#1801). `lines` reports the WORKSPACE's fork lines and
  needs the channel only for that authorization.
- `checkout`, `publish`, `apply` and `status` act on a checkout DIRECTORY and
  read the channel out of its baseline. `--channel` stays accepted there for
  baselines written by 0.19.0, which predate the field.

`status` is the one command in both groups: with a checkout it compares the
working copy against the line and the channel, and with `--channel` outside
one it answers "has my publish landed here?" from server state alone.

Handlers import `..cli` helpers inside the function body: cli.py imports this
package at module load to build the parser, so a module-level import cycles.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from popcorn_core import flow_rules, operations

# `baseline_changelog` is app_checkout's `manifest_changelog`, aliased because
# app_publish exports a same-named function doing a different job. This one
# NORMALISES the note, so reflowing a block scalar is not read as an edit, and
# it is what the baseline records for `template check` to compare against.
# app_publish's returns the raw working-copy value, only ever to warn about it.
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
from popcorn_core.app_checkout import (
    manifest_changelog as baseline_changelog,
)
from popcorn_core.app_publish import (
    BUMP_PARTS,
    bump_manifest_text,
    collect_tree,
    diff_tree,
    fork_line_reach,
    ignored_note,
    local_digest,
    manifest_changelog,
    manifest_file,
    manifest_version,
    next_version,
    parse_semver,
    preserved_note,
    publish_payload,
    require_bump,
    unrecognized_code_note,
    unrecognized_code_paths,
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
    flag_alias="--dir",
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


# How many channels ride each line is the safety information `app lines`
# exists to give, and the API does not carry it: `/apps/list` returns lineage
# heads only, and the backend's own per-line channel count
# (`_count_converging_channels`) lives behind `publish` and is exposed
# nowhere. The CLI could approximate it by listing channels and reading each
# one's binding, and deliberately does not: that enumerates only the channels
# the CALLER can see, so it under-counts exactly when the answer matters and
# would report "no channels" for a line another member's channel is bound to.
# An undercount presented as a safety check is worse than an honest gap, so
# the command names the gap instead. KEW-2371 carries the backend half.
_NO_CHANNEL_COUNT = (
    "How many channels ride each line is not shown: the API reports lineage "
    "heads only, with no per-line channel count."
)

# Deleting a fork line has no API behind it either — the whole `/apps`
# surface is list, tree, file, files, fork, publish and apply — so `app lines
# delete` is not implemented rather than shipped as a command that dead-ends.
_NO_DELETE = (
    "Deleting a fork line is not possible yet: the API has no endpoint for "
    "it. Lines accumulate until it does (KEW-2371)."
)


def _render_lines(lines_data: list[dict], channel: str) -> str:
    from ..formatting import format_timestamp

    if not lines_data:
        return (
            f"This workspace owns no fork lines of any app visible to {channel}.\n"
            "\n"
            "'popcorn app checkout --channel <channel> --fork' makes the first one."
        )

    rendered = [f"{'LINE':<30} {'APP':<24} {'HEAD':<10} PUBLISHED"]
    for item in lines_data:
        rendered.append(
            f"{item.get('fork_name') or 'default':<30} "
            f"{item.get('app', ''):<24} "
            f"{item.get('semver', ''):<10} "
            f"{format_timestamp(item.get('published_at'))}"
        )
    count = len(lines_data)
    rendered += [
        "",
        f"{count} fork line{'s' if count != 1 else ''} in this workspace.",
        _NO_CHANNEL_COUNT,
        _NO_DELETE,
    ]
    return "\n".join(rendered)


def _app_lines(args: argparse.Namespace) -> None:
    """This workspace's fork lines, across apps — not this channel's.

    `app list` answers a per-channel question (what does THIS channel run, and
    what could it run) and buries the line inventory in it, one row per line
    mixed with product entries and each row's flow list. Six throwaway lines
    in one workspace is a routine afternoon (KEW-2371) and nothing listed them
    on their own.

    `--channel` is required and is not a filter: `/apps/list` authorizes
    against `conversation_id` (popcorn-backend#1801), so a workspace-level
    read still has to name a channel it can reach. The lines that come back
    are the workspace's.
    """
    from ..cli import _get_client, _output

    client = _get_client(args)
    data = operations.list_channel_apps(client, args.channel)
    wanted = getattr(args, "app", None)
    lines_data = sorted(
        (
            item
            for item in (data.get("apps") or [])
            if item.get("kind") == "fork" and (not wanted or item.get("app") == wanted)
        ),
        key=lambda i: (str(i.get("app") or ""), str(i.get("fork_name") or "")),
    )
    payload = {
        "channel": args.channel,
        "lines": lines_data,
        # Stated on the wire too, so a script reading --json is told the count
        # is absent rather than inferring zero from a missing key.
        "channel_counts_available": False,
        "delete_supported": False,
    }
    _output(args, payload, _render_lines(lines_data, str(args.channel)))


def _app_checkout(args: argparse.Namespace) -> None:
    from ..cli import _confirm_force, _get_client, _output

    client = _get_client(args)
    # Resolved here rather than inside the operation because the baseline
    # stores it. resolve_conversation caches, so naming it twice is one
    # request, and it passes a UUID straight through.
    conv_id = resolve_conversation(client, args.channel)

    # Before the read, not after: the fork re-binds the channel, so a checkout
    # taken first would be of the product tree and the baseline would record
    # `kind: product` — the very state `publish` refuses.
    #
    # `is not None` rather than truthiness: `--fork` with no value parses to
    # the const "", which means "fork, infer the line" and must not read as
    # "no --fork". Absent, it stays None and this whole branch is skipped —
    # a fork-less checkout is a legitimate read of what a channel runs.
    forked = None
    if getattr(args, "fork", None) is not None:
        forked = _fork(args, client, conv_id, args.fork or None)

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

    # `_confirm_force`, not `_confirm`: this overwrites files the author may
    # be the only holder of, and `-y` must not be enough to lose them.
    if occupied(directory) and not _confirm_force(
        args, f"{directory} is not empty — overwrite its bundle files?"
    ):
        raise PopcornError(
            f"{directory} is not empty — its bundle files were left as they are",
            error_code="validation",
            hint="pass --force to overwrite them",
        )

    directory.mkdir(parents=True, exist_ok=True)
    written = write_tree(directory, files)
    baseline = baseline_from_response(resp, files, conversation_id=conv_id)
    write_baseline(directory, baseline)

    # What the channel runs, alongside what was served. An API older than
    # popcorn-backend #1985 sends neither field; then the served version IS
    # the bound one and there is nothing to note.
    channel_id = resp.get("bound_version_id", baseline.base_version_id)
    channel_semver = str(resp.get("bound_semver") or baseline.semver)
    data = {
        "directory": str(directory),
        "app": baseline.app,
        "kind": baseline.kind,
        "semver": baseline.semver,
        "base_version_id": baseline.base_version_id,
        "channel_semver": channel_semver,
        "channel_version_id": channel_id,
        "tree_digest": baseline.tree_digest,
        "files": written,
    }
    if forked is not None:
        data["fork"] = forked
    lines = [
        *(_fork_lines(forked) if forked is not None else []),
        f"Checked out {baseline.app} {baseline.semver} ({baseline.kind}) into {directory}",
    ]
    if channel_id != baseline.base_version_id:
        lines.append(
            f"Note: this channel still runs {baseline.app} {channel_semver}; "
            f"{baseline.semver} is the fork line's head — edits publish on top of "
            "the head and the channel moves straight to the new version."
        )
    lines += [
        *(f"  {p}" for p in written),
        "",
        f"{len(written)} file{'s' if len(written) != 1 else ''}, "
        f"baseline version {baseline.base_version_id}",
        "",
        f"Next: popcorn template check {directory}",
    ]
    _output(args, data, "\n".join(lines))


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
            # No "run:" prefix — the renderer supplies the verb (KEW-2373).
            hint="popcorn app checkout --channel '#your-channel'",
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
    """The fork line's head, refusing when it is not the version we edited.

    The diff is computed against this tree, so it must be the one the
    checkout came from — and the head is what a publish must be based on
    (popcorn-backend #1985). What the CHANNEL runs plays no part: a channel
    still behind its line (a head whose install has not landed, or failed)
    publishes fine from a checkout of that head. A head past the baseline
    means someone else published on the line; the server would refuse the
    stale base, and the answer is the same here: check out again.
    """
    resp = operations.get_channel_app_files(client, conversation, ref="head")
    head_id = resp.get("version_id")
    if head_id == baseline.base_version_id:
        return resp
    raise PopcornError(
        f"the fork line moved to {resp.get('app')} {resp.get('semver')} "
        f"(version {head_id}) since this checkout of {baseline.semver} "
        f"(version {baseline.base_version_id})",
        error_code="conflict",
        hint="re-run 'popcorn app checkout' and redo the edits on the current tree",
    )


def _inferred_fork_line(client, conversation: str) -> dict | None:
    """The line a NAMELESS fork would adopt, when there is exactly one.

    `fork_channel_app` posts `{}` and the 0/1/2+ decision is the server's, so
    the only way to know which line is about to be adopted is to ask: one
    `kind: "fork"` entry per line comes back from `app list`. Filtered to the
    app the channel actually runs, because a workspace can own fork lines of
    apps this channel has nothing to do with.

    None for both the 0 case (the server mints `default`, nothing to disclose)
    and the 2+ case (the server refuses and lists them — a good message, and
    duplicating it here is a second copy to drift).
    """
    data = operations.list_channel_apps(client, conversation)
    app = (data.get("channel") or {}).get("app")
    lines = [
        item
        for item in (data.get("apps") or [])
        if item.get("kind") == "fork" and (not app or item.get("app") == app)
    ]
    return lines[0] if len(lines) == 1 else None


# A fork always has a line name: bundle_version's CHECK constraint ties
# `fork_name` and `owner_workspace_id` together, so a fork row cannot carry a
# NULL name. An absent field is therefore the server declining to report it,
# never an unnamed line — which is why neither site below may fall back to
# "default". That is merely the name the backend mints for a workspace's FIRST
# line, so the guess reads as correct everywhere until someone names theirs,
# and then states the wrong line confidently (KEW-2375).
_LINE_UNREPORTED = "not reported by this server"


def _fork(args: argparse.Namespace, client, conversation: str, name: str | None) -> dict:
    """Fork, disclosing and confirming first when the line is being INFERRED.

    A named fork is an explicit choice and goes straight through. A nameless
    one silently adopts whatever single line the workspace owns, wherever that
    has got to — in one prod workspace, 23 minor versions behind product
    (KEW-2362).

    The disclosure is the point, not the gate, so it prints on EVERY path: a
    bare `print` rather than `cli._status`, because `--quiet` suppresses that
    and `-q -y` is precisely the agent invocation this exists for.
    """
    from ..cli import _confirm

    if not name:
        line = _inferred_fork_line(client, conversation)
        if line is not None:
            named = line.get("fork_name")
            label = f" '{named}'" if named else ""
            caveat = "" if named else " This server reported no name for it."
            print(
                f"No --name given: adopting this workspace's existing fork line"
                f"{label} of {line.get('app')}, at {line.get('semver')}.{caveat}",
                file=sys.stderr,
            )
            if not _confirm(args, f"Adopt fork line{label}?"):
                raise PopcornError(
                    "fork cancelled — no line was adopted",
                    error_code="validation",
                    hint=(
                        f"name the line to be sure: --name '{named}'"
                        if named
                        else "the server did not name the line; pass --name "
                        "<line> to say which one you mean"
                    ),
                )
    return operations.fork_channel_app(client, conversation, name)


def _fork_lines(data: dict) -> list[str]:
    """How a fork went, as rendered lines. Shared with `checkout --fork`."""
    status = data.get("status")
    headline = {
        "created": "Forked",
        "already_fork": "Already on this workspace's fork of",
        "adopting": "Adopting this workspace's existing fork of",
    }.get(str(status), f"{status}:")
    line = data.get("fork_name") or _LINE_UNREPORTED
    rendered = [f"{headline} {data.get('app')} {data.get('semver')} (line {line})"]
    if data.get("message"):
        rendered.append(str(data["message"]))
    if status == "adopting":
        rendered.append(
            # `app list` was the old answer and is a bad one: it prints the
            # binding inside a prose line, so callers grepped a semver out of
            # it. `status --channel` reports the same thing as a field.
            "The install is asynchronous — 'popcorn app status --channel "
            "<channel>' says when the channel has moved."
        )
    return rendered


def _app_fork(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    data = _fork(args, client, args.channel, args.name)

    rendered = [*_fork_lines(data), "", "Next: popcorn app checkout --channel <channel>"]
    _output(args, data, "\n".join(rendered))


def _refuse_bump_over_a_hand_edit(version: str, base_semver: str, part: str) -> None:
    """Refuse `--bump` when the manifest already advances past the baseline.

    `--bump` counts from the fork line's head — "mint the next `part` after
    what is published" — which is the only reading that does not depend on
    the state of a file the author may have edited. When the manifest has
    already been advanced by hand there are two answers and no way to tell
    which was meant: counting from the head overwrites the author's number,
    counting from the manifest skips whatever lies between and burns a
    version on the line for nothing.

    So neither happens. Both intentions are one keystroke away — drop the
    flag to publish the hand-written version, or reset the line and re-run —
    and the error names the two versions so the choice is made with them in
    view. Raised before the round trip: nothing here needs the server.
    """
    if parse_semver(version) <= parse_semver(base_semver):
        return
    raise PopcornError(
        f"the manifest already advances to {version} past the checked-out "
        f"{base_semver}, so --bump {part} has two answers "
        f"({next_version(base_semver, part)} from the line's head, "
        f"{next_version(version, part)} from the manifest) — it picks neither",
        error_code="validation",
        hint=f"publish the version you wrote by dropping --bump, or reset "
        f'manifest.yaml to version: "{base_semver}" and re-run with it',
    )


def _deprecated_changelog_used(argv: list[str] | None = None) -> bool:
    """Whether this invocation spelled the message flag `--changelog`.

    Read off argv because the alias shares `--message`'s dest — which is the
    point of an alias, and leaves the parsed namespace with no trace of which
    spelling was typed. An abbreviation argparse also accepts (`--changel`)
    misses this and simply goes unwarned; the flag still works, so the cost
    of the miss is a notice, not a failure.
    """
    words = sys.argv[1:] if argv is None else argv
    return any(w == "--changelog" or w.startswith("--changelog=") for w in words)


def _publish_message(args: argparse.Namespace, files: dict[str, str]) -> str | None:
    """The message to record on this version, having said what it will be.

    Precedence, read off the server rather than assumed: `/apps/publish`
    records the request's `changelog` and nothing else. The fallback to the
    manifest's own `changelog:` key lives on the product publish path
    (`publish_registry_template`) and NOT on the fork path a CLI publish
    takes (`publish_fork_version`), so `--message` does not merely win over
    the manifest field — the manifest field is not read at all, and a
    manifest that declares one while no flag is given records nothing.

    That silence is the whole of KEW-2368's complaint, so it gets a line.
    """
    from ..cli import _status

    message: str | None = args.message
    if _deprecated_changelog_used():
        _status("--changelog is deprecated and now spells --message/-m; it still works.")
    declared = manifest_changelog(files)
    if not message and declared:
        _status(
            "the manifest's 'changelog:' is not recorded by a publish — "
            "pass -m to record a message on this version."
        )
    return message


def _app_publish(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    directory = _directory(args)
    baseline = _require_baseline(directory)
    client = _get_client(args)
    conversation = _channel_of(args, baseline)

    # A product checkout is refused here rather than by the 409, because the
    # fix is a different command and the server's message cannot know that.
    if baseline.kind != "fork":
        raise PopcornError(
            f"{baseline.app} {baseline.semver} is a PRODUCT version — a "
            "publish lands on a fork line this workspace owns",
            error_code="conflict",
            hint="re-run 'popcorn app checkout --channel <channel> --fork' — "
            "one command forks and checks out the line's head",
        )

    local = collect_tree(directory)
    # Refused before the round trip: the server rejects the whole tree over
    # one such path, and its message cannot name the working copy the author
    # is standing in.
    unpublishable = unrecognized_code_paths(local.files)
    if unpublishable:
        raise PopcornError(
            unrecognized_code_note(unpublishable),
            error_code="validation",
            hint=f"move each one under {flow_rules.CODE_SUBDIR}/<block>/, or delete it",
        )
    version = manifest_version(local.files)
    bump = args.bump
    if bump:
        _refuse_bump_over_a_hand_edit(version, baseline.semver, bump)
    message = _publish_message(args, local.files)

    resp = _fetch_base(client, conversation, baseline)
    base_files = files_from_response(resp)
    # The emptiness check runs against the tree AS EDITED, before any bump is
    # applied. Otherwise `--bump patch` on an untouched checkout would write a
    # one-line manifest change and mint a version whose only content is its
    # own number — the wasted version this pair of tickets is about.
    if diff_tree(base_files, local.files).empty:
        raise PopcornError(
            f"nothing to publish — {directory} matches {baseline.app} {baseline.semver}",
            error_code="validation",
        )
    manifest, _ = manifest_file(local.files)
    if bump:
        version = next_version(baseline.semver, bump)
        local.files[manifest] = bump_manifest_text(local.files[manifest], version)
    diff = diff_tree(base_files, local.files)
    require_bump(version, baseline.semver)

    payload = publish_payload(baseline.base_version_id, diff, message)
    result = operations.publish_channel_app(client, conversation, payload)

    # Written only now, after the server accepted it: a publish that fails —
    # a moved line, a rejected tree — leaves the working copy exactly as the
    # author left it, so re-running the same `--bump patch` is the retry
    # rather than a second bump on top of the first.
    if bump:
        (directory / manifest).write_text(local.files[manifest])

    # The working copy now corresponds to the PUBLISHED version — the line's
    # new head — so the baseline moves with it; otherwise the next edit needs
    # a fresh checkout, which is the loop this command exists to close. The
    # channel catches up when the install lands, and that is its business:
    # the next publish is based on the head either way.
    published = Baseline(
        app=str(result.get("app") or baseline.app),
        kind="fork",
        semver=str(result.get("semver") or version),
        base_version_id=int(result.get("version_id") or 0),
        tree_digest=local_digest(local.files),
        fork_name=baseline.fork_name,
        conversation_id=baseline.conversation_id,
        # The note that just shipped, so the NEXT bump is measured against it
        # rather than against whatever the last `app checkout` saw.
        changelog=baseline_changelog(local.files),
    )
    write_baseline(directory, published)

    rendered = [
        f"Published {published.app} {published.semver} "
        f"(version {published.base_version_id})"
        + ("" if result.get("created", True) else " — already existed, no-op"),
        *diff.summary(),
        "",
    ]
    # The only readback there is. No endpoint serves a published version's
    # message — `/apps/list` returns lineage heads without one and there is no
    # per-version endpoint at all — so this line is the single moment the
    # author can see what landed, and it says the absent case out loud rather
    # than leaving a blank to interpret.
    rendered.append(
        f"Message: {message}"
        if message
        else "No message recorded on this version — pass -m next time."
    )
    if local.ignored:
        rendered.append(ignored_note(local.ignored))
    if diff.preserved:
        rendered.append(preserved_note(diff.preserved))
    reach = fork_line_reach(result)
    if reach:
        rendered.append(reach)
    rendered += _install_lines(result)
    # `message` rides into --json for the same reason it is printed: an agent
    # has no other way to learn what text this publish recorded.
    _output(args, {**result, "diff": diff.summary(), "message": message}, "\n".join(rendered))


def _install_lines(result: dict) -> list[str]:
    """How the install onto this channel went, from `install_status`.

    The version is published whatever the status says; every value but
    "started" is about the INSTALL half, and `app apply` is the retry for all
    of them. An API older than popcorn-backend #1985 sends no status, only a
    workflow id when the install started.
    """
    status = str(result.get("install_status") or "")
    workflow_id = result.get("install_workflow_id")
    if status == "started" or (not status and workflow_id):
        # Not "Next:" — nothing further is required of the caller here. The
        # install converges on its own; status is how you CONFIRM it, and
        # presenting it as a mandatory step is what made the loop read as
        # more manual than it is (KEW-2373).
        return [
            f"Installing on this channel: {workflow_id}",
            "It converges on its own — 'popcorn app status' confirms it landed.",
        ]
    if status == "blocked_app_updates_locked":
        return [
            "Not applied to this channel: app updates are locked here — ask a "
            "member to unlock them, then run 'popcorn app apply'",
        ]
    if status == "blocked_install_in_progress":
        return [
            "Not applied yet — another install holds this channel's lock.",
            "Next: popcorn app apply",
        ]
    return ["Not applied to any channel.", "Next: popcorn app apply"]


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


def _channel_status(args: argparse.Namespace, conversation: str) -> None:
    """ "Has my publish landed on this channel?", from server state alone.

    The question the publish loop actually raises, and before this it had no
    direct answer: `status` needed a checkout, so callers polled `app list`
    and grepped a semver out of its prose (KEW-2370). Two version IDs from
    `/apps/tree?ref=head` settle it — `bound_version_id` is what the channel
    runs, `version_id` is the line's head — and `install_state` puts the
    answer in one machine-readable field so nothing has to parse rendering.

    `install_state` is deliberately NOT the install job's status. `publish`
    prints a workflow id (`channel-install:<uuid>`) but the API exposes no
    endpoint that reads it: the whole `/apps` surface is list, tree, file,
    files, fork, publish, apply, and the one place the backend describes that
    workflow is a private helper behind publish and apply. So a channel
    behind its line reads as "pending" whether the install is still running
    or has failed, and this cannot tell the two apart until the backend
    exposes the job. `apply` is the retry either way, which is why "pending"
    points there.
    """
    from ..cli import _get_client, _output

    client = _get_client(args)
    listing = operations.list_channel_apps(client, conversation)
    binding = listing.get("channel")
    if not binding:
        raise PopcornError(
            f"{conversation} does not run an app bundle — there is no install to report",
            error_code="not_found",
            hint=f"popcorn app list --channel '{conversation}'",
        )

    tree = operations.get_channel_app_tree(client, conversation, ref="head")
    head_id = tree.get("version_id")
    head_semver = tree.get("semver")
    # An API older than popcorn-backend #1985 sends no `bound_*`; then the
    # served version IS the bound one, so the binding's own fields answer.
    channel_id = tree.get("bound_version_id", binding.get("version_id"))
    channel_semver = tree.get("bound_semver", binding.get("semver"))
    behind = channel_id != head_id
    line = binding.get("fork_name")

    data = {
        "channel": conversation,
        "app": binding.get("app"),
        "kind": binding.get("kind"),
        "fork_name": line,
        "channel_semver": channel_semver,
        "channel_version_id": channel_id,
        "head_semver": head_semver,
        "head_version_id": head_id,
        "channel_behind": behind,
        "install_state": "pending" if behind else "current",
    }

    lines = [
        f"{binding.get('app')} {channel_semver} ({binding.get('kind')}"
        + (f", line {line}" if line else "")
        + f") on {conversation}",
    ]
    if behind:
        lines += [
            f"Fork line head: {head_semver} (version {head_id})",
            "",
            f"Install: PENDING — the channel still runs {channel_semver} (version {channel_id}).",
            "The install job's own status is not readable from the API, so a "
            "failed install looks the same as one still running.",
            f"Re-run this to re-check, or 'popcorn app apply --channel "
            f"{conversation}' to retry it.",
        ]
    else:
        lines += [
            f"Fork line head: {head_semver} (version {head_id})",
            "",
            f"Install: CURRENT — the channel runs the line's head (version {head_id}).",
        ]
    _output(args, data, "\n".join(lines))


def _app_status(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    directory = _directory(args)
    # `--channel` outside a checkout is the channel-scoped read; inside one it
    # keeps its older meaning — the channel to compare the working copy
    # against, which a v1 baseline (0.19.0) cannot supply on its own. Branching
    # on the baseline rather than on the flag is what keeps that intact.
    baseline = read_baseline(directory)
    if baseline is None:
        if getattr(args, "channel", None):
            _channel_status(args, str(args.channel))
            return
        raise PopcornError(
            f"no {BASELINE_FILE} in {directory} — this is not an app checkout",
            error_code="not_found",
            hint="pass --channel '#your-channel' to report that channel's "
            "install without a checkout",
        )
    client = _get_client(args)
    conversation = _channel_of(args, baseline)

    local = collect_tree(directory)
    unpublishable = unrecognized_code_paths(local.files)
    resp = operations.get_channel_app_files(client, conversation, ref="head")
    base_files = files_from_response(resp)
    # Diffed against the line's HEAD, not the baseline's digest: status is
    # the command you run when the two disagree, so it must not refuse the
    # way publish does.
    diff = diff_tree(base_files, local.files)
    head_id = resp.get("version_id")
    head_semver = resp.get("semver")
    # The channel's own version rides along (popcorn-backend #1985); an older
    # API sends only the served one, and then the two are the same.
    channel_id = resp.get("bound_version_id", head_id)
    channel_semver = resp.get("bound_semver", head_semver)
    in_sync = head_id == baseline.base_version_id

    data = {
        "directory": str(directory),
        "app": baseline.app,
        "kind": baseline.kind,
        "baseline_semver": baseline.semver,
        "baseline_version_id": baseline.base_version_id,
        "head_semver": head_semver,
        "head_version_id": head_id,
        "channel_semver": channel_semver,
        "channel_version_id": channel_id,
        "in_sync": in_sync,
        "channel_behind": channel_id != head_id,
        "dirty": local_digest(local.files) != baseline.tree_digest,
        "added": diff.added,
        "changed": diff.changed,
        "deleted": diff.deletes,
        "ignored": local.ignored,
        "preserved": diff.preserved,
        "unpublishable": unpublishable,
    }

    lines = [
        f"{baseline.app} {baseline.semver} ({baseline.kind}) in {directory}",
    ]
    if not in_sync:
        lines.append(
            f"Fork line moved to {head_semver} (version {head_id}) — re-run 'popcorn app checkout'."
        )
    elif channel_id != head_id:
        lines.append(
            f"Channel still runs {channel_semver} (version {channel_id}); "
            f"{baseline.semver} is the line's head and its install has not "
            "landed — 'popcorn app apply' retries it."
        )
    else:
        lines.append(f"Channel runs the same version ({head_id}).")
    lines.append("")
    if diff.empty:
        lines.append("Working copy matches the fork line's head.")
    else:
        lines.append("Uncommitted edits:")
        lines += diff.summary()
    if local.ignored:
        lines.append("")
        lines.append(ignored_note(local.ignored))
    if unpublishable:
        lines.append("")
        lines.append(unrecognized_code_note(unpublishable))
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
                "lines",
                "List this workspace's fork lines — name, head, published_at",
                _app_lines,
                [
                    Argument(
                        "channel",
                        "Any channel you can reach — the API authorizes this "
                        "read against a conversation; the lines listed are "
                        "the workspace's, not the channel's",
                        required=True,
                    ),
                    Argument("app", "Only this app's lines (default: every app)"),
                ],
            ),
            Subcommand(
                "checkout",
                "Write the fork line's head to disk, with a baseline",
                _app_checkout,
                [
                    _CHANNEL,
                    Argument(
                        "directory",
                        "Target directory (default: ./<app>)",
                        positional=True,
                        nargs="?",
                        flag_alias="--dir",
                    ),
                    Argument(
                        "fork",
                        "Fork first, then check out the line's head. Takes an "
                        "optional line name; bare, it confirms the line it "
                        "infers. It cannot be told apart from the directory "
                        "positional, so '--fork mydir' names the LINE 'mydir' "
                        "— write '--fork=<line>', spell the directory "
                        "'--dir <path>', or put the directory ahead of a "
                        "bare --fork",
                        nargs="?",
                        const="",
                    ),
                    Argument(
                        "force",
                        "Overwrite bundle files in a non-empty directory "
                        "without asking (--yes does not cover this)",
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
                    Argument(
                        "message",
                        "What changed, recorded on the version (-m, like git "
                        "commit). --changelog is a deprecated alias",
                        flags=["-m", "--changelog"],
                    ),
                    Argument(
                        "bump",
                        "Mint the next version off the fork line's head, "
                        "writing manifest.yaml's 'version:' on a successful "
                        "publish. Refused when the manifest already advances "
                        "past the head",
                        choices=list(BUMP_PARTS),
                    ),
                    _CHANNEL_OPT,
                ],
            ),
            Subcommand(
                "apply",
                "Recovery only: retry an install that did not land. 'publish' "
                "starts one and it normally converges on its own",
                _app_apply,
                [_DIRECTORY, _CHANNEL_OPT],
            ),
            Subcommand(
                "status",
                "Has the publish landed? With a checkout, also what differs "
                "from the fork line's head",
                _app_status,
                [
                    _DIRECTORY,
                    Argument(
                        "channel",
                        "Channel to act on (default: the checkout's baseline). "
                        "Outside a checkout this reports that channel's bound "
                        "version, its line's head and the install state",
                    ),
                ],
            ),
        ],
    )
)
