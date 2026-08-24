"""Materialize an app bundle onto disk, and the baseline that tracks it.

`popcorn app checkout` writes the channel's bound version as files plus a
`.popcorn-app.json` baseline. The baseline is what `app publish` diffs
against: it names the version the working copy came from, so a publish can be
refused when the channel has moved underneath it, and it names the channel so
`publish`/`apply`/`status` need no `--channel`.

The baseline lives INSIDE the checkout directory but is not bundle content.
It is a dotfile so `template check`'s globs skip it, and publish must exclude
dotfiles for the same reason.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PopcornError

BASELINE_FILE = ".popcorn-app.json"
# 2 added `conversation_id`. A v1 baseline still parses — every field is read
# with a default — and its commands fall back to an explicit --channel rather
# than being rewritten underneath the user.
_VERSION = 2


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
    version: int = _VERSION

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
    """True when `directory` holds anything other than a stale baseline.

    A directory containing only our own baseline is a re-checkout of the same
    working copy, not a collision, so it does not need --force.
    """
    if not directory.exists():
        return False
    entries = [p for p in directory.iterdir() if p.name != BASELINE_FILE]
    return bool(entries)


def write_baseline(directory: Path, baseline: Baseline) -> Path:
    target = directory / BASELINE_FILE
    target.write_text(json.dumps(baseline.to_dict(), indent=2) + "\n")
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
        tree_digest=tree_digest(files),
    )
