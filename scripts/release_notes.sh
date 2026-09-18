#!/bin/sh
# Write release notes for a tag to a file. Shared by the two workflows that
# create releases, so the shell-injection guard below lives in one place.
#
# Usage: scripts/release_notes.sh <tag> <output-file>
#
# Notes reach `gh` as a FILE, never as shell text. Commit subjects in this repo
# are full of backticks (`--dir`, `--channel`, `changelog:`), and interpolating
# one into a double-quoted `run:` string makes it a command substitution the
# runner executes, with a write-scoped token in the environment. That is how
# v0.33.0's release failed.

set -eu

tag=$1
out=$2

prev=$(git describe --tags --abbrev=0 "${tag}^" 2>/dev/null || git rev-list --max-parents=0 HEAD)

# `grep -v` exits 1 when it filters everything out, which under `set -e` would
# fail a release whose every commit was a chore.
git log --pretty=format:"- %s" "${prev}..${tag}" | grep -v "^- chore:" > "$out" || true

# An empty notes file makes `gh release create` fail; say something instead.
if [ ! -s "$out" ]; then
    printf -- "- %s\n" "$tag" > "$out"
fi
