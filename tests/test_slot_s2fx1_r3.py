"""S1 session 2, fix round 3, lane FX1: the S2_L3_r2 findings in control.py, runtime_helper.py and start.py
(triage ``S2_L3_r2_TRIAGE.md`` Lane FX1: g1-N1, g1-N2, g1-O2, g1-L1, g2-N1, g2-N2, g2-L1, g2-L2).

* CREATE: each ``create_at`` writer has its own temp and unlinks only its own; a failure after the link leaves the
  record EXISTING for ``prepare``, which removes it durably before any lease goes back (or keeps the slot leased).
* RESTART: CONTROL's start pass splits records by STATE alone; a record past live with a stale ``lease_live:true``
  is cleared, dropped and the reaper woken. ``_prune`` survives an unsettled record.
* START: the fd-relative remover (3.9's calls only, run on the host's 3.9 when there is one); exec = ``+`` then
  EOF inside the one bound; the failed clone's domain killed before its repo is emptied; a forwarder whose ready
  read raises is abandoned.
"""
from __future__ import annotations

import errno
import os
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from levain.slot import Refused, wall
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import start as SS
from tests.test_slot_s2e_root_imports import REPO, ROOT_PYTHON, _flags, _root_python_39
from tests.test_slot_s2f2_runtime_helper import world  # noqa: F401 - the fixture
from tests.test_slot_s2f3_start import E0, S0, _rendered, _S, _world
from tests.test_slot_s2k1_authority import S1, Box
from tests.test_slot_s2k2a_launch import _no_active, scripted


# --- g1-N1: create_at's temp is the writer's own ---------------------------------------------------------------------

def test_two_writers_of_one_name_never_touch_each_others_temp(tmp_path):
    """codex HIGH: both writers used ``name.new``; the second unlinked the first's live temp. Here writer B runs
    whole while writer A sits between its write and its link: B's record lands intact, A then meets an EXISTING
    name (``FileExistsError``, its own temp unlinked), and no temp is left."""
    d = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        def a_fsync(fd):
            os.fsync(fd)
            if not stat.S_ISDIR(os.fstat(fd).st_mode) and not b_done:
                b_done.append(True)
                H.create_at(d, "x.json", b"B", mode=0o600, fsync=os.fsync)
        b_done: list = []
        with pytest.raises(FileExistsError):
            H.create_at(d, "x.json", b"A", mode=0o600, fsync=a_fsync)
        assert b_done
        assert (tmp_path / "x.json").read_bytes() == b"B"
        assert os.listdir(tmp_path) == ["x.json"]
    finally:
        os.close(d)


def test_temp_names_are_per_writer():
    seen = set()
    real = os.open

    def spy(path, flags, mode=0o777, *, dir_fd=None):
        if isinstance(path, str) and path.endswith(".new"):
            seen.add(path)
        return real(path, flags, mode, dir_fd=dir_fd)

    import tempfile
    with tempfile.TemporaryDirectory() as t:
        d = os.open(t, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.open = spy
            try:
                for i in range(3):
                    H.create_at(d, f"n{i}.json", b"x", mode=0o600, fsync=os.fsync)
                    with pytest.raises(FileExistsError):
                        H.create_at(d, f"n{i}.json", b"y", mode=0o600, fsync=os.fsync)
            finally:
                os.open = real
        finally:
            os.close(d)
    assert len(seen) == 6


# --- g1-N2: a failure after the link -----------------------------------------------------------------------------------

def _fail_alloc_dir_fsync(world, times: int) -> list:
    """The alloc dir's fsync fails ``times`` times (counted from the first, which is create_at's after its link)."""
    ino = os.stat(world.L / "alloc").st_ino
    failed: list = []
    real = world.seams.fsync

    def fsync(fd):
        if os.fstat(fd).st_ino == ino and len(failed) < times:
            failed.append(fd)
            raise OSError(errno.EIO, "injected alloc dir fsync failure")
        real(fd)
    world.seams.fsync = fsync
    return failed


def _allocs(world) -> list:
    return [n for n in os.listdir(world.L / "alloc") if n.endswith(".json")]


def test_linked_alloc_whose_dir_fsync_fails_is_removed_before_the_lease_goes_back(world):
    """codex HIGH / complement MED: the link succeeded, the dir fsync failed, ``created`` stayed False: the lease
    went back while the alloc named the slot. Now the alloc is removed (and its removal fsynced), THEN the slot is
    reusable."""
    failed = _fail_alloc_dir_fsync(world, 1)
    line = world.prepare()
    assert not line["ok"] and len(failed) == 1
    assert _allocs(world) == []
    assert world.slot_state(E0).state == "reusable"
    assert not any(n.endswith(".new") for n in os.listdir(world.L / "alloc"))


def test_alloc_removal_not_proved_durable_keeps_the_slot_leased(world):
    """Fail closed: the undo's own dir fsync fails too, so the removal is not proved durable: the slot stays
    leased (the reaper's alloc-less orphan), never reusable under an alloc that may come back."""
    failed = _fail_alloc_dir_fsync(world, 2)
    line = world.prepare()
    assert not line["ok"] and len(failed) == 2
    assert world.slot_state(E0).state == "leased"


def test_linked_not_durable_is_an_oserror_but_not_file_exists():
    e = H.LinkedNotDurable(errno.EIO, "x")
    assert isinstance(e, OSError) and not isinstance(e, FileExistsError)


# --- g1-O2: the restart split keys off the state ---------------------------------------------------------------------

def test_restart_drops_a_revoked_record_whose_lease_live_survived(tmp_path):
    """codex OPEN: a CAS into REVOKED leaves ``lease_live:true``; CONTROL died before its after-acts. The next start
    used to route it through the live branch, clear the flag and only prune: no DROP-SESSION, no wake."""
    box = Box(tmp_path)
    op = box.attach(S1)
    rec = box.disk(S1)
    pre = replace_rec(rec, state=R.REVOKED, lease_live=True)
    box.store.put(pre)                          # as the crash left it: REVOKED, lease_live true, no after-acts
    del op
    box2 = Box(tmp_path)
    box2.control.start()
    box2.control.join_eof(5)
    after = box2.disk(S1)
    assert after.state == R.REVOKED and after.lease_live is False
    assert ("drop", S1) in box2.calls and ("wake",) in box2.calls


def replace_rec(rec: R.SessionState, **kw) -> R.SessionState:
    from dataclasses import replace
    return replace(rec, generation=rec.generation + 1, **kw)


def test_restart_still_revokes_a_live_record(tmp_path):
    box = Box(tmp_path)
    box.attach(S1)
    box2 = Box(tmp_path)
    box2.control.start()
    box2.control.join_eof(5)
    after = box2.disk(S1)
    assert after.state == R.REVOKED and after.lease_live is False
    assert box2.calls.count(("drop", S1)) == 1


# --- g1-L1: _prune on an unsettled record ----------------------------------------------------------------------------

def test_prune_survives_an_unsettled_record(tmp_path):
    box = Box(tmp_path)
    box.attach(S1)
    lease = box.control._session_lease[S1]
    lease.live = False

    def unsettled(session):
        raise Refused("record-unsettled", session)
    box.control.store.get = unsettled
    box.control._prune(S1)                      # used to raise, ending the revoke thread that called it
    assert box.control._session_lease.get(S1) is lease      # left bound (dead): a later prune unbinds it


# --- g2-N1: the fd-relative remover ----------------------------------------------------------------------------------

def _tree(root: Path, outside: Path) -> None:
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "a" / "f").write_text("x")
    (root / "a" / "b" / "c" / "g").write_text("y")
    (root / "fifo-like").write_text("z")
    os.symlink(outside, root / "a" / "to-dir")
    os.symlink(outside / "keep.txt", root / "a" / "b" / "to-file")
    os.symlink("dangling", root / "dangling")


def test_remove_tree_at_removes_everything_and_follows_no_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the session's\n")
    root = tmp_path / "S"
    root.mkdir()
    _tree(root / "repo", outside)
    sfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        SS.remove_tree_at(sfd, "repo")
    finally:
        os.close(sfd)
    assert os.listdir(root) == []
    assert (outside / "keep.txt").read_text() == "not the session's\n"


def test_remove_tree_at_on_a_link_unlinks_the_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("k")
    os.symlink(outside, tmp_path / "repo")
    sfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        SS.remove_tree_at(sfd, "repo")
    finally:
        os.close(sfd)
    assert not os.path.lexists(tmp_path / "repo") and (outside / "keep.txt").read_text() == "k"


def test_remove_tree_at_takes_a_swapped_name_as_what_it_is_now(tmp_path, monkeypatch):
    """A file listed by the walk becomes a directory before its unlink; a directory listed by the walk becomes a
    link before its open. The first is emptied and removed, the second's link unlinked: the outside is kept."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("k")
    repo = tmp_path / "repo"
    (repo / "d").mkdir(parents=True)
    (repo / "d" / "inner").write_text("i")
    (repo / "f").write_text("f")
    real_unlink, real_open = os.unlink, os.open
    swapped: list = []

    def unlink(name, *, dir_fd=None):
        if name == "f" and "f" not in swapped:
            swapped.append("f")
            real_unlink(name, dir_fd=dir_fd)
            os.mkdir("f", dir_fd=dir_fd)
            fd = real_open("f", os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)
            try:
                os.close(real_open("x", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
            finally:
                os.close(fd)
        return real_unlink(name, dir_fd=dir_fd)

    def open_(name, flags, mode=0o777, *, dir_fd=None):
        if name == "d" and "d" not in swapped:
            swapped.append("d")
            fd = real_open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)
            try:
                real_unlink("inner", dir_fd=fd)
            finally:
                os.close(fd)
            os.rmdir("d", dir_fd=dir_fd)
            os.symlink(str(outside), "d", dir_fd=dir_fd)
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(SS.os, "unlink", unlink)
    monkeypatch.setattr(SS.os, "open", open_)
    sfd = real_open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        SS.remove_tree_at(sfd, "repo")
    finally:
        os.close(sfd)
    assert sorted(swapped) == ["d", "f"]
    assert not os.path.lexists(repo)
    assert os.listdir(outside) == ["keep.txt"]


_CHILD_39 = """
import os, sys
sys.path.insert(0, {repo!r})
from levain.slot import start as SS
fd = os.open({parent!r}, os.O_RDONLY | os.O_DIRECTORY)
SS.remove_tree_at(fd, "repo")
os.close(fd)
print("ok", sys.version_info[:2])
"""


def test_remove_tree_at_runs_on_the_root_python_39(tmp_path):
    """codex MED: ``shutil.rmtree(dir_fd=)`` is 3.11+, so on macOS root's 3.9 a failed clone could never be emptied.
    The remover itself runs here under ``/usr/bin/python3`` when that is a 3.9 (root's interpreter, PY_FLAGS)."""
    why = _root_python_39()
    if why is not None:
        pytest.skip(why)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("k")
    _tree(tmp_path / "repo", outside)
    code = _CHILD_39.format(repo=str(REPO), parent=str(tmp_path))
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    r = subprocess.run([ROOT_PYTHON, *_flags(), "-c", code], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0 and r.stdout.startswith("ok (3, 9)"), r.stderr[-2000:]
    assert not os.path.lexists(tmp_path / "repo") and (outside / "keep.txt").read_text() == "k"


# --- g2-N2: the exec is ``+`` then EOF -------------------------------------------------------------------------------

def test_marker_then_no_eof_in_the_bound_is_exec_failed_never_active(env, monkeypatch):
    """codex MED: ``+`` followed by no ``!`` for 250 ms used to be the exec. A last hop that wrote ``+`` and still
    holds the pipe (descheduled before its execve, or stuck) is not an exec: the bound ends it, the domain is
    killed, and the record stays RENDERED."""
    w, la, table = env
    s = _rendered(w)
    held = scripted(w, la, vendor=b"+", hold=("vendor",))
    monkeypatch.setattr(SS, "EXEC_MARKER_S", 0.1)
    try:
        assert w.main(["start", s])[1]["refused"] == "exec-failed"
    finally:
        for fd in held:
            os.close(fd)
    (v,) = la.of("vendor")
    assert v["pid"] in la.killed and table.of(S0) == []
    assert w.state(s).state == R.RENDERED and _no_active(w, s)


def test_await_exec_is_one_bound_across_both_reads():
    """The second wait is what is left of the bound, never a fresh one: the bound is spent by the time ``+`` is read
    (the clock says so), and the EOF that comes 0.3 s later is past it."""
    r, wfd = os.pipe()
    times = iter([0.0, 0.0])

    def clock():
        return next(times, 10.0)
    os.write(wfd, wall.EXEC_OK)
    timer = threading.Timer(0.3, os.close, args=(wfd,))
    timer.start()
    try:
        assert SS.await_exec(r, bound_s=1.0, clock=clock) == "timeout"
    finally:
        timer.join()
        os.close(r)


def test_await_exec_eof_after_the_marker_arrives_late_is_still_the_exec():
    r, wfd = os.pipe()
    os.write(wfd, wall.EXEC_OK)
    timer = threading.Timer(0.3, os.close, args=(wfd,))
    timer.start()
    try:
        assert SS.await_exec(r, bound_s=5.0) == "exec"
    finally:
        timer.join()
        os.close(r)


@pytest.fixture
def env(tmp_path):
    return _world(tmp_path)


# --- g2-L1: the failed clone's domain is killed before its repo is emptied -------------------------------------------

def test_failed_clone_kills_the_domain_before_emptying_the_repo(env, monkeypatch):
    w, la, _ = env
    s = _rendered(w)
    order: list = []
    real_kill, real_fresh = SS.kill_domain, SS._fresh_repo

    def kill(ctx, a):
        order.append("kill")
        return real_kill(ctx, a)

    def fresh(ctx, a, sfd):
        order.append("fresh")
        return real_fresh(ctx, a, sfd)
    monkeypatch.setattr(SS, "kill_domain", kill)
    monkeypatch.setattr(SS, "_fresh_repo", fresh)
    la.codes["clone"] = 128
    assert w.main(["start", s])[1]["refused"] == "clone-failed"
    assert order == ["fresh", "kill", "fresh"]
    assert os.listdir(_S(w, s) / "repo") == []


def test_failed_clone_still_empties_the_repo_when_the_kill_fails(env, monkeypatch):
    w, la, _ = env
    s = _rendered(w)

    def kill(ctx, a):
        raise OSError(errno.EIO, "injected")
    monkeypatch.setattr(SS, "kill_domain", kill)
    la.codes["clone"] = 128
    assert w.main(["start", s])[1]["refused"] == "clone-failed"
    assert os.listdir(_S(w, s) / "repo") == []


# --- g2-L2: a forwarder whose ready read raises is abandoned ---------------------------------------------------------

def test_forwarder_ready_read_raising_abandons_the_forwarder(env, monkeypatch):
    w, la, table = env
    s = _rendered(w)
    real = SS._readable
    calls: list = []

    def readable(fd, bound_s):
        if not calls:
            calls.append(fd)
            raise OSError(errno.EBADF, "injected ready read failure")
        return real(fd, bound_s)
    monkeypatch.setattr(SS, "_readable", readable)
    code, line = w.main(["start", s])
    assert not line["ok"]
    (f,) = la.of("forwarder")
    assert f["pid"] in la.killed and table.of(S0) == []
    assert la.of("vendor") == [] and _no_active(w, s)
