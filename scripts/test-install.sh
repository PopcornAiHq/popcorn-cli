#!/usr/bin/env bash
# Test that popcorn-cli installs correctly with pip, pipx, and uv.
# Requires: Docker, uv (to build the wheel)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"
IMAGE="python:3.12-slim"

# Build the wheel
echo "Building wheel..."
(cd "$REPO_ROOT" && uv build --quiet)

# Find the wheel
WHEEL=$(ls -t "$DIST_DIR"/popcorn_cli-*.whl 2>/dev/null | head -1)
if [[ -z "$WHEEL" ]]; then
  echo "ERROR: No wheel found in $DIST_DIR" >&2
  exit 1
fi
WHEEL_NAME=$(basename "$WHEEL")
echo "Using $WHEEL_NAME"
echo ""

FAILED=0

run_test() {
  local name=$1
  local script=$2

  echo "--- $name ---"
  if docker run --rm -v "$DIST_DIR:/dist:ro" "$IMAGE" bash -c "$script"; then
    echo "PASS: $name"
  else
    echo "FAIL: $name"
    FAILED=1
  fi
  echo ""
}

run_test "pip" "
  pip install --quiet /dist/$WHEEL_NAME 2>&1
  popcorn --version
  popcorn --help >/dev/null
"

run_test "pipx" "
  pip install --quiet pipx 2>&1
  pipx install /dist/$WHEEL_NAME 2>&1
  export PATH=\"\$PATH:/root/.local/bin\"
  popcorn --version
  popcorn --help >/dev/null
"

run_test "uv" "
  pip install --quiet uv 2>&1
  uv pip install --system /dist/$WHEEL_NAME 2>&1
  popcorn --version
  popcorn --help >/dev/null
"

if [[ $FAILED -eq 0 ]]; then
  echo "All install methods passed."
else
  echo "Some install methods failed." >&2
  exit 1
fi
