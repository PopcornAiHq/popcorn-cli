#!/bin/sh
# Pre-commit hook and CI check: this repository is PUBLIC.
#
# Internal-only references must not land in it — issue-tracker ids, private-repo
# PR numbers, and source paths from the private backend repo. A scrub removed
# the ones that had accumulated; nothing but memory was holding that in place,
# and the private sibling repo's own conventions actively encourage writing
# them, so an engineer or agent moving between the two checkouts reintroduces
# them while believing they are following house style. This fails instead of
# remembering. The rule, and what to write instead, is in CLAUDE.md
# ("This repository is public").
#
# Scans the INDEX (`git grep --cached`), which is both what a commit is about to
# contain and, in a CI checkout, the whole tracked tree — so the hook and the CI
# job are one command with one scope. Scanning the index rather than only the
# staged paths is deliberate: a hook that sees a single file cannot tell you the
# tree already contains a violation.
#
# Usage: scripts/check-public-repo.sh   (no arguments, in either context)

set -eu

# Skip this file: it necessarily contains the patterns it searches for.
self=':(exclude)scripts/check-public-repo.sh'

# Each line is "<what it is>|<extended regex>". Deliberately narrow — a pattern
# that cries wolf gets switched off, which is worse than one that misses.
#
# A bare "#<number>" is unusable as a private-repo PR reference: it collides
# with this repo's own PR numbers, which are legitimate here.
#
# The backend-path regex requires a ".py" tail and a preceding non-path
# character, so a nested path inside a synthetic fixture tree
# ("code/calc/lib/deep/util.py") is not read as a backend source reference while
# "backend: lib/<domain>/services/<name>.py" still is.
found=0

while IFS='|' read -r label regex; do
    [ -n "$label" ] || continue

    set +e
    hits=$(git grep --cached -nIE -e "$regex" -- "$self")
    status=$?
    set -e

    case "$status" in
        0)
            found=1
            echo ""
            echo "✖  $label in a public repo:"
            printf '%s\n' "$hits" | sed 's/^/     /'
            ;;
        1) ;;  # no match — the success case
        *)
            echo "✖  git grep failed (exit $status) scanning for $label" >&2
            exit "$status"
            ;;
    esac
done <<'PATTERNS'
issue-tracker id|KEW-[0-9]+
private-repo PR reference|popcorn-backend#[0-9]+
backend source path|(^|[^/[:alnum:]_.-])(lib|services)/[a-z_]+/[a-z_/]*\.py
PATTERNS

if [ "$found" -eq 1 ]; then
    cat <<'MSG'

   This repository is public. Cite behaviour ("the server refuses X"), never the
   ticket or private source file that proves it. Fixture identifiers are
   synthetic: example-* and 00000000-0000-4000-8000-0000000000NN.

   See CLAUDE.md — "This repository is public".
MSG
    exit 1
fi
