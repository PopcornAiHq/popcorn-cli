#!/usr/bin/env python3
"""PR gate for the version in `pyproject.toml`.

Two failures this catches, both of which have happened here:

* **A bump that never happened.** `src/` changes merge with the version
  untouched, so `scripts/auto_tag.sh` correctly tags nothing and the change
  ships unreachable — self-upgrade resolves a tag, and there is no tag.
  `scripts/check-version-bump.sh` only *warns*, only pre-commit, and only for
  someone who ran `pre-commit install`.
* **Two PRs claiming one version.** Both branch from the same `main`, both bump
  to the same number, and the second merge is a no-op on that line — git
  resolves two sides setting a value to the *same* value without a conflict,
  and GitHub reports it mergeable. Two unrelated feature sets then share a
  version.

It never chooses a version. Patch vs minor is a human judgement made in the PR
(CLAUDE.md, "Versioning"); this only rejects a number that cannot be right.

**Known limit.** The collision case is only visible once this PR's base
includes the other merge. A branch that never updates after the other one lands
is checked against a stale base and passes. "Require branches to be up to date
before merging" is what closes that; the check alone cannot.

Usage: scripts/check_version.py [base-ref]      (default: origin/main)
"""

from __future__ import annotations

import pathlib
import subprocess
import sys


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def decide(
    head_version: str,
    base_version: str,
    src_changed: bool,
    existing_tags: set[str],
) -> tuple[bool, str]:
    """Return (ok, message). Pure, so every branch below is directly testable."""
    if head_version == base_version:
        if src_changed:
            return False, (
                f"src/ changed but the version is still {head_version}.\n"
                "   Bump it in pyproject.toml (and run `uv lock`), or the change\n"
                "   merges with no tag and never reaches anyone upgrading.\n"
                "   Patch for a fix, minor for a feature or a breaking change —\n"
                "   see CLAUDE.md, 'Versioning'."
            )
        return True, f"version unchanged at {head_version}; no src/ change — nothing to release."

    if f"v{head_version}" in existing_tags:
        return False, (
            f"v{head_version} is already released.\n"
            "   Another PR claimed this number and merged first. Pick the next\n"
            "   one; git will not flag it, because both sides set the same line\n"
            "   to the same value."
        )

    try:
        head_t, base_t = _version_tuple(head_version), _version_tuple(base_version)
    except ValueError:
        return False, f"cannot compare versions {base_version!r} -> {head_version!r}"

    if head_t < base_t:
        return False, f"version goes backwards: {base_version} -> {head_version}."

    if head_t[0] != base_t[0]:
        return False, (
            f"{base_version} -> {head_version} is a major bump.\n"
            "   Major is a product decision and stays manual: scripts/auto_tag.sh\n"
            "   refuses it, so this would merge and never be released."
        )

    return True, f"version {base_version} -> {head_version}."


def _run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def _version_in(ref: str | None) -> str:
    if ref:
        text = _run("git", "show", f"{ref}:pyproject.toml")
    else:
        text = pathlib.Path("pyproject.toml").read_text()
    for line in text.splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise SystemExit("✖  no version found in pyproject.toml")


def main() -> int:
    base_ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    base = _run("git", "merge-base", "HEAD", base_ref)

    head_version = _version_in(None)
    base_version = _version_in(base)
    src_changed = bool(_run("git", "diff", "--name-only", f"{base}...HEAD", "--", "src/"))
    tags = set(_run("git", "tag").splitlines())

    ok, message = decide(head_version, base_version, src_changed, tags)
    print(("✓  " if ok else "✖  ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
