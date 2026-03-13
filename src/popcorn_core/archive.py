"""Tarball creation for deploy push — filesystem + subprocess I/O."""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pathspec

from .errors import PopcornError

_HARDCODED_EXCLUDES = {".popcorn.local.json", ".popcornignore"}


def _load_ignore_patterns(root: Path) -> pathspec.PathSpec | None:
    """Read .popcornignore from root and return a compiled PathSpec, or None."""
    ignore_file = root / ".popcornignore"
    if not ignore_file.is_file():
        return None
    lines = ignore_file.read_text().splitlines()
    return pathspec.PathSpec.from_lines("gitignore", lines)


def create_tarball() -> str:
    """Create a gzipped tarball of the current directory, respecting .gitignore."""
    root = Path(".")
    spec = _load_ignore_patterns(root)

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as fd:
        tarball = fd.name

    try:
        if _is_git_repo():
            try:
                files = subprocess.check_output(
                    ["git", "ls-files", "-co", "--exclude-standard"],
                    text=True,
                ).splitlines()
            except subprocess.CalledProcessError as e:
                raise PopcornError(f"Failed to list git-tracked files: {e}") from e
            with tarfile.open(tarball, "w:gz") as tar:
                for f in files:
                    if not f:
                        continue
                    if f in _HARDCODED_EXCLUDES:
                        continue
                    if spec is not None and spec.match_file(f):
                        continue
                    tar.add(f)
        else:
            dir_excludes = {".git", "node_modules"}
            with tarfile.open(tarball, "w:gz") as tar:
                for dirpath, dirnames, filenames in os.walk(root):
                    # Prune .git and node_modules at any depth
                    dirnames[:] = [d for d in dirnames if d not in dir_excludes]
                    for name in filenames:
                        full = os.path.join(dirpath, name)
                        rel = os.path.relpath(full, root)
                        if rel in _HARDCODED_EXCLUDES:
                            continue
                        if spec is not None and spec.match_file(rel):
                            continue
                        tar.add(full, arcname=rel)
    except PopcornError:
        Path(tarball).unlink(missing_ok=True)
        raise
    except Exception as e:
        Path(tarball).unlink(missing_ok=True)
        raise PopcornError(f"Failed to create deploy tarball: {e}") from e

    return tarball


def _is_git_repo() -> bool:
    """Check if the current directory is inside a git repository."""
    try:
        subprocess.check_output(
            ["git", "rev-parse", "--is-inside-work-tree"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
