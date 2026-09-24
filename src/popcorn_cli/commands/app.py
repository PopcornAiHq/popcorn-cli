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
publish is a line operation and must be based on the head; the two differ
only while the channel lags its line — a head whose
install has not landed, or failed — and that is the case where a checkout of
the bound tree used to leave the line stuck. `checkout` says so when it
happens; `status` shows both versions.

`checkout --version N` is the one way to read anything else: a past version
of the line, to recover the tree from before a bad publish or to diff two
versions. Its baseline is marked `historical` unless N is the head, and
`publish` refuses a historical checkout rather than basing a publish on it.

Two groups of commands, split by what they act on:

- `fork` acts on a CHANNEL, so it takes `--channel`. `list` and `lines`
  report the WORKSPACE's apps and fork lines, which the API serves without a
  channel; `--channel` on `list` adds what that channel runs, and `lines`
  needs none.
- `checkout`, `publish`, `apply` and `status` act on a checkout DIRECTORY and
  read the channel out of its baseline. `--channel` stays accepted there for
  baselines written by 0.19.0, which predate the field.

`status` is the one command in both groups: with a checkout it compares the
working copy against the line and the channel, and with `--channel` outside
one it answers "has my publish landed here?" from server state alone.

Either way it also checks the channel's live schedules against the ones its
bound manifest declares. Most differences there are deliberate —
`set_app_mode` retunes cadences off prod, and a plain daily cron is moved off
its declared minute by the de-peak offset — so `schedule_drift` classifies
each one and only an unexplained difference, or a schedule paused with nothing
saying why, makes the command exit non-zero.

Handlers import `..cli` helpers inside the function body: cli.py imports this
package at module load to build the parser, so a module-level import cycles.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from popcorn_core import flow_rules, operations, schedule_drift

# `baseline_changelog` is app_checkout's `manifest_changelog`, aliased because
# app_publish exports a same-named function doing a different job. This one
# NORMALISES the note, so reflowing a block scalar is not read as an edit, and
# it is what the baseline records for `template check` to compare against.
# app_publish's returns the raw working-copy value, only ever to warn about it.
from popcorn_core.app_checkout import (
    BASELINE_FILE,
    GUIDE_TEXT,
    Baseline,
    baseline_from_response,
    files_from_response,
    guide_text,
    occupied,
    read_baseline,
    write_agent_guide,
    write_baseline,
    write_tree,
)
from popcorn_core.app_checkout import (
    manifest_changelog as baseline_changelog,
)
from popcorn_core.app_publish import (
    BUMP_PARTS,
    bump_manifest_text,
    classify_tree,
    collect_tree,
    diff_tree_hashes,
    file_sha256,
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
    served_hashes,
    unrecognized_code_note,
    unrecognized_code_paths,
)
from popcorn_core.errors import APIError, PopcornError
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


def _render_list(data: dict, named_channel: bool = True) -> str:
    """`named_channel` separates "this channel runs nothing" from "no channel
    was asked about" — the response's `channel` is null for both."""
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
    elif named_channel:
        lines.append("This channel does not run an app bundle.")
    else:
        lines.append("Pass --channel to also see what one channel runs.")
    return "\n".join(lines)


def _app_list(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    channel = getattr(args, "channel", None)
    data = operations.list_channel_apps(client, channel)
    _output(args, data, _render_list(data, named_channel=bool(channel)))


# How many channels ride each line is the safety information `app lines`
# exists to give, and the API does not carry it: `/apps/list` returns lineage
# heads only, and the count the server keeps for itself lives behind
# `publish` and is exposed nowhere. The CLI could approximate it by listing channels and reading each
# one's binding, and deliberately does not: that enumerates only the channels
# the CALLER can see, so it under-counts exactly when the answer matters and
# would report "no channels" for a line another member's channel is bound to.
# An undercount presented as a safety check is worse than an honest gap, so
# the command names the gap instead, and closing it needs a server-side count.
_NO_CHANNEL_COUNT = (
    "How many channels ride each line is not shown: the API reports lineage "
    "heads only, with no per-line channel count."
)

# Deleting a fork line has no API behind it either — the whole `/apps`
# surface is list, tree, file, files, fork, publish and apply — so `app lines
# delete` is not implemented rather than shipped as a command that dead-ends.
_NO_DELETE = (
    "Deleting a fork line is not possible yet: the API has no endpoint for "
    "it. Lines accumulate until it does."
)


def _render_lines(lines_data: list[dict]) -> str:
    from ..formatting import format_timestamp

    if not lines_data:
        return (
            "This workspace owns no fork lines.\n"
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
    in one workspace is a routine afternoon and nothing listed them on their
    own.

    No channel is sent: the inventory is workspace-scoped and the API serves
    it without one. `--channel` is still accepted so scripts written when the
    API required it keep working, and it changes nothing that is listed.
    """
    from ..cli import _get_client, _output

    client = _get_client(args)
    data = operations.list_channel_apps(client)
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
        # Echoed as passed (null when omitted): `--json` keys are add-only,
        # even for an argument that no longer scopes anything.
        "channel": getattr(args, "channel", None),
        "lines": lines_data,
        # Stated on the wire too, so a script reading --json is told the count
        # is absent rather than inferring zero from a missing key.
        "channel_counts_available": False,
        "delete_supported": False,
    }
    _output(args, payload, _render_lines(lines_data))


# Where a version id can be seen, since nothing lists a line's past versions:
# the API serves lineage heads and single versions, never a history. Shared by
# the 404 hint and the publish refusal's wording so the two cannot disagree.
_WHERE_VERSION_IDS_ARE = (
    "'app publish' prints each version's id as it publishes it, 'app status' "
    f"shows the line's head and what the channel runs, and a checkout's "
    f"{BASELINE_FILE} records its base_version_id; no command lists a line's "
    "past versions"
)


def _version_id(value: str) -> int:
    """argparse type for `--version`: a version id, never a semver.

    Plain `int` answers `--version 0.1.0` with "invalid int value", which
    says nothing about why a version number is the wrong kind of version.
    """
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"takes a version id — a whole number as 'app publish' and 'app "
            f"status' print it, e.g. 'version 12' — not a semver like {value!r}"
        ) from None


# `commands --json` reports an argument's type by its converter's name, and
# the value this produces is an int.
_version_id.__name__ = "int"


def _read_version(client, conv_id: str, version_id: int) -> dict:
    """The files of one named version, with the server's refusal made usable.

    The 404 is deliberately one answer for every unreadable id — nonexistent,
    another workspace's, another line's, a product version the channel is not
    offered — so the hint says where valid ids come from and nothing about
    which of those this was. Matched on the message so that a DIFFERENT 404
    (a channel that runs no app) keeps its own text and gains no hint that
    would misdirect.
    """
    try:
        return operations.get_channel_app_files(client, conv_id, version_id=version_id)
    except APIError as exc:
        if exc.status_code == 404 and "is not a version of" in str(exc):
            exc.hint = (
                "a version id is readable only from this channel's own line — "
                + _WHERE_VERSION_IDS_ARE
            )
        raise


def _app_checkout(args: argparse.Namespace) -> None:
    from ..cli import _confirm_force, _get_client, _output

    version_id = getattr(args, "version", None)
    # Checked before any request: the server's own 422 for this is correct but
    # names a query parameter the caller never typed.
    if version_id is not None and version_id < 1:
        raise PopcornError(
            f"--version takes a version id, a whole number from 1 (got {version_id})",
            error_code="validation",
            hint=_WHERE_VERSION_IDS_ARE,
        )

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
    # argparse keeps `--fork` and `--version` apart, so this never runs for a
    # version checkout.
    forked = None
    if getattr(args, "fork", None) is not None:
        forked = _fork(args, client, conv_id, args.fork or None)

    head: dict | None = None
    if version_id is None:
        resp = operations.get_channel_app_files(client, conv_id)
    else:
        resp = _read_version(client, conv_id, version_id)
        # Whether the named version is the line's head decides what the
        # baseline may be used for. The head needs no flag at all — it is
        # exactly what a plain checkout would have written, and making it
        # unpublishable would be a refusal with no reason behind it.
        #
        # Read AFTER the files: a publish landing between the two reads then
        # makes the copy historical — the safe direction — where the other
        # order could mark a superseded version publishable.
        #
        # Fork lines only. A product-bound channel is offered the version it
        # runs and the workspace's release-track head, which is NEWER than
        # what it runs, so "past version" would be simply wrong there; and a
        # product checkout needs no flag, since publish refuses it already.
        if resp.get("kind") == "fork":
            head = operations.get_channel_app_tree(client, conv_id, ref="head")
    files = files_from_response(resp)
    if not files:
        raise PopcornError(
            f"{resp.get('app') or 'this channel'} returned no files to check out",
            error_code="not_found",
        )
    historical = head is not None and head.get("version_id") != resp.get("version_id")

    # Default to ./<app> rather than '.', so a bare checkout in a working
    # directory cannot scatter bundle files over whatever is already there.
    # A version checkout defaults to ./<app>-<semver> instead: reading an old
    # version beside the working copy, or two versions side by side to diff
    # them, is what it is for, and a shared default would collide every time.
    app_name = resp.get("app") or "app"
    if args.directory:
        directory = Path(args.directory)
    elif version_id is not None:
        directory = Path(f"{app_name}-{resp.get('semver') or version_id}")
    else:
        directory = Path(app_name)

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

    # Read before the new baseline overwrites it: the guide this directory
    # would have been given last time is how an unedited one is recognised.
    previous = read_baseline(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = write_tree(directory, files)
    baseline = baseline_from_response(resp, files, conversation_id=conv_id, historical=historical)
    write_baseline(directory, baseline)
    # --force is already the author saying "take checkout's version of this
    # directory"; without it an existing guide is theirs to keep, unless it is
    # still checkout's own text for a different kind of checkout.
    guide = write_agent_guide(
        directory,
        force=bool(getattr(args, "force", False)),
        text=guide_text(baseline),
        replaceable=(GUIDE_TEXT,) + ((guide_text(previous),) if previous else ()),
    )

    # Checkout writes the served files and deletes nothing, so a re-checkout
    # over an older tree keeps whatever that tree had and this one lacks — and
    # those paths publish. Listed rather than removed: the baseline records a
    # digest, not a file list, so nothing can tell a file the last checkout
    # wrote from one the author added, and deleting the author's is the loss
    # `_confirm_force` exists to prevent.
    stale = sorted(rel for rel, _ in classify_tree(directory)[0] if rel not in files)
    # What the channel runs, alongside what was served. An older API sends
    # neither field; then the served version IS the bound one and there is
    # nothing to note.
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
        # Named rather than folded into `files`: it is not bundle content and
        # publish will not send it. None when an existing one was kept.
        "guide": guide.name if guide else None,
        # Bundle paths on disk that the served tree lacks; see above.
        "stale_files": stale,
    }
    if head is not None:
        data["historical"] = historical
        data["head_version_id"] = head.get("version_id")
        data["head_semver"] = head.get("semver")
    if forked is not None:
        data["fork"] = forked
    lines = [
        *(_fork_lines(forked) if forked is not None else []),
        f"Checked out {baseline.app} {baseline.semver} ({baseline.kind}) into {directory}",
    ]
    if historical:
        assert head is not None
        lines.append(
            f"This is version {baseline.base_version_id}, not the line's head "
            f"({head.get('semver')}, version {head.get('version_id')}); the channel "
            f"runs {channel_semver} (version {channel_id}). It is a copy to read "
            "and diff — 'app publish' refuses it, and says how to republish its "
            "content on top of the head."
        )
    elif version_id is not None and baseline.kind != "fork":
        lines.append(
            f"This is a {baseline.kind} version (version {baseline.base_version_id}); "
            f"the channel runs {channel_semver} (version {channel_id}). A publish "
            "needs a fork line — 'popcorn app checkout --fork' makes one."
        )
    elif channel_id != baseline.base_version_id:
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
    ]
    if stale:
        lines += [
            "",
            f"Left in place, and NOT part of {baseline.app} {baseline.semver}: " + ", ".join(stale),
            "checkout never deletes a file; these publish from here unless you delete them.",
        ]
    if guide is not None:
        lines.append(
            f"Also wrote {guide.name} — "
            + (
                "what this snapshot is and how to republish its content, "
                if historical
                else "how to edit and publish this directory, "
            )
            + "for whoever reads it next. It is not bundle content and does not publish."
        )
    if not historical:
        lines += ["", f"Next: popcorn template check {directory}"]
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
            # No "run:" prefix — the renderer supplies the verb.
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


def _read_head(client, conversation: str) -> tuple[dict, dict[str, str]]:
    """The fork line's head: its version fields, and `{path: sha256}` of its tree.

    Hashes are all a diff needs from the base side (see `diff_tree_hashes`),
    so this reads `/apps/tree`, which serves them, and never the content.
    When `/apps/tree` has no usable hashes — a server that predates them, or
    a malformed map — the full-tree read runs instead, and its response then
    supplies the version fields as well —
    the tree read is discarded rather than mixed with it, so the version and
    the hashes always come from one response.
    """
    tree = operations.get_channel_app_tree(client, conversation, ref="head")
    hashes = served_hashes(tree)
    if hashes is not None:
        return tree, hashes
    resp = operations.get_channel_app_files(client, conversation, ref="head")
    return resp, {path: file_sha256(text) for path, text in files_from_response(resp).items()}


def _fetch_base(client, conversation: str, baseline: Baseline) -> tuple[dict, dict[str, str]]:
    """The fork line's head, refusing when it is not the version we edited.

    Returns the version fields and `{path: sha256}`; see `_read_head` for
    when those come from the full-tree read instead of `/apps/tree`.

    The diff is computed against this tree, so it must be the one the
    checkout came from — and the head is what a publish must be based on.
    What the CHANNEL runs plays no part: a channel
    still behind its line (a head whose install has not landed, or failed)
    publishes fine from a checkout of that head. A head past the baseline
    means someone else published on the line; the server would refuse the
    stale base, and the answer is the same here: check out again.
    """
    resp, hashes = _read_head(client, conversation)
    head_id = resp.get("version_id")
    if head_id == baseline.base_version_id:
        return resp, hashes
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


# A fork always has a line name — the server will not store a fork version
# without one. An absent field is therefore the server declining to report it,
# never an unnamed line, which is why neither site below may fall back to
# "default". That is merely the name minted for a workspace's FIRST line, so
# the guess reads as correct everywhere until someone names theirs, and then
# states the wrong line confidently.
_LINE_UNREPORTED = "not reported by this server"


def _fork(args: argparse.Namespace, client, conversation: str, name: str | None) -> dict:
    """Fork, disclosing and confirming first when the line is being INFERRED.

    A named fork is an explicit choice and goes straight through. A nameless
    one silently adopts whatever single line the workspace owns, wherever that
    has got to — in one production workspace, 23 minor versions behind
    product.

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
    manifest's own `changelog:` key lives on the product publish path and NOT
    on the fork path a CLI publish takes, so `--message` does not merely win
    over the manifest field — the manifest field is not read at all, and a
    manifest that declares one while no flag is given records nothing.

    Recording nothing without saying so is the failure this exists to stop,
    so it gets a line.
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


def _refuse_historical_publish(baseline: Baseline, directory: Path) -> None:
    """Refuse a publish from a `checkout --version` of a past version.

    A publish is based on the line's head, and the server refuses any other
    base, so this could only ever fail — but by the round trip it would fail
    as "the fork line moved", which reads as someone else's publish and sends
    the author to re-check-out, discarding exactly the old tree they came for.

    Not offered as a one-step "revert": publishing an old tree over the head
    silently undoes everything published since, and that deserves a working
    copy of the head in front of the author when it happens, where `app
    status` and the diff summary show what is being undone.
    """
    channel = baseline.conversation_id or "<channel>"
    raise PopcornError(
        f"{directory} is a checkout of {baseline.app} {baseline.semver} "
        f"(version {baseline.base_version_id}), a past version rather than its "
        "line's head — a publish is based on the head, so it cannot start here",
        error_code="conflict",
        hint="to republish this content: 'popcorn app checkout --channel "
        f"{channel} --dir <new-dir>' for the head, copy these bundle files over it (leave its "
        f"{BASELINE_FILE}) and delete any file there this version lacks — "
        "or the publish keeps everything added since — then 'popcorn app publish <new-dir> --bump patch -m "
        '"..."\' — the diff it prints is what reverts',
    )


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
    if baseline.historical:
        _refuse_historical_publish(baseline, directory)

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

    _, base_hashes = _fetch_base(client, conversation, baseline)
    # The emptiness check runs against the tree AS EDITED, before any bump is
    # applied. Otherwise `--bump patch` on an untouched checkout would write a
    # one-line manifest change and mint a version whose only content is its
    # own number — the wasted version this pair of tickets is about.
    if diff_tree_hashes(base_hashes, local.files).empty:
        raise PopcornError(
            f"nothing to publish — {directory} matches {baseline.app} {baseline.semver}",
            error_code="validation",
        )
    manifest, _ = manifest_file(local.files)
    if bump:
        version = next_version(baseline.semver, bump)
        local.files[manifest] = bump_manifest_text(local.files[manifest], version)
    diff = diff_tree_hashes(base_hashes, local.files)
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
    of them. An older API sends no status, only a workflow id when the
    install started.
    """
    status = str(result.get("install_status") or "")
    workflow_id = result.get("install_workflow_id")
    if status == "started" or (not status and workflow_id):
        # Not "Next:" — nothing further is required of the caller here. The
        # install converges on its own; status is how you CONFIRM it, and
        # presenting it as a mandatory step is what made the loop read as
        # more manual than it is.
        return [
            f"Installing on this channel: {workflow_id}",
            "It converges on its own — 'popcorn app status' confirms it landed.",
        ]
    if status == "blocked_app_updates_locked":
        return [
            "Not applied to this channel: app updates are locked here — ask a "
            "channel admin or a workspace admin to unlock them, then run "
            "'popcorn app apply'",
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


def _collect_schedule_drift(
    client: Any, conversation: str
) -> tuple[schedule_drift.DriftReport | None, str | None]:
    """Gather both sides of the comparison, or say why it could not be made.

    Compared against the BOUND manifest in both of `status`'s modes, including
    from inside a checkout where a local manifest is also to hand. The live
    schedules were installed from the version the channel runs, so that is the
    only manifest they can be judged against — a working copy's `schedules:`
    describes a channel state that does not exist yet, and a head that has not
    landed describes one that may never.

    Returns `(None, reason)` rather than raising: a channel with no manifest,
    or a Temporal outage behind the schedule list, must not take down the
    version reporting that is this command's main job and works fine without
    it. The reason is rendered where a reader will see it.
    """
    try:
        resp = operations.get_channel_app_file(client, conversation, "manifest.yaml")
    except APIError as exc:
        return None, f"the bound version has no readable manifest.yaml ({exc})"

    try:
        manifest = yaml.safe_load(resp.get("content") or "") or {}
    except yaml.YAMLError as exc:
        return None, f"the bound manifest.yaml does not parse ({exc})"
    if not isinstance(manifest, dict):
        return None, "the bound manifest.yaml is not a YAML mapping"

    declared = manifest.get("schedules")
    if not isinstance(declared, list):
        # No `schedules:` key at all is the ordinary case for most bundles,
        # and it is not a failure to report.
        return schedule_drift.DriftReport(), None

    try:
        live_resp = operations.list_scheduled_flows(client, conversation)
    except APIError as exc:
        return None, f"the channel's live schedules could not be read ({exc})"
    live = live_resp.get("scheduled_flows") or []

    # `popcorn.app_mode` separates a deliberate retune from an unexplained one,
    # and a channel that never set it reads as None — which `classify` treats
    # as "could not confirm" rather than as prod.
    app_mode: str | None = None
    try:
        scalar = operations.get_scalar(client, conversation, "popcorn.app_mode")
        value = (scalar.get("scalar") or {}).get("value")
        app_mode = str(value) if value is not None else None
    except APIError:
        pass

    entries = [e for e in declared if isinstance(e, dict)]
    return schedule_drift.classify(entries, live, app_mode), None


def _render_schedule_drift(
    report: schedule_drift.DriftReport | None, error: str | None
) -> list[str]:
    """The `Schedules:` block, and nothing when a bundle declares none."""
    if error is not None:
        return ["", f"Schedules: not checked — {error}"]
    assert report is not None
    if not report.findings:
        return []

    lines = ["", "Schedules:"]
    for finding in report.findings:
        if finding.drift_class is None:
            lines.append(f"  OK    {finding.slug} — {finding.summary}")
            continue
        label = "DRIFT" if finding.alarming else "note "
        lines.append(f"  {label} {finding.slug} — {finding.summary}")
        if finding.declared or finding.live:
            lines.append(
                f"        declared {finding.declared or '(none)'}; "
                f"live {finding.live or '(not installed)'}"
            )
    return lines


def _schedule_drift_section(
    client: Any, conversation: str, data: dict[str, Any], lines: list[str]
) -> str | None:
    """Fold the drift check into a status report's data and rendering.

    Returns the message the caller must raise AFTER emitting output, so a
    drifted channel still prints its report rather than only an error.
    """
    report, error = _collect_schedule_drift(client, conversation)
    data["schedule_drift"] = report.to_dict() if report is not None else None
    data["schedule_drift_error"] = error
    lines += _render_schedule_drift(report, error)
    if report is None or not report.alarming:
        return None
    slugs = ", ".join(f.slug for f in report.alarming)
    return f"{len(report.alarming)} schedule(s) drifted from the manifest: {slugs}"


def _channel_status(args: argparse.Namespace, conversation: str) -> None:
    """ "Has my publish landed on this channel?", from server state alone.

    The question the publish loop actually raises, and before this it had no
    direct answer: `status` needed a checkout, so callers polled `app list`
    and grepped a semver out of its prose. Two version IDs from
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
    # An older API sends no `bound_*`; then the served version IS the bound
    # one, so the binding's own fields answer.
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
    drift = _schedule_drift_section(client, conversation, data, lines)
    _output(args, data, "\n".join(lines))
    if drift:
        raise PopcornError(drift, error_code="validation")


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
    resp, base_hashes = _read_head(client, conversation)
    # Diffed against the line's HEAD, not the baseline's digest: status is
    # the command you run when the two disagree, so it must not refuse the
    # way publish does.
    diff = diff_tree_hashes(base_hashes, local.files)
    head_id = resp.get("version_id")
    head_semver = resp.get("semver")
    # The channel's own version rides along; an older API sends only the
    # served one, and then the two are the same.
    channel_id = resp.get("bound_version_id", head_id)
    channel_semver = resp.get("bound_semver", head_semver)
    in_sync = head_id == baseline.base_version_id and not baseline.historical

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
        "historical": baseline.historical,
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
    if baseline.historical:
        # Not "the line moved": nobody moved it, this copy was asked for as a
        # past version, and re-checking out would throw away what it is for.
        lines.append(
            f"A past version (checked out with --version); the line's head is "
            f"{head_semver} (version {head_id}). 'app publish' refuses this copy."
        )
    elif not in_sync:
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
        # A past version differs from the head before anyone edits it, so
        # calling the difference "edits" would claim work nobody did.
        lines.append(
            "Differs from the fork line's head:" if baseline.historical else "Uncommitted edits:"
        )
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
    drift = _schedule_drift_section(client, conversation, data, lines)
    _output(args, data, "\n".join(lines))
    if drift:
        raise PopcornError(drift, error_code="validation")


register(
    Command(
        name="app",
        category="flows",
        description="App bundles — fork, check out, edit and publish one",
        subcommands=[
            Subcommand(
                "list",
                "Show each app's product and fork lines, and with --channel what that channel runs",
                _app_list,
                [Argument("channel", "Also report what this channel runs (name or UUID)")],
            ),
            Subcommand(
                "lines",
                "List this workspace's fork lines — name, head, published_at",
                _app_lines,
                [
                    Argument(
                        "channel",
                        "Not needed, and ignored: the lines listed are the "
                        "workspace's. Accepted so older scripts keep working",
                    ),
                    Argument("app", "Only this app's lines (default: every app)"),
                ],
            ),
            Subcommand(
                "checkout",
                "Write the fork line's head (or, with --version, one past "
                "version) to disk, with a baseline",
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
                        exclusive_group="checkout_source",
                    ),
                    Argument(
                        "version",
                        "Check out this version id of the channel's own line "
                        "instead of its head, into ./<app>-<semver> by default. "
                        "A past version is for reading and diffing; 'app "
                        "publish' refuses it. Ids appear in 'app publish' and "
                        "'app status' output",
                        type=_version_id,
                        exclusive_group="checkout_source",
                    ),
                    Argument(
                        "force",
                        "Overwrite bundle files in a non-empty directory "
                        "without asking (--yes does not cover this). Nothing "
                        "is deleted: files the new tree lacks stay, and are "
                        "listed",
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
