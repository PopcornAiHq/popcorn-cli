#!/bin/sh
# Tag and release the version sitting on `main`, if it has not been tagged yet.
#
# This does NOT choose a version. The number is hand-edited into
# `pyproject.toml` inside a PR, by a person, and reviewed in the diff — patch
# vs minor stays a human judgement (CLAUDE.md, "Versioning"). All this does is
# the `git tag && git push` that follows a merge — the step with no owner,
# which is why runs of consecutive versions have reached `main` untagged.
#
# That is worse than untidy. The CLI resolves a TAG: `_VERSION_CHECK_URL` reads
# the tags API and self-upgrade installs `@vX.Y.Z`, so an untagged version is
# unreachable by anyone upgrading, while `pyproject.toml` on `main` claims it
# exists.
#
# Usage:
#   scripts/auto_tag.sh              # tag, push, release
#   scripts/auto_tag.sh --dry-run    # say what it would do, touch nothing
#
# It creates the release itself rather than letting `release.yml` fire on the
# tag push: a tag pushed with the default GITHUB_TOKEN does NOT trigger another
# workflow, so relying on that would leave a reachable version with no release
# and no error. `release.yml` still handles tags pushed by hand.

set -eu

dry_run=0
[ "${1:-}" = "--dry-run" ] && dry_run=1

version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
[ -n "$version" ] || { echo "✖  no version found in pyproject.toml" >&2; exit 1; }
tag="v${version}"

if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null; then
    echo "✓  ${tag} is already tagged — nothing to do."
    echo "   (normal for a merge that ships nothing to users and carries no bump)"
    exit 0
fi

latest=$(git describe --tags --abbrev=0 2>/dev/null || echo "")

# Refuse a major bump. Going to 1.0 is a product decision, not a consequence of
# a merge, and refusing here also means a typo'd major cannot publish itself.
# While the project is pre-1.0 a breaking change rides in a minor bump, which
# is what the removals of whole command families used.
if [ -n "$latest" ]; then
    new_major=${version%%.*}
    old_major=${latest#v}; old_major=${old_major%%.*}
    if [ "$new_major" != "$old_major" ]; then
        echo "✖  ${latest} -> ${tag} changes the major version." >&2
        echo "   Refusing to tag it automatically. Tag it by hand if that is intended:" >&2
        echo "     git tag ${tag} && git push origin ${tag}" >&2
        exit 1
    fi
fi

if [ "$dry_run" = "1" ]; then
    echo "would tag ${tag} at $(git rev-parse --short HEAD) (previous: ${latest:-none})"
    tmp=$(mktemp)
    # Notes need the tag to exist; make it locally, read them, then drop it.
    git tag "$tag"
    scripts/release_notes.sh "$tag" "$tmp"
    git tag -d "$tag" >/dev/null
    echo "would release with notes:"
    sed 's/^/    /' "$tmp"
    rm -f "$tmp"
    exit 0
fi

echo "Tagging ${tag} at $(git rev-parse --short HEAD) (previous: ${latest:-none})"
git tag "$tag"
git push origin "$tag"

scripts/release_notes.sh "$tag" release-notes.md
gh release create "$tag" --title "$tag" --notes-file release-notes.md
rm -f release-notes.md
echo "✓  released ${tag}"
