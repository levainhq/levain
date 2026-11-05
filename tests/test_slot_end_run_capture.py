"""1105-10 endfix (finding a): END over a tree holding START's own vendor capture (c7.15, ``<S>/run/vendor.out`` and
``vendor.err``, root : root 0700 / 0600, made by :func:`levain.slot.start.open_capture`).

The capture is deliberately unreadable to every session uid, and END's manifest step refuses
``unowned-unreadable`` for any non-session-owned file no session uid can read (codex r2 N2). Before this fix the pair
made every executor session that passed the clone check end HELD at ``manifest``. END now hashes those two files as
root, and ONLY those: every other non-session-owned file it cannot read through a session uid still refuses.

Same world as test_slot_s2c_end (real files, the real Control.cas, fake freeze / kill / proof). Not root, so "root" is
this test's uid (``root_uid=UID``) and the session is a uid that owns nothing (``UID + 4242``), as in test_slot_s2k3_end.
The session-uid reader is stage's in-process one, except that it answers ``link-unreadable`` inside a listed
directory: the real slot uid gets EACCES there (``run/`` is 0700 and not its own)."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.slot import end as end_mod
from levain.slot import runtime_helper as H
from levain.slot import start as SS
from levain.slot.end import HELD, RETAINED, REVOKED, end_session
from tests.test_slot_s2c_end import DARWIN, SESSION, UID, Control, World, thread_run_as

X = UID + 4242   # the session's uid: it owns nothing in these trees
OUT_BYTES = b"stream-json line 1\n" * 7
ERR_BYTES = b"vendor stderr: a warning\n"


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def capture(w: World, out: bytes = OUT_BYTES, err: bytes = ERR_BYTES) -> list:
    """The REAL ``open_capture`` on the world's ``<S>``, then the two logs written through its file fds, as the
    helper's drain writes them. Returns the chown calls it asked for (not root: recorded, not performed)."""
    chowns: list = []
    seams = SimpleNamespace(L=w.L, host="darwin" if DARWIN else "linux", trusted=lambda st: True,
                            chown=lambda fd, uid, gid, rel: chowns.append((rel, uid, gid)), fsync=os.fsync)
    store = H.Store(seams)
    ctx = H.Ctx(seams=seams, store=store, sudo_uid=0, caller="test", operator_uid=0, env={})
    cap = SS.VendorCapture()
    try:
        SS.open_capture(ctx, SimpleNamespace(session=SESSION), cap)
        os.write(cap.out_f, out)
        os.write(cap.err_f, err)
    finally:
        cap.close()
        store.close()
    return chowns


def blind_in(*dirs: Path):
    """The session-uid reader, with the slot uid's EACCES on anything directly inside ``dirs``."""
    keys = {(st.st_dev, st.st_ino) for st in (os.lstat(d) for d in dirs)}
    inner = thread_run_as()
    answer = end_mod.LINK_UNREADABLE.encode("ascii")

    def run_as(uid, gid, fn, wfd, keep=()):
        st = os.fstat(keep[0])
        if (st.st_dev, st.st_ino) in keys:
            os.write(wfd, b"R" + bytes([len(answer)]) + answer)
            os.close(wfd)
            return _Done()
        return inner(uid, gid, fn, wfd, keep)
    return run_as


class _Done:
    def kill(self) -> None:
        pass

    def wait(self) -> int:
        return 0


def world(tmp_path: Path) -> World:
    w = World(tmp_path, var_folders=False, uchg=False)
    w.facts = replace(w.facts, slot_uid=X, slot_gid=X)
    return w


def by_path(w: World) -> dict:
    return {base64.b64decode(m["path_b64"]): m for m in w.manifest_file()}


def end(w: World, *blind: Path, **kw):
    ctl = Control()
    got = end_session(w.facts, w.seams(ctl, run_as=blind_in(*blind), **kw), record_state=REVOKED)
    return got, ctl


def held_unowned_unreadable(w: World, got, ctl, path: bytes) -> bool:
    return (got == HELD and ctl.failed_step == "manifest"
            and any("unowned-unreadable" in n and path.decode() in n for n in w.notes)
            and not w.facts.tree.exists() and w.S.is_dir())


# --- the capture START made is proven -----------------------------------------------------------------------------


def test_end_over_the_real_open_capture_proves_the_manifest_and_hashes_both_logs(tmp_path):
    w = world(tmp_path)
    chowns = capture(w)
    run = w.S / SS.RUN_DIR
    assert {r for r, _, _ in chowns} == {f"live/{SESSION}/run", f"live/{SESSION}/run/vendor.out",
                                         f"live/{SESSION}/run/vendor.err"}
    assert all((u, g) == (0, 0) for _, u, g in chowns)                     # what open_capture asked root for
    assert stat.S_IMODE(os.lstat(run).st_mode) == 0o700
    got, ctl = end(w, run)
    assert got == RETAINED, w.notes
    m = by_path(w)
    assert m[b"run/vendor.out"]["size"] == len(OUT_BYTES) and m[b"run/vendor.out"]["digest"] == _sha(OUT_BYTES)
    assert m[b"run/vendor.err"]["size"] == len(ERR_BYTES) and m[b"run/vendor.err"]["digest"] == _sha(ERR_BYTES)
    assert (w.facts.tree / "run" / "vendor.err").read_bytes() == ERR_BYTES  # the bytes stay in the tree
    assert not any("unowned-unreadable" in n for n in w.notes)


def test_an_empty_capture_is_proven_with_the_empty_files_digest(tmp_path):
    w = world(tmp_path)
    capture(w, out=b"", err=b"")
    got, _ = end(w, w.S / SS.RUN_DIR)
    assert got == RETAINED, w.notes
    assert by_path(w)[b"run/vendor.err"]["digest"] == _sha(b"")             # never UNREAD's ""


# --- everything else a session uid cannot read still holds --------------------------------------------------------


def test_another_root_file_in_the_run_dir_still_refuses(tmp_path):
    w = world(tmp_path)
    capture(w)
    run = w.S / SS.RUN_DIR
    (run / "other.log").write_bytes(b"not a capture\n")
    os.chmod(run / "other.log", 0o600)
    got, ctl = end(w, run)
    assert held_unowned_unreadable(w, got, ctl, b"run/other.log")


def test_a_capture_named_file_below_the_top_level_still_refuses(tmp_path):
    w = world(tmp_path)
    deep = w.S / "repo" / "run"
    deep.mkdir()
    (deep / "vendor.err").write_bytes(b"planted\n")
    os.chmod(deep / "vendor.err", 0o600)
    os.chmod(deep, 0o700)
    got, ctl = end(w, deep)
    assert held_unowned_unreadable(w, got, ctl, b"repo/run/vendor.err")


@pytest.mark.parametrize("mode", [0o730, 0o703, 0o750, 0o1700])
def test_a_run_dir_another_uid_may_write_or_enter_still_refuses(tmp_path, mode):
    w = world(tmp_path)
    capture(w)
    run = w.S / SS.RUN_DIR
    os.chmod(run, mode)
    got, ctl = end(w, run)
    assert held_unowned_unreadable(w, got, ctl, b"run/vendor.err")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o4600])
def test_a_capture_file_another_uid_may_read_or_a_setid_one_still_refuses(tmp_path, mode):
    w = world(tmp_path)
    capture(w)
    run = w.S / SS.RUN_DIR
    os.chmod(run / "vendor.err", mode)
    got, ctl = end(w, run)
    assert held_unowned_unreadable(w, got, ctl, b"run/vendor.err")


def test_a_multi_link_capture_still_refuses(tmp_path):
    w = world(tmp_path)
    capture(w)
    run = w.S / SS.RUN_DIR
    os.unlink(run / "vendor.out")
    os.link(run / "vendor.err", run / "vendor.out")                         # both names in the tree: nlink 2
    got, ctl = end(w, run)
    assert held_unowned_unreadable(w, got, ctl, b"run/vendor.err")


def test_a_capture_whose_dir_the_slot_owns_still_refuses(tmp_path, monkeypatch):
    """``run/`` owned by the slot uid (it swapped in a directory of its own) holding a root-owned file: not START's
    capture. Not root, so the dir's owner is what ``fstat`` reports for it."""
    w = world(tmp_path)
    capture(w)
    run = w.S / SS.RUN_DIR
    key = (os.lstat(run).st_dev, os.lstat(run).st_ino)
    real = os.fstat

    def fstat(fd):
        st = real(fd)
        if (st.st_dev, st.st_ino) == key:
            f = list(st)
            f[stat.ST_UID] = X
            return os.stat_result(f)
        return st
    monkeypatch.setattr(os, "fstat", fstat)
    got, ctl = end(w, run)
    assert held_unowned_unreadable(w, got, ctl, b"run/vendor.err")


def test_a_capture_root_does_not_own_still_refuses(tmp_path):
    """The capture files' owner is not root (here root is another uid than the files' owner): the session uid's
    refusal stands, so the exemption keys on root's ownership, not on the names."""
    w = world(tmp_path)
    capture(w)
    got, ctl = end(w, w.S / SS.RUN_DIR, root_uid=UID + 1)
    assert held_unowned_unreadable(w, got, ctl, b"run/vendor.err")


def test_a_capture_swapped_after_the_walk_is_refused_not_hashed(tmp_path, monkeypatch):
    """The fd END hashes is the inode the walk classified: a capture replaced between the walk's lstat and the open
    is ``tree-changed``, never root's read of the new file."""
    w = world(tmp_path)
    capture(w)
    run = w.S / SS.RUN_DIR
    swapped = []
    real_open = os.open

    def opener(path, flags, *a, dir_fd=None, **k):
        if path == b"vendor.err" and dir_fd is not None and not swapped and flags & os.O_NOFOLLOW:
            st = os.fstat(dir_fd)
            if (st.st_dev, st.st_ino) == (os.lstat(run).st_dev, os.lstat(run).st_ino):
                swapped.append(1)
                os.rename(run / "vendor.err", run / "old.err")
                fd = real_open(run / "vendor.err", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, b"swapped in\n")
                os.close(fd)
        return real_open(path, flags, *a, dir_fd=dir_fd, **k)
    monkeypatch.setattr(os, "open", opener)
    got, ctl = end(w, run)
    assert swapped and got == HELD and ctl.failed_step == "manifest"
    assert any("tree-changed" in n for n in w.notes), w.notes


def test_a_symlink_or_fifo_named_as_a_capture_is_recorded_as_itself_never_read(tmp_path):
    """No new read: a link's record is its target bytes (readlink, as for every link) and a fifo has no bytes; root
    never opens through either. (These never reached the ``unowned-unreadable`` path, before or after the fix.)"""
    w = world(tmp_path)
    capture(w)
    run = w.S / SS.RUN_DIR
    os.unlink(run / "vendor.err")
    os.symlink("vendor.out", run / "vendor.err")
    os.unlink(run / "vendor.out")
    os.mkfifo(run / "vendor.out", 0o600)
    got, ctl = end(w, run)
    assert got == RETAINED, w.notes
    m = by_path(w)
    assert m[b"run/vendor.err"]["type"] == "link" and m[b"run/vendor.err"]["digest"] == _sha(b"vendor.out")
    assert m[b"run/vendor.out"]["type"] == "fifo" and m[b"run/vendor.out"]["digest"] == ""


# --- the predicate itself -------------------------------------------------------------------------------------------


def _st(mode: int, uid: int = 0, nlink: int = 1) -> os.stat_result:
    return os.stat_result((mode, 1, 1, nlink, uid, 0, 0, 0, 0, 0))


@pytest.mark.parametrize("dir_st,file_st,ok", [
    (_st(stat.S_IFDIR | 0o700), _st(stat.S_IFREG | 0o600), True),
    (_st(stat.S_IFDIR | 0o700, uid=X), _st(stat.S_IFREG | 0o600), False),       # slot-owned dir
    (_st(stat.S_IFDIR | 0o770), _st(stat.S_IFREG | 0o600), False),              # group may write
    (_st(stat.S_IFDIR | 0o700), _st(stat.S_IFREG | 0o600, uid=X), False),       # slot-owned file
    (_st(stat.S_IFDIR | 0o700), _st(stat.S_IFREG | 0o600, nlink=2), False),     # a second link
    (_st(stat.S_IFDIR | 0o700), _st(stat.S_IFLNK | 0o600), False),              # a symlink
    (_st(stat.S_IFDIR | 0o700), _st(stat.S_IFIFO | 0o600), False),              # not regular
    (_st(stat.S_IFDIR | 0o700), _st(stat.S_IFREG | 0o2600), False),             # setgid
    (_st(stat.S_IFLNK | 0o700), _st(stat.S_IFREG | 0o600), False),              # the dir is a link
])
def test_start_capture_predicate(dir_st, file_st, ok):
    assert end_mod.start_capture_ok(dir_st, file_st, 0) is ok


def test_start_and_end_share_the_capture_names():
    assert SS.RUN_DIR == end_mod.CAPTURE_DIR
    assert (SS.VENDOR_OUT, SS.VENDOR_ERR) == end_mod.CAPTURE_LOGS


# --- endfix2 H1: END never hashes a capture START is still draining into ------------------------------------------
#
# In production START's helper drains the vendor's pipes (runtime_helper.wait_child_capturing) OUTSIDE the session
# lock, so END can run while it does: END kills the vendor, and START then reads what was left in the pipes, fsyncs
# and closes. The tests above close the capture before END; these keep START's file fds open, as the drain does.

REST_OUT = b"stream-json line 8 (drained after the kill)\n" * 3
REST_ERR = b"vendor stderr: the last line\n"


def live_capture(w: World, out: bytes = OUT_BYTES, err: bytes = ERR_BYTES):
    """The REAL ``open_capture`` with a prefix written and the capture left OPEN (START mid-drain). Returns the
    capture and the store; the caller closes both."""
    seams = SimpleNamespace(L=w.L, host="darwin" if DARWIN else "linux", trusted=lambda st: True,
                            chown=lambda fd, uid, gid, rel: None, fsync=os.fsync)
    store = H.Store(seams)
    ctx = H.Ctx(seams=seams, store=store, sudo_uid=0, caller="test", operator_uid=0, env={})
    cap = SS.VendorCapture()
    SS.open_capture(ctx, SimpleNamespace(session=SESSION), cap)
    os.write(cap.out_f, out)
    os.write(cap.err_f, err)
    return cap, store


def test_end_waits_for_starts_final_drain_and_hashes_the_whole_capture(tmp_path):
    """START's last drain lands while END waits: the manifest describes the bytes the retained tree holds. The drain
    thread writes the rest when END first sleeps (it is waiting) or after END returned (it did not wait)."""
    w = world(tmp_path)
    cap, store = live_capture(w)
    go = threading.Event()

    def drain():
        go.wait(20)
        os.write(cap.out_f, REST_OUT)
        os.write(cap.err_f, REST_ERR)
        os.fsync(cap.out_f)
        os.fsync(cap.err_f)
        cap.close()

    def sleep(s):
        go.set()
        time.sleep(min(s, 0.01))
    t = threading.Thread(target=drain)
    t.start()
    try:
        got, ctl = end(w, w.S / SS.RUN_DIR, sleep=sleep)
    finally:
        go.set()
        t.join(20)
        cap.close()
        store.close()
    assert got == RETAINED, w.notes
    m = by_path(w)
    kept_out = (w.facts.tree / "run" / "vendor.out").read_bytes()
    kept_err = (w.facts.tree / "run" / "vendor.err").read_bytes()
    assert kept_out == OUT_BYTES + REST_OUT and kept_err == ERR_BYTES + REST_ERR
    assert m[b"run/vendor.out"]["size"] == len(kept_out) and m[b"run/vendor.out"]["digest"] == _sha(kept_out)
    assert m[b"run/vendor.err"]["size"] == len(kept_err) and m[b"run/vendor.err"]["digest"] == _sha(kept_err)


def test_a_capture_start_never_closes_ends_held_capture_busy_never_hashed(tmp_path):
    """START's helper hung mid-drain (or stopped): END waits its bound, then HELD at ``manifest`` naming
    ``capture-busy``; no tree is retained and nothing describes the half-written file."""
    w = world(tmp_path)
    cap, store = live_capture(w)
    now = [1000.0]

    def sleep(s):
        now[0] += s
    try:
        got, ctl = end(w, w.S / SS.RUN_DIR, clock=lambda: now[0], sleep=sleep)
    finally:
        cap.close()
        store.close()
    assert got == HELD and ctl.failed_step == "manifest", w.notes
    assert any("capture-busy" in n and "vendor." in n for n in w.notes), w.notes
    assert not w.facts.tree.exists() and w.S.is_dir()
    assert now[0] - 1000.0 >= end_mod.CAPTURE_WAIT_S                      # it did wait its bound first


def test_open_capture_holds_the_capture_lock_until_close(tmp_path):
    """The writer side: from creation to close START's fds hold an exclusive flock on each log, so another open file
    description (END's) cannot take a shared one until START closes."""
    w = world(tmp_path)
    cap, store = live_capture(w)
    paths = [w.S / SS.RUN_DIR / n for n in end_mod.CAPTURE_LOGS]

    def shared_free(p: Path) -> bool:
        fd = os.open(p, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)
    try:
        assert [shared_free(p) for p in paths] == [False, False]
    finally:
        cap.close()
        store.close()
    assert [shared_free(p) for p in paths] == [True, True]


# --- endfix2 M1: the capture's size is bounded by START's own cap, and END proves one that size ----------------------


def test_a_capture_at_starts_log_cap_is_hashed_whole(tmp_path):
    """START writes at most ``VENDOR_LOG_CAP`` bytes per log (the rest is counted and dropped), so that is the largest
    capture END meets. END proves one of that size per log under the default caps: no size rule of END's turns a
    full log back into a refusal."""
    w = world(tmp_path)
    big = (b"x" * 4095 + b"\n") * (H.VENDOR_LOG_CAP // 4096)
    assert len(big) == H.VENDOR_LOG_CAP
    capture(w, out=big, err=big)
    got, _ = end(w, w.S / SS.RUN_DIR)
    assert got == RETAINED, w.notes
    m = by_path(w)
    for name in (b"run/vendor.out", b"run/vendor.err"):
        assert m[name]["size"] == H.VENDOR_LOG_CAP and m[name]["digest"] == _sha(big)
