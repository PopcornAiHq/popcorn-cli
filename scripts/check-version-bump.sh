#!/bin/sh
# Pre-commit hook:
# 1. Warn if src/ files are staged but pyproject.toml is not (forgotten version bump)
# 2. Error if pyproject.toml is staged but uv.lock is not (forgotten lockfile)

staged_src=$(git diff --cached --name-only -- 'src/')
staged_toml=$(git diff --cached --name-only -- 'pyproject.toml')
staged_lock=$(git diff --cached --name-only -- 'uv.lock')
dirty_lock=$(git diff --name-only -- 'uv.lock')

if [ -n "$staged_src" ] && [ -z "$staged_toml" ]; then
    echo ""
    echo "⚠  src/ files changed but pyproject.toml was not updated."
    echo "   Did you forget to bump the version?  (make bump v=X.Y.Z)"
    echo ""
fi

if [ -n "$staged_toml" ] && [ -z "$staged_lock" ] && [ -n "$dirty_lock" ]; then
    echo ""
    echo "✖  pyproject.toml is staged but uv.lock is not."
    echo "   Run: git add uv.lock"
    echo ""
    exit 1
fi
