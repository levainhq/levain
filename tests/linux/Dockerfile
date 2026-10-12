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
#   docker run --rm levain-linux
#
# ⛔ THIS IMAGE NO LONGER RUNS THE FLOOR, AND MUST NOT BE MADE TO. Since the cgroup v2 frame (S2,
# 2026-10-09) levain refuses a container BY NAME: each command needs its own scope from a host's
# systemd user manager, and delegation into a container is not measured. The image used to require
# the live floor (`LEVAIN_REQUIRE_LINUX_LIVE=1`), which made it red on every run, so it now sets
# `LEVAIN_EXPECT_CONTAINER_REFUSAL=1` and a green run means the refusal holds
# (`test_the_linux_image_is_refused_as_a_container_by_name`). Run it WITHOUT Docker's --security-opt
# relaxations: with them bwrap starts, the `linux_live` tests stop skipping, and each one fails on
# the container refusal. The live floor runs on a host: the CI `linux-live` job's VM, or argushub.
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
    LEVAIN_EXPECT_CONTAINER_REFUSAL=1
RUN mkdir -p /tmp/xdg && python3 -m venv /home/entity/venv
COPY --chown=entity pyproject.toml README.md LICENSE NOTICE MANIFEST.in ./
COPY --chown=entity levain ./levain
RUN /home/entity/venv/bin/pip install -q -e ".[dev,openhands]"
# Then the rest of the repo: the suite reads bench/, scripts/, examples/ and docs/ as well as tests/.
COPY --chown=entity . .
CMD ["/home/entity/venv/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
