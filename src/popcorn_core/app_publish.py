"""Turn an edited checkout into an `/apps/publish` request.

`app publish` sends a diff, not a tree: `{base_version_id, files, deletes}`
over the version the checkout came from. Everything here is the local half of
that — collecting the working copy, diffing it, and refusing the four things
the server would refuse anyway, so an obvious mistake costs no round trip.

The shape rules mirror `backend:lib/temporal/flows/templates.py` — the server
is the source of truth and re-checks all of it; these copies exist to produce
a better message, never to decide. Two of them are worth naming:

- `collect_tree` mirrors `bundle_file_tree`, the server's own DISK collector,
  which silently skips anything the installer would not read — `evals/` in
  `claimcoordinator` is shipped in-repo and filtered exactly this way. So
  auxiliary files are filtered here too, and reported so a misplaced
  `flows/alert.yaml` is visible rather than a mystery. Refusing them instead
  would make the CLI stricter than the server on identical input and would
  reject the ordinary bundle layout the CLI's own docs teach.
  `unrecognized_tree_paths` — the server's refusal — applies to a tree
  already sent, which is never what this builds.
- `parse_semver` is strict for a reason the YAML makes non-obvious: an
  unquoted `version: 1.0` parses as the float 1.0 and `version: 1.0.0` as a
  string, so the two look identical in the file. Rejecting the float here is
  what stops `"1.0"` reaching the registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .app_checkout import BASELINE_FILE, tree_digest
from .errors import PopcornError

# One level under these seeds a channel_parameter of the same name; nothing
# else may be a directory in a bundle.
FILES_SUBDIRS = ("prompts", "templates")
MANIFEST_FILENAMES = ("manifest.yaml", "config.yaml")
_DOC_FILENAMES = ("AGENT.md", "README.md")
# Byproducts, never authored content — the only paths skipped without comment.
_SILENT_SKIPS = ("__pycache__",)

_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def parse_semver(version: str) -> tuple[int, int, int]:
    """`"2.4.0"` → `(2, 4, 0)`. Raises on anything else, including `"1.0"`."""
    match = _SEMVER_RE.match(version.strip())
    if match is None:
        raise PopcornError(
            f"{version!r} is not MAJOR.MINOR.PATCH",
            error_code="validation",
        )
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _is_bundle_file(filename: str) -> bool:
    """Whether a ROOT-level filename is installable bundle content."""
    if filename.startswith("."):
        return False
    if filename in _DOC_FILENAMES:
        return True
    return filename.endswith((".yaml", ".yml"))


def recognized(path: str) -> bool:
    """Whether this CLI would collect `path` from disk.

    Applied to SERVED paths as well as local ones, which is the point: the
    server may publish a shape this CLI predates — another `_FILES_SUBDIRS`
    entry is the obvious candidate — and a path we cannot collect is one we
    must not claim the user deleted.
    """
    if "/" not in path:
        return _is_bundle_file(path)
    parts = path.split("/")
    return (
        len(parts) == 2
        and parts[0] in FILES_SUBDIRS
        and bool(parts[1])
        and not parts[1].startswith(".")
    )


@dataclass
class LocalTree:
    """A working copy, read as the server would classify it."""

    files: dict[str, str] = field(default_factory=dict)
    # Paths the installer would not read, so they are not published. Reported
    # rather than dropped quietly: an edit to `flows/alert.yaml` — a plausible
    # wrong guess at the layout — would otherwise look like it published.
    ignored: list[str] = field(default_factory=list)


def collect_tree(directory: Path) -> LocalTree:
    """Read `directory` as a bundle tree, classifying every path.

    Dotfiles are skipped silently: the baseline is one, and a bundle has no
    hidden members by definition. `__pycache__` is skipped for the same
    reason. Everything else the installer would not read lands in `ignored`,
    which the caller prints — an authoring directory legitimately holds
    fixtures and notes, and refusing them would reject the very examples the
    CLI docs ship.
    """
    tree = LocalTree()
    if not directory.is_dir():
        raise PopcornError(f"{directory} is not a directory", error_code="not_found")

    for entry in sorted(directory.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or entry.name in _SILENT_SKIPS:
            continue
        if entry.is_dir():
            if entry.name not in FILES_SUBDIRS:
                tree.ignored.append(f"{entry.name}/")
                continue
            for child in sorted(entry.iterdir(), key=lambda p: p.name):
                if child.name.startswith(".") or child.name in _SILENT_SKIPS:
                    continue
                rel = f"{entry.name}/{child.name}"
                if not child.is_file():
                    # Nested a level too deep — read_template ignores it, so
                    # publishing it would change nothing.
                    tree.ignored.append(f"{rel}/")
                    continue
                tree.files[rel] = _read_text(child)
            continue
        if not entry.is_file():
            continue
        if not _is_bundle_file(entry.name):
            tree.ignored.append(entry.name)
            continue
        tree.files[entry.name] = _read_text(entry)
    return tree


def _read_text(path: Path) -> str:
    """UTF-8 text, with the decode failure named by path.

    A bundle has no binary members — every path the format allows is decoded
    at install time — so a file that is not text here could not install, and
    saying which one beats a UnicodeDecodeError traceback.
    """
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise PopcornError(
            f"{path} is not UTF-8 text — a bundle has no binary files",
            error_code="validation",
        ) from None


def manifest_version(files: dict[str, str]) -> str:
    """The working copy's manifest `version:`.

    `manifest.yaml` wins over the legacy `config.yaml` when both exist, the
    same precedence `template_from_tree` applies.
    """
    for name in MANIFEST_FILENAMES:
        if name not in files:
            continue
        try:
            doc = yaml.safe_load(files[name]) or {}
        except yaml.YAMLError as exc:
            raise PopcornError(f"{name}: {exc}", error_code="validation") from exc
        if not isinstance(doc, dict):
            raise PopcornError(
                f"{name}: top-level YAML must be a mapping",
                error_code="validation",
            )
        if "version" not in doc:
            raise PopcornError(
                f"{name} declares no 'version:' — a publish must name the version it is minting",
                error_code="validation",
            )
        raw = doc["version"]
        if not isinstance(raw, str):
            # `version: 1.0` unquoted is a float; `version: 1.0.0` is a str.
            raise PopcornError(
                f"{name}: version must be a quoted MAJOR.MINOR.PATCH string, "
                f'got {raw!r} — write version: "1.0.1"',
                error_code="validation",
            )
        parse_semver(raw)
        return raw
    raise PopcornError(
        "no manifest.yaml in the working copy — is this an app checkout?",
        error_code="not_found",
    )


@dataclass
class TreeDiff:
    """The publish payload's `files` and `deletes`, plus what to print."""

    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    # Served paths this CLI cannot collect, so it cannot know they are gone
    # rather than merely unread. Left in the published version untouched.
    preserved: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.files and not self.deletes

    def summary(self) -> list[str]:
        return [
            *(f"  + {p}" for p in self.added),
            *(f"  M {p}" for p in self.changed),
            *(f"  - {p}" for p in self.deletes),
        ]


def diff_tree(base: dict[str, str], local: dict[str, str]) -> TreeDiff:
    """What changed from the checked-out tree to the working copy.

    `files` carries COMPLETE new contents for added and changed paths — the
    endpoint takes whole files, not patches — and untouched paths are omitted
    entirely so the server's byte-for-byte passthrough covers them.

    A served path absent locally is a deletion ONLY if this CLI would have
    collected it. `collect_tree` filters the local side, so without the
    symmetry a bundle subdirectory this CLI predates would read as "the user
    deleted all of it" and publish would silently drop the lot.
    """
    diff = TreeDiff()
    for path in sorted(local):
        if path not in base:
            diff.added.append(path)
            diff.files[path] = local[path]
        elif local[path] != base[path]:
            diff.changed.append(path)
            diff.files[path] = local[path]
    for path in sorted(base):
        if path in local:
            continue
        if recognized(path):
            diff.deletes.append(path)
        else:
            diff.preserved.append(path)
    return diff


def preserved_note(preserved: list[str]) -> str:
    """Reported, never silent: this CLI is behind the bundle format."""
    return (
        "Left untouched — this popcorn does not recognize them, so it cannot "
        "tell a deletion from a blind spot: "
        + ", ".join(preserved)
        + " (upgrade with 'popcorn upgrade')"
    )


def ignored_note(ignored: list[str]) -> str:
    """The one-line report for paths that will not be published."""
    return (
        "Not installable, so not published: "
        + ", ".join(ignored)
        + " (flows are root-level <name>.yaml; prompts and templates go "
        "exactly one level under prompts/ or templates/)"
    )


def require_bump(local_version: str, base_semver: str) -> None:
    """Refuse a publish whose manifest version did not advance.

    Compared against the BASELINE's semver, which is the fork line's head
    whenever a publish can succeed at all — the endpoint requires the base to
    be the channel's binding, and a publishable binding is the head. When the
    head has genuinely moved further the server's 409 is the honest answer and
    this check does not try to predict it.
    """
    if parse_semver(local_version) > parse_semver(base_semver):
        return
    raise PopcornError(
        f"manifest version {local_version} does not advance past the "
        f"checked-out {base_semver} — bundle versions only ever move forward",
        error_code="validation",
        hint=f"bump 'version:' in manifest.yaml past {base_semver}",
    )


def local_digest(files: dict[str, str]) -> str:
    """The working copy's digest, in the baseline's terms."""
    return tree_digest(files)


def baseline_path_hint(directory: Path) -> str:
    return str(directory / BASELINE_FILE)


def publish_payload(base_version_id: int, diff: TreeDiff, changelog: str | None) -> dict[str, Any]:
    """The `/apps/publish` body.

    `base_version_id` is the BASELINE's, never a freshly fetched binding:
    it is the optimistic-concurrency token for the tree the edits were
    computed against, so re-reading it from the server would defeat the check
    it exists to make.
    """
    body: dict[str, Any] = {
        "base_version_id": base_version_id,
        "files": diff.files,
        "deletes": diff.deletes,
    }
    if changelog:
        body["changelog"] = changelog
    return body
