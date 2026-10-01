# A Linux host with systemd AND bubblewrap, for the tests that self-skip on macOS.
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
# Build from the REPO ROOT (the context is the repo; `Dockerfile.dockerignore` beside this file keeps
# the venv and build output out of it), then run the whole suite:
#
#   docker build -f tests/linux/Dockerfile -t levain-linux .
#   docker run --rm --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
#       --security-opt systempaths=unconfined levain-linux
#
# ⛔ THE THREE --security-opt FLAGS ARE THE DIFFERENCE BETWEEN TESTING THE FLOOR AND SKIPPING IT.
# Docker's default seccomp profile refuses the unprivileged user namespace `bwrap` needs, and its
# masked /proc paths refuse a fresh procfs mount. Without the flags `bwrap_available()` is False and
# every `linux_live` test would SKIP — a green run that never touched the floor. So this image sets
# `LEVAIN_REQUIRE_LINUX_LIVE=1`, which turns that skip into a FAILURE: run it without the flags and
# it goes red, saying why. The flags relax the CONTAINER, not the floor under test.
#
# ⚠ A CONTAINER IS STILL NOT A HOST. It proves the namespace mechanics on a real Linux kernel; it does
# not prove an Ubuntu host's AppArmor posture or a real `systemd --user` session with linger. Those
# need a real machine.
FROM ubuntu:24.04
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends \
      systemd python3 python3-venv bubblewrap ca-certificates git openssh-client procps \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -s /bin/bash entity
USER entity
WORKDIR /home/entity/levain
# `systemd-analyze verify --user` — the form the tests use — needs a runtime dir, and without one it
# fails with "Failed to lookup RuntimeDirectory path" / "Failed to initialize manager" BEFORE it ever
# looks at the unit. That reads exactly like a rejected unit, so the first run here reported two
# FAILURES that were the container's, not levain's. Setting it in the image means the invocation in
# the header works as written; an environment gap that surfaces as a unit-verification failure is
# the kind of false red that teaches people to ignore a suite.
ENV XDG_RUNTIME_DIR=/tmp/xdg \
    LEVAIN_REQUIRE_LINUX_LIVE=1
RUN mkdir -p /tmp/xdg && python3 -m venv /home/entity/venv
COPY --chown=entity pyproject.toml README.md LICENSE NOTICE MANIFEST.in ./
COPY --chown=entity levain ./levain
RUN /home/entity/venv/bin/pip install -q -e ".[dev,openhands]"
# Then the rest of the repo: the suite reads bench/, scripts/, examples/ and docs/ as well as tests/.
COPY --chown=entity . .
CMD ["/home/entity/venv/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
