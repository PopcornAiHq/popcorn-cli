"""Tarball creation for deploy push — filesystem + subprocess I/O."""

from __future__ import annotations

import subprocess
import tarfile
import tempfile
from pathlib import Path

from .errors import PopcornError


def create_tarball() -> str:
    """Create a gzipped tarball of the current directory, respecting .gitignore."""
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
                    if f and f != ".popcorn.local.json":
                        tar.add(f)
        else:
            with tarfile.open(tarball, "w:gz") as tar:
                for item in Path(".").iterdir():
                    if item.name not in (".git", "node_modules", ".popcorn.local.json"):
                        tar.add(item)
    except PopcornError:
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
