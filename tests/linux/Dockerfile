# A Linux host with systemd, for the tests that self-skip on macOS.
#
# ⛔ WHY THIS FILE EXISTS, AND IT IS NOT "CI CONVENIENCE". On 2026-09-04 the overnight reviewer
# reported "I HAVE NO LINUX HOST" and self-skipped both `systemd-analyze verify` tests — correctly,
# and while an image exactly like this one had been sitting on the build machine since the previous
# day, built by hand for this very work. The capability existed, was invisible, and would have died
# with the laptop. An honest coverage statement had become a false constraint: a correctly-declared
# gap is not evidence that the gap is necessary.
#
# It is committed for the same reason `scripts/install_hooks.sh` argues its hook must be tracked —
# an uncommitted capability protects exactly one clone.
#
#   docker build -t levain-linux-systemd -f tests/linux/Dockerfile tests/linux
#   docker run --rm -v "$PWD":/src:ro levain-linux-systemd \
#       sh -c 'cp -r /src /tmp/w && cd /tmp/w && python3 -m pytest tests/test_daemon.py -k systemd -q'
#
# Verified 2026-09-04: 17 passed, 56 deselected — the two `systemd-analyze verify` tests that
# self-skip on macOS RUN and PASS here, against systemd 255 (255.4-1ubuntu8.17).
#
# ⚠ THIS IMAGE DOES NOT COVER `bwrap`, AND DO NOT ASSUME IT DOES. The 8 `linux_live` confinement
# tests need a kernel that grants unprivileged user namespaces; measured in a plain container,
# `bwrap 0.9.0` refuses with "No permissions to create new namespace". Those tests still require a
# real Linux host (argushub). What this image buys is the systemd half — unit RENDERING and
# `systemd-analyze verify` — which is pure text and needs no namespace.
FROM ubuntu:24.04
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends systemd python3 python3-pytest ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -s /bin/bash entity
USER entity
WORKDIR /home/entity
# `systemd-analyze verify --user` — the form the tests use — needs a runtime dir, and without one it
# fails with "Failed to lookup RuntimeDirectory path" / "Failed to initialize manager" BEFORE it ever
# looks at the unit. That reads exactly like a rejected unit, so the first run here reported two
# FAILURES that were the container's, not levain's. Setting it in the image means the invocation in
# the header works as written; an environment gap that surfaces as a unit-verification failure is
# the kind of false red that teaches people to ignore a suite.
ENV XDG_RUNTIME_DIR=/tmp/xdg
RUN mkdir -p /tmp/xdg
