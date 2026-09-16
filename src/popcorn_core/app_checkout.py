"""Materialize an app bundle onto disk, and the baseline that tracks it.

`popcorn app checkout` writes the fork line's head as files plus a
`.popcorn-app.json` baseline. The baseline is what `app publish` diffs
against: it names the version the working copy came from, so a publish can be
refused when the line has moved underneath it, and it names the channel so
`publish`/`apply`/`status` need no `--channel`.

The baseline lives INSIDE the checkout directory but is not bundle content.
It is a dotfile so `template check`'s globs skip it, and publish must exclude
dotfiles for the same reason.

`CLAUDE.md` is written beside it for the same "inside the directory, not part
of the bundle" reason, and for one the baseline cannot serve: a coding agent
asked to reword an email in this directory has nothing in the REQUEST naming
Popcorn, so no skill of ours can trigger on it, and it edits a file and ships
nothing. A `CLAUDE.md` in the directory loads when the agent reads a file in
the directory — the trigger and the failure window become the same event
(KEW-2380).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import flow_rules
from .errors import PopcornError

BASELINE_FILE = ".popcorn-app.json"
# Not a dotfile, unlike the baseline: Claude Code loads a subdirectory's
# CLAUDE.md by that exact name, so the name is the mechanism.
GUIDE_FILE = "CLAUDE.md"
# 2 added `conversation_id`. 3 added `changelog`. An older baseline still
# parses — every field is read with a default — and each command degrades to
# what it can still answer rather than rewriting the file underneath the user:
# a v1 falls back to an explicit --channel, and a v1/v2 simply has no recorded
# changelog for `template check` to compare against.
_VERSION = 3
# The first baseline version that captured the checked-out manifest's
# `changelog:`. Below it, absence of the field means "not recorded", which is
# not the same answer as "the manifest had none".
CHANGELOG_VERSION = 3

_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

# Deliberately not templated with the app or channel — the baseline next to it
# carries those and stays correct as the line moves, while a name baked in
# here would be a second copy to go stale. What belongs here is only what is
# true of every checkout and unreachable from the wording of a request: that
# this directory publishes, and the one authoring rule that fails silently.
GUIDE_TEXT = f"""# Popcorn app bundle — working copy

This directory is a **checked-out Popcorn app bundle**, not a folder of YAML.
`{BASELINE_FILE}` beside this file names the app, the fork line, and the
version this copy came from.

**Editing a file here ships nothing on its own.** The loop is:

```
edit  →  popcorn template check .  →  popcorn app publish . -m "<what changed>"
```

`app publish` refuses a manifest whose `version:` has not advanced past the
checked-out one; `--bump patch` (or `minor`/`major`) writes that bump for you
on a successful publish. Until a publish lands, the channel still runs the old
version — a clean `template check` is a check, not a release.

## Posting markdown needs `format: markdown`

`foundation.channel.post` defaults to `format: plain`, so a step whose `text:`
carries markdown posts its asterisks and hashes literally:

```yaml
- id: announce
  activity: foundation.channel.post
  args:
    channel_id: $inputs.conversation_id
    text: "**Deploy complete** — 3 services updated"
    format: markdown          # without this the ** ships as literal asterisks
```

Nothing in the authoring loop catches the omission: `template check` passes,
the flow runs green, and the message is wrong only on screen. Read
`part.format` back with `popcorn message list` after a test run.

`foundation.channel.edit` and `foundation.channel.post_file` take **no
`format` argument at all** and always emit plain, so refreshing a markdown
post with `channel.edit` silently drops the rendering.

## What this file is

`popcorn app checkout` wrote it, and `popcorn app publish` does not upload it.
It is local guidance for whoever edits this working copy, so deleting it
changes nothing about the app — the next checkout writes it again, and a
re-checkout leaves your edits to it alone unless you pass `--force`.

`AGENT.md` is the opposite and is not interchangeable with this: it is bundle
content, it publishes, and it travels to every channel that installs the app.
Instructions for the app's own users go there; instructions about editing and
shipping this directory go here.
"""


def semver_key(version: str) -> tuple[int, int, int] | None:
    """`"2.4.0"` → `(2, 4, 0)`; None for anything not MAJOR.MINOR.PATCH.

    Tuple ordering is the point: `1.10.0` must sort above `1.9.0`, which
    string comparison gets backwards.
    """
    match = _SEMVER_RE.match(version.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def parse_semver(version: str) -> tuple[int, int, int]:
    """`semver_key`, raising instead of returning None.

    Strict for a reason the YAML makes non-obvious: an unquoted
    `version: 1.0` parses as the float 1.0 and `version: 1.0.0` as a string,
    so the two look identical in the file. Rejecting the float here is what
    stops `"1.0"` reaching the registry.
    """
    key = semver_key(version)
    if key is None:
        raise PopcornError(
            f"{version!r} is not MAJOR.MINOR.PATCH",
            error_code="validation",
        )
    return key


def changelog_of(manifest: dict[str, Any]) -> str | None:
    """A manifest's `changelog:` as a comparable string, or None when absent.

    Read from the PARSED document rather than the file's bytes so that
    re-indenting a block scalar, or switching `>-` for `|`, is not mistaken
    for a rewrite. None and `""` both mean "no note"; the distinction that
    matters is recorded-vs-not, which `Baseline.changelog_recorded` carries.
    """
    value = manifest.get("changelog")
    if not isinstance(value, str):
        return None
    return value.strip() or None


def manifest_changelog(files: dict[str, str]) -> str | None:
    """`changelog_of` the manifest inside a `{path: content}` bundle tree.

    Lenient everywhere `manifest_version` is strict: this feeds a warning, so
    an unparseable or absent manifest is simply nothing to record.
    """
    import yaml

    for name in flow_rules.MANIFEST_FILENAMES:
        if name not in files:
            continue
        try:
            doc = yaml.safe_load(files[name])
        except yaml.YAMLError:
            return None
        return changelog_of(doc) if isinstance(doc, dict) else None
    return None


@dataclass
class Baseline:
    """What the working copy was checked out from.

    `tree_digest` is computed HERE, over the bytes written, and is NOT the
    server's `bundle_version.digest` — /apps/files does not return one. It
    answers "has the working copy changed since checkout", which is the
    question publish needs and a purely local one. Named distinctly so nobody
    later assumes the two are comparable.
    """

    app: str
    semver: str
    base_version_id: int
    tree_digest: str
    kind: str = "product"
    # The line name is only ever displayed, and /apps/files does not carry it
    # (AppFilesResponse is app/kind/version_id/semver/files), so a checkout
    # leaves this None. `app list` is where the real value lives.
    fork_name: str | None = None
    # Resolved UUID, not the "#name" that was typed: resolve_conversation
    # accepts either and a UUID survives a channel rename. None in a v1
    # baseline.
    conversation_id: str | None = None
    # The checked-out manifest's `changelog:`, so `template check` can tell a
    # note rewritten for this version from the previous version's left in
    # place. None means the manifest declared none — NOT that the baseline
    # predates the field; `changelog_recorded` is what separates those.
    changelog: str | None = None
    version: int = _VERSION

    @property
    def changelog_recorded(self) -> bool:
        """Whether `changelog` was captured at checkout and so is comparable."""
        return self.version >= CHANGELOG_VERSION

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "version": self.version,
            "app": self.app,
            "kind": self.kind,
            "semver": self.semver,
            "base_version_id": self.base_version_id,
            "tree_digest": self.tree_digest,
        }
        if self.fork_name:
            d["fork_name"] = self.fork_name
        if self.conversation_id:
            d["conversation_id"] = self.conversation_id
        if self.changelog:
            d["changelog"] = self.changelog
        return d


def tree_digest(files: dict[str, str]) -> str:
    """SHA-256 over paths and contents, in sorted path order.

    Length-prefixed framing on both path and body: without it, renaming
    `ab`→`a` while prepending `b` to the next file would hash identically.
    """
    h = hashlib.sha256()
    for path in sorted(files):
        body = files[path].encode("utf-8")
        h.update(f"{len(path)}:".encode())
        h.update(path.encode("utf-8"))
        h.update(f"{len(body)}:".encode())
        h.update(body)
    return h.hexdigest()


def files_from_response(resp: dict[str, Any]) -> dict[str, str]:
    """`{path: content}` from an /apps/files payload."""
    out: dict[str, str] = {}
    for item in resp.get("files") or []:
        path = item.get("path")
        if not path:
            continue
        out[path] = item.get("content") or ""
    return out


def _reject_unsafe(path: str) -> None:
    """Refuse a served path that would escape the checkout directory.

    The server builds these from bundle rows, not from caller input, so this
    should never fire — but it is the one place a remote string becomes a
    filesystem write, and `../` traversal is not a failure mode worth
    trusting a peer about.
    """
    p = Path(path)
    if p.is_absolute() or any(part == ".." for part in p.parts):
        raise PopcornError(
            f"refusing to write unsafe path from server: {path!r}",
            error_code="validation",
        )


def write_tree(directory: Path, files: dict[str, str]) -> list[str]:
    """Write every file, creating parent directories. Returns paths written.

    Content is written verbatim — no added trailing newline, no re-encoding —
    so a checkout round-trips the served bytes exactly.
    """
    written: list[str] = []
    for path in sorted(files):
        _reject_unsafe(path)
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files[path].encode("utf-8"))
        written.append(path)
    return written


def occupied(directory: Path) -> bool:
    """True when `directory` holds anything checkout did not write itself.

    A directory containing only our own baseline and the guide beside it is a
    re-checkout of the same working copy, not a collision, so it does not need
    --force.

    The guide only counts as ours when the baseline is there with it. A lone
    `CLAUDE.md` is somebody else's project file, and scattering a bundle over
    their directory is precisely the collision this exists to catch.
    """
    if not directory.exists():
        return False
    ours = {BASELINE_FILE}
    if (directory / BASELINE_FILE).exists():
        ours.add(GUIDE_FILE)
    return any(p.name not in ours for p in directory.iterdir())


def write_baseline(directory: Path, baseline: Baseline) -> Path:
    target = directory / BASELINE_FILE
    target.write_text(json.dumps(baseline.to_dict(), indent=2) + "\n")
    return target


def write_agent_guide(directory: Path, force: bool = False) -> Path | None:
    """Write `CLAUDE.md` into the checkout. Returns the path, or None if kept.

    Separate from `write_baseline` because the two answer to different rules:
    the baseline is derived state and is always rewritten to match what was
    served, while this is prose a reader may have edited and the edit is worth
    more than a refresh. So an existing file is left alone unless the caller
    is already overwriting the working copy (`--force`), which is the one
    moment the author has said they want checkout's version of these files.
    """
    target = directory / GUIDE_FILE
    if target.exists() and not force:
        return None
    target.write_text(GUIDE_TEXT)
    return target


def read_baseline(directory: Path) -> Baseline | None:
    """The checkout's baseline, or None when absent or unreadable.

    Unreadable is treated as absent rather than raised: a corrupt baseline
    should send you to `app checkout` again, not wedge every command.
    """
    target = directory / BASELINE_FILE
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or "base_version_id" not in data:
        return None
    return Baseline(
        app=data.get("app", ""),
        semver=data.get("semver", ""),
        base_version_id=data["base_version_id"],
        tree_digest=data.get("tree_digest", ""),
        kind=data.get("kind", "product"),
        fork_name=data.get("fork_name"),
        conversation_id=data.get("conversation_id"),
        changelog=data.get("changelog"),
        version=data.get("version", 1),
    )


def baseline_from_response(
    resp: dict[str, Any],
    files: dict[str, str],
    conversation_id: str | None = None,
) -> Baseline:
    return Baseline(
        app=resp.get("app", ""),
        kind=resp.get("kind", "product"),
        semver=resp.get("semver", ""),
        base_version_id=resp.get("version_id", 0),
        conversation_id=conversation_id,
        changelog=manifest_changelog(files),
        tree_digest=tree_digest(files),
    )
