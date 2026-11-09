#!/usr/bin/env bash
# Install levain's dependencies from the committed uv.lock, hash-checked, then levain itself without resolving.
# usage: [LOCK_DIR=dir] scripts/ci_install_locked.sh [extra ...]     (each argument is a pyproject extra name; LOCK_DIR is the directory whose uv.lock is exported, default the current one)
# The lock is the one source of versions: a new upstream release changes nothing here until uv.lock is regenerated
# and committed. Two hashed installs, in order: the `build` dependency group (the build backend, wheels), then every
# other package with --no-build-isolation, so a source-only package builds with the backend just installed from the
# lock instead of fetching its build requirements unhashed. The project itself installs last, without isolation too.
# Unhashed and fetched by version only: pip and uv themselves (the bootstrap hop below). UV_VERSION and PIP_VERSION
# override the pins.
set -euo pipefail
UV_VERSION="${UV_VERSION:-0.13.5}"
PIP_VERSION="${PIP_VERSION:-26.3}"
tmp="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
extras=()
for e in "$@"; do extras+=(--extra "$e"); done
python -m pip install "pip==${PIP_VERSION}" "uv==${UV_VERSION}"
uv --directory "${LOCK_DIR:-.}" export --frozen --no-emit-project --no-header --hashes --only-group build -o "$tmp/levain-build.txt"
# ${extras[@]+...}: an empty array is an unbound variable under set -u on bash before 4.4 (macOS /bin/bash is 3.2).
uv --directory "${LOCK_DIR:-.}" export --frozen --no-emit-project --no-header --hashes ${extras[@]+"${extras[@]}"} -o "$tmp/levain-req.txt"
python -m pip install --require-hashes --no-deps -r "$tmp/levain-build.txt"
python -m pip install --require-hashes --no-deps --no-build-isolation -r "$tmp/levain-req.txt"
python -m pip install --no-deps --no-build-isolation -e .
