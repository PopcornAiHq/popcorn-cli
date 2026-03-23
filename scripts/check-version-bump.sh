#!/bin/sh
# Pre-commit hook: warn if src/ files are staged but pyproject.toml is not.
# This catches forgotten version bumps.

staged_src=$(git diff --cached --name-only -- 'src/')
staged_toml=$(git diff --cached --name-only -- 'pyproject.toml')

if [ -n "$staged_src" ] && [ -z "$staged_toml" ]; then
    echo ""
    echo "⚠  src/ files changed but pyproject.toml was not updated."
    echo "   Did you forget to bump the version?  (make bump v=X.Y.Z)"
    echo ""
    # Warning only — don't block the commit
    # exit 1
fi
