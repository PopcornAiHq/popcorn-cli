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
# Commit messages are scanned too, because they are published as-is: every
# merge method `main` allows publishes the branch's commit messages, the pull
# request's title, or both, so a private path in a feature-branch commit lands
# on `main` permanently. `--message` is the commit-msg hook; `--commits` is the
# CI pass over a pull request's commits.
#
# Issue-tracker ids are the one category a message MAY carry. The tracker's
# GitHub integration links and closes issues from the id in a PR title, PR
# description or commit message, and an id alone discloses no more than the
# behaviour the commit already describes. They stay banned from tracked files,
# where they would be read as documentation by people who cannot open them.
#
# Usage: scripts/check-public-repo.sh                         tracked files (index)
#        scripts/check-public-repo.sh --message <file>        one commit message
#        scripts/check-public-repo.sh --commits <base>..<head> every message in a range

set -eu

# Skip this file: it necessarily contains the patterns it searches for.
self=':(exclude)scripts/check-public-repo.sh'

mode=index
target=
case "${1:-}" in
    "") ;;
    --message|--commits)
        [ $# -eq 2 ] || { echo "usage: $0 [--message <file> | --commits <base>..<head>]" >&2; exit 2; }
        mode=${1#--}
        target=$2
        ;;
    *)
        echo "usage: $0 [--message <file> | --commits <base>..<head>]" >&2
        exit 2
        ;;
esac

if [ "$mode" = message ] && [ ! -r "$target" ]; then
    echo "✖  cannot read commit message file: $target" >&2
    exit 2
fi

# The comment character git strips from a message. `auto` picks one per commit
# from the message's own content, which this cannot reproduce, so it falls back
# to the default like an unset value does.
cc=$(git config --get core.commentChar 2>/dev/null || true)
case "$cc" in
    ""|auto) cc='#' ;;
esac

# Resolved once, up front: inside `search` a bad range would fail in a command
# substitution nobody checks, and a scan of zero commits reports clean.
if [ "$mode" = commits ]; then
    # A single ref is valid rev-list syntax meaning "all of its history", which
    # would scan every commit ever made rather than the pull request's own.
    case "$target" in
        *..*) ;;
        *)
            echo "✖  --commits needs a range (<base>..<head>), got: $target" >&2
            exit 2
            ;;
    esac
    commits=$(git rev-list "$target") || {
        echo "✖  cannot resolve commit range: $target" >&2
        exit 2
    }
fi

# Print matches of $1 in whatever this mode scans; exit 0 on a match, 1 on
# none, anything else on failure — git grep's contract, kept for every mode.
search() {
    case "$mode" in
        index)
            git grep --cached -nIE -e "$1" -- "$self"
            ;;
        message)
            # Only what git keeps is published. `git commit -v` appends the
            # diff below a scissors line WITHOUT comment prefixes, and the hook
            # sees that unstripped file — so a commit that removes an id would
            # be blocked by its own diff. Cut at the scissors, then drop comment
            # lines. awk compares strings, so no comment character needs regex
            # escaping.
            awk -v cc="$cc" '
                BEGIN { scissors = cc " ------------------------ >8 ------------------------" }
                $0 == scissors { exit }
                substr($0, 1, length(cc)) == cc { next }
                { print }
            ' "$target" | grep -nE -e "$1"
            ;;
        commits)
            out=
            for commit in $commits; do
                short=$(git rev-parse --short "$commit")
                hits=$(git log -1 --format=%B "$commit" | grep -nE -e "$1" || true)
                [ -z "$hits" ] || out="$out$(printf '%s\n' "$hits" | sed "s/^/$short:/")
"
            done
            [ -n "$out" ] || return 1
            printf '%s' "$out"
            ;;
    esac
}

# Each line is "<what it is>|<scope>|<extended regex>". Only the first two "|"
# separate fields, so a regex may contain alternations. <scope> is `all` (every
# mode) or `files` (the index scan only — commit messages and PR titles may
# carry it). Deliberately narrow — a pattern that cries wolf gets switched off,
# which is worse than one that misses.
#
# A bare "#<number>" is unusable as a private-repo PR reference: it collides
# with this repo's own PR numbers, which are legitimate here.
#
# The backend-path regex requires a ".py" tail and a preceding non-path
# character, so a nested path inside a synthetic fixture tree
# ("code/calc/lib/deep/util.py") is not read as a backend source reference while
# "backend: lib/<domain>/services/<name>.py" still is.
found=0

while IFS='|' read -r label scope regex; do
    [ -n "$label" ] || continue
    [ "$scope" = all ] || [ "$mode" = index ] || continue

    set +e
    hits=$(search "$regex")
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
            echo "✖  scan failed (exit $status) looking for $label" >&2
            exit "$status"
            ;;
    esac
done <<'PATTERNS'
issue-tracker id|files|KEW-[0-9]+
private-repo PR reference|all|popcorn-backend#[0-9]+
backend source path|all|(^|[^/[:alnum:]_.-])(lib|services)/[a-z_]+/[a-z_/]*\.py
PATTERNS

if [ "$found" -eq 1 ]; then
    cat <<'MSG'

   This repository is public. Cite behaviour ("the server refuses X"), never the
   private source file or pull request that proves it. Fixture identifiers are
   synthetic: example-* and 00000000-0000-4000-8000-0000000000NN. A ticket id
   belongs in a commit message or pull request, not in a tracked file.

   See CLAUDE.md — "This repository is public".
MSG
    if [ "$mode" != index ]; then
        cat <<'MSG'

   A commit message on a branch becomes part of main's history when the pull
   request is merged, whichever merge method is used. Reword it before
   pushing: `git commit --amend` for the last commit, an interactive rebase for
   an earlier one.
MSG
    fi
    exit 1
fi
