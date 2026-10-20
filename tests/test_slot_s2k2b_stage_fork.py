"""S1 session 2, fix round 2 (lane K2b): the staging reader through the REAL fork (CONTRACT §5.4 step 2).

codex r1 g2 1 / comp 5 / VM run X4: production hands ``stage_copy`` the staging dir as a root-held dirfd, and
``fork_run_as`` closed every fd but the pipe before the reader ran, so every real ``render`` refused
``staging-open-failed`` (EBADF). The unit suite used ``thread_run_as`` and never saw it. These run the real
``fork_run_as`` as the suite's own uid with a no-op drop seam (the drop itself is a VM row)."""
from __future__ import annotations

import errno
import functools
import hashlib
import os

import pytest

from levain.slot import Refused, stage as st

UID, GID = os.getuid() or 501, os.getgid() or 20
BUNDLE = b"# v2 git bundle\n" + b"0123456789abcdef0123456789abcdef01234567 refs/heads/main\n\n" + os.urandom(3000)


def _noop_drop(uid, gid):
    return None


def _fork(drop=_noop_drop):
    return functools.partial(st.fork_run_as, drop=drop)


@pytest.fixture()
def dirs(tmp_path):
    staging, dest = tmp_path / "staging", tmp_path / "in"
    staging.mkdir()
    dest.mkdir()
    (staging / st.BUNDLE_NAME).write_bytes(BUNDLE)
    return staging, dest


def _stage(staging, dest, run_as):
    return st.stage_bundle(staging, dest, staging_uid=UID, staging_gid=GID, dest_gid=GID, run_as=run_as,
                           chown=lambda fd, u, g: None)


def test_real_fork_reads_the_bundle_through_the_root_held_staging_dirfd(dirs):
    """The production form: root opens the staging DIR and hands its fd over; the forked reader opens the name
    under it. Red before the fix (``staging-open-failed``: the child had closed the dirfd)."""
    staging, dest = dirs
    dfd = os.open(staging, st._O_DIR)
    try:
        res = _stage(dfd, dest, _fork())
    finally:
        os.close(dfd)
    assert (dest / st.BUNDLE_NAME).read_bytes() == BUNDLE
    assert res == st.StageResult(len(BUNDLE), "sha256:" + hashlib.sha256(BUNDLE).hexdigest())


def test_real_fork_reads_the_bundle_by_path(dirs):
    staging, dest = dirs
    assert _stage(staging, dest, _fork()).size == len(BUNDLE)
    assert (dest / st.BUNDLE_NAME).read_bytes() == BUNDLE


def test_the_child_keeps_only_the_pipe_and_the_staging_dirfd(dirs, tmp_path):
    """In the child, at the drop: the staging dirfd is open and an unrelated fd root held is closed. A drop that
    sees otherwise raises, so the child exits 70 having written nothing and root refuses ``staging-reader-failed``."""
    staging, dest = dirs
    canary = os.open(tmp_path / "canary", os.O_WRONLY | os.O_CREAT, 0o600)
    dfd = os.open(staging, st._O_DIR)

    def checking_drop(uid, gid):
        os.fstat(dfd)                                # still open: raises EBADF if the child closed it
        try:
            os.fstat(canary)
        except OSError as e:
            if e.errno == errno.EBADF:
                return
        raise RuntimeError("an fd outside keep survived the fork")

    try:
        assert _stage(dfd, dest, _fork(checking_drop)).size == len(BUNDLE)
    finally:
        os.close(dfd)
        os.close(canary)


def test_a_drop_that_fails_in_the_child_reads_nothing(dirs):
    staging, dest = dirs

    def failing_drop(uid, gid):
        raise Refused("drop-unproven", "the test's drop")

    dfd = os.open(staging, st._O_DIR)
    try:
        with pytest.raises(Refused) as ei:
            _stage(dfd, dest, _fork(failing_drop))
    finally:
        os.close(dfd)
    assert ei.value.name == "staging-reader-failed"
    assert not (dest / st.BUNDLE_NAME).exists()
