#!/usr/bin/env bash
# Install levain's dependencies from the committed uv.lock, hash-checked, then levain itself without resolving.
# usage: scripts/ci_install_locked.sh [extra ...]     (each argument names a pyproject extra: dev, openhands, isolation)
# The lock is the one source of versions: a new upstream release changes nothing here until uv.lock is regenerated
# and committed. UV_VERSION is the uv that reads and exports the lock.
set -euo pipefail
UV_VERSION="${UV_VERSION:-0.13.5}"
tmp="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
extras=()
for e in "$@"; do extras+=(--extra "$e"); done
python -m pip install --upgrade pip "uv==${UV_VERSION}"
uv export --frozen --no-emit-project --no-header --hashes "${extras[@]}" -o "$tmp/levain-req.txt"
python -m pip install --require-hashes --no-deps -r "$tmp/levain-req.txt"
python -m pip install --no-deps -e .
