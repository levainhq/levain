"""keys' credential writer (design 1.8, session 3b-5a): the one commit discipline and `CredWriter`, the real
`CredLifecycle` keys' shutdown calls.

The commit runs on `CredWriter`'s one writer thread: `flock` (non-blocking, retried, bounded -> `credential-lock-timeout`),
the stale-temp sweep, the `rev` compare (mismatch or gone = DISCARD), a unique temp fsynced, the rename, the directory
fsync (a failure there is success with the warning `credential-commit-not-durable`), unlock. Every file the writer makes
is 0600 and parses as the reader parses it, so the next exchange's observation picks the new `rev` up."""
from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import threading
import time

import pytest

from levain.broker import creds as C
from levain.broker.norm import Refused

NAME = "anthropic"


def _fields(secret: str = "sk-test-one", **over: object) -> dict:
    d = {"format": C.FORMAT, "name": NAME, "hosts": ["api.anthropic.com"],
         "inject": {"header": "x-api-key", "scheme": ""}, "kind": "static", "secret": secret}
    d.update(over)
    return d


def _refresh_fields() -> dict:
    return {"format": C.FORMAT, "name": NAME, "hosts": ["api.example.com"],
            "inject": {"header": "authorization", "scheme": "Bearer"}, "kind": "oauth2-refresh",
            "access": "at-1", "expires_at": 1, "refresh": {"token_url": "https://api.example.com/token",
                                                           "client_id": "cid", "refresh_token": "rt-1"}}


@pytest.fixture()
def cred_dir(tmp_path):
    d = tmp_path / "cred"
    d.mkdir(mode=0o700)
    return d


def _dir_fd(path) -> int:
    return os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)


def _commit_now(cred_dir, fields, expect_rev=None, **kw):
    fd = _dir_fd(cred_dir)
    try:
        return C.commit(fd, NAME, fields, expect_rev=expect_rev, **kw)
    finally:
        os.close(fd)


# --- render: the writer writes only what the reader parses ---------------------------------------------------------

def test_render_writes_a_fresh_rev_the_reader_parses():
    raw1, rev1 = C.render(_fields(), NAME)
    raw2, rev2 = C.render(_fields(), NAME)
    assert C.REV_RE.fullmatch(rev1) and rev1 != rev2
    assert C.parse(raw1, NAME).rev == rev1 and C.parse(raw1, NAME).secret == "sk-test-one"
    assert C.parse(raw2, NAME).rev == rev2


@pytest.mark.parametrize("bad, field", [
    (dict(_fields(), rev="0" * 32), "rev"),
    (dict(_fields(), hosts=["API.example.com"]), "hosts"),
    (dict(_refresh_fields(), refresh={"token_url": "https://elsewhere.example.com/t", "client_id": "c",
                                      "refresh_token": "r"}), "refresh.token_url host"),
    (dict(_fields(), secret="x" * (C.VALUE_CAP + 1)), "secret"),
    (dict(_fields(), name="other"), "name"),
])
def test_render_refuses_what_the_reader_would_refuse(bad, field):
    with pytest.raises(C.Unparseable) as e:
        C.render(bad, NAME)
    assert str(e.value) == field


# --- the commit, step by step --------------------------------------------------------------------------------------

def test_commit_writes_0600_and_the_observation_picks_up_the_new_rev(cred_dir):
    out = _commit_now(cred_dir, _fields("sk-first"))
    assert out.result == C.COMMITTED and out.durable
    st = os.stat(cred_dir / f"{NAME}.json")
    assert stat.S_IMODE(st.st_mode) == 0o600
    store = C.CredStore(_dir_fd(cred_dir))
    first = store.observe(NAME)
    assert first.rev == out.rev and first.secret == "sk-first"
    out2 = _commit_now(cred_dir, _fields("sk-second"))
    second = store.observe(NAME)   # the next exchange's observation: the new rev, the new secret
    assert second.rev == out2.rev != first.rev and second.secret == "sk-second"
    assert sorted(p.name for p in cred_dir.iterdir()) == [".lock", f"{NAME}.json"]   # no temp left
    assert stat.S_IMODE(os.stat(cred_dir / ".lock").st_mode) == 0o600


def test_rev_compare_mismatch_discards_and_the_file_is_kept(cred_dir):
    first = _commit_now(cred_dir, _fields("sk-operator"))
    out = _commit_now(cred_dir, _fields("sk-refreshed"), expect_rev="f" * 32)
    assert out.result == C.DISCARDED and out.rev is None
    assert C.parse((cred_dir / f"{NAME}.json").read_bytes(), NAME).rev == first.rev


def test_rev_compare_on_a_removed_file_discards_and_it_stays_removed(cred_dir):
    first = _commit_now(cred_dir, _fields())
    fd = _dir_fd(cred_dir)
    try:
        assert C.remove(fd, NAME).result == C.REMOVED
        assert C.remove(fd, NAME).result == C.ABSENT   # already gone
    finally:
        os.close(fd)
    out = _commit_now(cred_dir, _fields("sk-late"), expect_rev=first.rev)
    assert out.result == C.DISCARDED
    assert not (cred_dir / f"{NAME}.json").exists()


def test_rev_compare_match_commits(cred_dir):
    first = _commit_now(cred_dir, _fields())
    out = _commit_now(cred_dir, _fields("sk-next"), expect_rev=first.rev)
    assert out.result == C.COMMITTED and out.rev != first.rev


def test_a_directory_fsync_failure_after_the_rename_is_success_with_a_warning(cred_dir, caplog):
    def bad_dir_sync(fd: int) -> None:
        raise OSError(5, "injected")

    seams = C.WriteSeams(fsync_dir=bad_dir_sync)
    with caplog.at_level(logging.WARNING, logger="levain.broker"):
        out = _commit_now(cred_dir, _fields("sk-new-tokens"), seams=seams)
    assert out.result == C.COMMITTED and out.durable is False
    assert any("credential-commit-not-durable" in r.getMessage() for r in caplog.records)
    store = C.CredStore(_dir_fd(cred_dir))
    assert store.observe(NAME).secret == "sk-new-tokens"   # the new tokens are used


def test_a_temp_fsync_failure_is_a_failure_with_the_old_file_intact(cred_dir):
    first = _commit_now(cred_dir, _fields("sk-old"))

    def bad_file_sync(fd: int) -> None:
        raise OSError(5, "injected")

    with pytest.raises(C.CommitFailed) as e:
        _commit_now(cred_dir, _fields("sk-new"), seams=C.WriteSeams(fsync_file=bad_file_sync))
    assert e.value.name == "credential-commit-failed"
    assert C.parse((cred_dir / f"{NAME}.json").read_bytes(), NAME).rev == first.rev
    assert sorted(p.name for p in cred_dir.iterdir()) == [".lock", f"{NAME}.json"]


def test_a_rename_failure_is_a_failure_with_the_temp_unlinked(cred_dir):
    first = _commit_now(cred_dir, _fields("sk-old"))

    def bad_rename(src: str, dst: str, dir_fd: int) -> None:
        raise OSError(28, "injected")

    with pytest.raises(C.CommitFailed):
        _commit_now(cred_dir, _fields("sk-new"), seams=C.WriteSeams(rename=bad_rename))
    assert C.parse((cred_dir / f"{NAME}.json").read_bytes(), NAME).rev == first.rev
    assert sorted(p.name for p in cred_dir.iterdir()) == [".lock", f"{NAME}.json"]


def test_whoever_holds_the_lock_sweeps_stale_temps_only(cred_dir):
    stale = cred_dir / f".{NAME}.111.abcdef01.tmp"
    fresh = cred_dir / f".{NAME}.222.abcdef02.tmp"
    other = cred_dir / "notes.tmp"
    for p in (stale, fresh, other):
        p.write_bytes(b"x")
    old = time.time() - C.STALE_TMP_S - 5
    os.utime(stale, (old, old))
    os.utime(other, (old, old))
    _commit_now(cred_dir, _fields())
    assert not stale.exists() and fresh.exists() and other.exists()


def test_the_lock_is_refused_unless_a_regular_file(cred_dir):
    os.mkfifo(str(cred_dir / ".lock"), 0o600)
    with pytest.raises(C.CommitFailed):
        _commit_now(cred_dir, _fields())


_HOLDER = r"""
import fcntl, os, signal, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
sys.stdout.write("locked\n"); sys.stdout.flush()
os.kill(os.getpid(), signal.SIGSTOP)
"""


def _stopped_holder(cred_dir) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(cred_dir / ".lock")], stdout=subprocess.PIPE)
    assert p.stdout is not None and p.stdout.readline() == b"locked\n"
    return p


def test_a_lock_held_by_a_stopped_child_is_credential_lock_timeout(cred_dir):
    holder = _stopped_holder(cred_dir)
    try:
        t0 = time.monotonic()
        with pytest.raises(C.CommitFailed) as e:
            _commit_now(cred_dir, _fields(), lock_timeout_s=0.4)
        took = time.monotonic() - t0
    finally:
        holder.kill()
        holder.wait()
    assert e.value.name == "credential-lock-timeout"
    assert 0.35 <= took < 2.0
    assert not (cred_dir / f"{NAME}.json").exists()   # nothing written
    out = _commit_now(cred_dir, _fields())             # the lock is free again: the next commit lands
    assert out.result == C.COMMITTED


def test_the_lock_timeout_constants_are_the_designs():
    assert C.LOCK_TIMEOUT_S == 10.0 and C.LOCK_RETRY_S == 0.05 and C.STALE_TMP_S == 60.0


# --- CredWriter: the writer thread and the CredLifecycle shell ------------------------------------------------------

def test_credwriter_is_the_seam_and_its_shell_starts_empty(cred_dir):
    w = C.CredWriter(str(cred_dir))
    try:
        seam = {m for m in vars(C.CredLifecycle) if not m.startswith("_")}
        assert seam == {"begin_closing", "cancel_detached"}      # the seam is exactly these two (R86)
        for m in seam:
            assert callable(getattr(w, m))
        assert w._closing is False and w._refresh_tasks == set()
        w.cancel_detached()           # the empty set: returns
        w.begin_closing()
        assert w._closing is True
        w.begin_closing()             # idempotent
        assert w._closing is True
    finally:
        w.close()


def test_cancel_detached_cancels_the_members_of_the_set(cred_dir):
    async def go():
        w = C.CredWriter(str(cred_dir))
        try:
            t = asyncio.ensure_future(asyncio.sleep(30))
            w._refresh_tasks.add(t)
            w.cancel_detached()
            with pytest.raises(asyncio.CancelledError):
                await t
        finally:
            w.close()
    asyncio.run(go())


def test_credwriter_commits_on_its_one_writer_thread_never_the_loop(cred_dir):
    seen = []

    def sync(fd: int) -> None:
        seen.append(threading.current_thread().name)

    async def go():
        w = C.CredWriter(str(cred_dir), seams=C.WriteSeams(fsync_file=sync, fsync_dir=sync))
        try:
            outs = await asyncio.gather(*(w.commit(NAME, _fields(f"sk-{i}")) for i in range(4)))
        finally:
            w.close()
        return outs, threading.current_thread().name

    outs, loop_thread = asyncio.run(go())
    assert all(o.result == C.COMMITTED for o in outs) and len({o.rev for o in outs}) == 4
    assert len(set(seen)) == 1 and seen[0] != loop_thread and seen[0].startswith("levain-cred-writer")
    revs = [o.rev for o in outs]
    assert C.parse((cred_dir / f"{NAME}.json").read_bytes(), NAME).rev == revs[-1]   # serialized, in order


def test_credwriter_lock_timeout_surfaces_and_the_thread_is_free_after(cred_dir):
    holder = _stopped_holder(cred_dir)

    async def go():
        w = C.CredWriter(str(cred_dir))
        w.lock_timeout_s = 0.3
        try:
            with pytest.raises(C.CommitFailed) as e:
                await w.commit(NAME, _fields())
            assert e.value.name == "credential-lock-timeout"
            holder.kill()
            holder.wait()
            return await w.commit(NAME, _fields("sk-after"))
        finally:
            w.close()

    try:
        out = asyncio.run(go())
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
    assert out.result == C.COMMITTED


def test_credwriter_without_a_credential_directory_fails_the_commit(tmp_path):
    async def go():
        w = C.CredWriter(str(tmp_path / "absent"))
        try:
            await w.commit(NAME, _fields())
        finally:
            w.close()
    with pytest.raises(C.CommitFailed) as e:
        asyncio.run(go())
    assert e.value.name == "credential-commit-failed"


def test_credwriter_construction_starts_no_thread_and_opens_nothing(cred_dir):
    before = {t.name for t in threading.enumerate()}
    w = C.CredWriter(str(cred_dir))
    try:
        assert {t.name for t in threading.enumerate()} == before
        assert w._dir_fd is None
    finally:
        w.close()


def test_the_store_still_refuses_a_missing_credential(cred_dir):
    with pytest.raises(Refused):
        C.CredStore(_dir_fd(cred_dir)).observe(NAME)


# --- keys wires the real writer ------------------------------------------------------------------------------------

def test_keys_main_builds_the_real_credwriter_as_cred_lifecycle(monkeypatch, tmp_path):
    from levain.broker import keys as K

    seen = {}
    real = K.Keys

    class Spy(real):   # type: ignore[misc, valid-type]
        def __init__(self, **kw):
            super().__init__(**kw)
            seen["lifecycle"] = self.cred_lifecycle

    async def fake_serve(keys, *a):
        keys.trigger()
        await keys.wait_done()
        return 0

    monkeypatch.setattr(K, "Keys", Spy)
    monkeypatch.setattr(K, "_serve", fake_serve)
    monkeypatch.setattr(K, "install_log", lambda lib_root: None)
    monkeypatch.setattr(K, "raise_nofile", lambda: None)
    rc = K.main(["--runtime-root", str(tmp_path), "--lib-root", str(tmp_path), "--control", str(tmp_path / "c"),
                 "--test-single-uid"])
    assert rc == 0
    w = seen["lifecycle"]
    assert type(w) is C.CredWriter
    assert w.cred_dir == os.path.join(str(tmp_path), "keys", "cred")
    assert w._closing is True                 # the latch called begin_closing on the real writer
    assert w._executor._shutdown is True      # main() shut its executor at exit


def test_keys_reader_does_not_follow_a_linked_cred_dir(tmp_path):
    # HEAD_1022-12 H7: design 1.8's "every path below a dirfd of L/keys/cred opened O_DIRECTORY|O_NOFOLLOW" holds for
    # the reader's own dirfd too: a `cred` that is a link is refused, never followed.
    from levain.broker import keys as K

    real = tmp_path / "elsewhere"
    real.mkdir(mode=0o700)
    (real / f"{NAME}.json").write_bytes(C.render(_fields(), NAME)[0])
    (tmp_path / "keys").mkdir(mode=0o700)
    (tmp_path / "keys" / "cred").symlink_to(real)
    k = K.Keys(unmetered=True, lib_root=str(tmp_path))
    with pytest.raises(Refused) as e:
        k._cred_store()
    assert e.value.name == "credential-unavailable"
    assert k.creds is None
    os.unlink(tmp_path / "keys" / "cred")
    os.rename(real, tmp_path / "keys" / "cred")
    store = k._cred_store()
    try:
        assert store.observe(NAME).secret == "sk-test-one"
    finally:
        os.close(store.dir_fd)


# --- L3 r1 fixes (1022-12 3b5a, P1-P7) ------------------------------------------------------------------------------

def _eio(fd):
    raise OSError(errno.EIO, "EIO")


def test_p1_a_remove_whose_directory_fsync_fails_says_not_durable_and_logs_it(cred_dir, caplog):
    _commit_now(cred_dir, _fields())
    fd = _dir_fd(cred_dir)
    try:
        with caplog.at_level(logging.WARNING):
            out = C.remove(fd, NAME, seams=C.WriteSeams(fsync_dir=_eio))
    finally:
        os.close(fd)
    assert (out.result, out.durable) == (C.REMOVED, False)
    assert not (cred_dir / f"{NAME}.json").exists()
    assert "credential-remove-not-durable" in caplog.text


def test_p1_a_remove_of_an_absent_file_still_fsyncs_the_directory(cred_dir):
    # an ENOENT retry of a remove whose fsync failed: the unlink is in the directory but may not be on the medium
    synced = []
    fd = _dir_fd(cred_dir)
    try:
        out = C.remove(fd, NAME, seams=C.WriteSeams(fsync_dir=synced.append))
        assert (out.result, out.durable) == (C.ABSENT, True)
        assert synced == [fd]
        bad = C.remove(fd, NAME, seams=C.WriteSeams(fsync_dir=_eio))
        assert (bad.result, bad.durable) == (C.ABSENT, False)
    finally:
        os.close(fd)


def test_p2_no_flock_attempt_is_made_past_the_deadline(cred_dir):
    # the thread is descheduled in its retry sleep; meanwhile the holder lets go and the clock passes the deadline.
    # The next step must be the deadline check, not an flock that would commit arbitrarily late.
    holder = os.open(str(cred_dir / C.LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    now = [1000.0]

    def descheduled(s):
        fcntl.flock(holder, fcntl.LOCK_UN)
        now[0] += C.LOCK_TIMEOUT_S + 1

    try:
        with pytest.raises(C.CommitFailed) as e:
            _commit_now(cred_dir, _fields(), seams=C.WriteSeams(monotonic=lambda: now[0], sleep=descheduled))
    finally:
        os.close(holder)
    assert e.value.name == "credential-lock-timeout"
    assert not (cred_dir / f"{NAME}.json").exists()


def test_p2_each_retry_sleeps_at_most_what_remains(cred_dir):
    holder = os.open(str(cred_dir / C.LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    now, slept = [1000.0], []

    def sleep(s):
        slept.append(s)
        now[0] += s

    bound = C.LOCK_RETRY_S * 2.5
    try:
        with pytest.raises(C.CommitFailed):
            _commit_now(cred_dir, _fields(), lock_timeout_s=bound,
                        seams=C.WriteSeams(monotonic=lambda: now[0], sleep=sleep))
    finally:
        os.close(holder)
    assert max(slept) <= C.LOCK_RETRY_S
    assert sum(slept) == pytest.approx(bound)          # never past the bound
    assert slept[-1] == pytest.approx(bound - 2 * C.LOCK_RETRY_S)


def _listdir_fails_on_fds(monkeypatch):
    real = os.listdir

    def listdir(p="."):
        if isinstance(p, int):
            raise OSError(errno.EIO, "EIO")
        return real(p)
    monkeypatch.setattr(os, "listdir", listdir)


def test_p3_a_sweep_that_cannot_list_skips_and_the_commit_and_remove_go_on(cred_dir, monkeypatch, caplog):
    _listdir_fails_on_fds(monkeypatch)
    with caplog.at_level(logging.WARNING):
        out = _commit_now(cred_dir, _fields())
        assert out.result == C.COMMITTED
        fd = _dir_fd(cred_dir)
        try:
            assert C.remove(fd, NAME).result == C.REMOVED
        finally:
            os.close(fd)
    assert "credential-temp-sweep-skipped" in caplog.text


def test_p3_a_skipped_sweep_does_not_hide_a_commit_failure(cred_dir, monkeypatch):
    _listdir_fails_on_fds(monkeypatch)

    def bad_rename(src, dst, dir_fd):
        raise OSError(errno.EIO, "EIO")
    with pytest.raises(C.CommitFailed) as e:
        _commit_now(cred_dir, _fields(), seams=C.WriteSeams(rename=bad_rename))
    assert e.value.name == "credential-commit-failed"


def _fullfsync_refused(monkeypatch):
    """darwin's path with an F_FULLFSYNC the filesystem refuses (SMB/NFS/FUSE answer ENOTSUP); os.fsync recorded."""
    import fcntl as F
    full = getattr(F, "F_FULLFSYNC", 51)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(F, "F_FULLFSYNC", full, raising=False)
    real_fcntl, real_fsync, synced = F.fcntl, os.fsync, []

    def fcntl_(fd, cmd, *a):
        if cmd == full:
            raise OSError(errno.ENOTSUP, "ENOTSUP")
        return real_fcntl(fd, cmd, *a)

    def fsync(fd):
        synced.append(fd)
        return real_fsync(fd)
    monkeypatch.setattr(F, "fcntl", fcntl_)
    monkeypatch.setattr(os, "fsync", fsync)
    return synced


def test_p4_full_fsync_falls_back_to_fsync_as_dir_fsync_does(cred_dir, monkeypatch):
    synced = _fullfsync_refused(monkeypatch)
    fd = os.open(str(cred_dir / "probe"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        C.full_fsync(fd)
        C.dir_fsync(fd)
    finally:
        os.close(fd)
    assert synced == [fd, fd]
    out = _commit_now(cred_dir, _fields())           # the real seams: the file's fsync no longer fails the commit
    assert (out.result, out.durable) == (C.COMMITTED, True)


def test_p5_a_commit_holds_at_most_commit_fds_beyond_the_counted_ones(cred_dir):
    from levain.broker import keys as K

    seen = []

    def fsync_file(fd):
        seen.append(K.count_open_fds())
        os.fsync(fd)

    # a writer whose directory did not exist when keys counted: its first commit opens the directory too
    lazy = C.CredWriter(str(cred_dir), seams=C.WriteSeams(fsync_file=fsync_file))
    try:
        base = K.count_open_fds()
        lazy._commit_sync(NAME, _fields(), None)
        assert seen[-1] - base == C.COMMIT_FDS
    finally:
        lazy.close()
    # a writer opened before the count: the directory is a fixed fd, the commit's own are the rest
    w = C.CredWriter(str(cred_dir), seams=C.WriteSeams(fsync_file=fsync_file))
    try:
        before = K.count_open_fds()
        assert w.open_dir() is True
        base = K.count_open_fds()
        assert base - before == 1
        w._commit_sync(NAME, _fields("sk-two"), None)
        assert seen[-1] - base == C.COMMIT_FDS - 1
        assert K.count_open_fds() == base               # nothing of the commit's is kept
    finally:
        w.close()
    assert C.COMMIT_FDS <= K.TRANSIENT_NAMED


def test_p5_keys_main_opens_the_writers_directory_before_serve_counts(monkeypatch, tmp_path):
    from levain.broker import keys as K

    (tmp_path / "keys" / "cred").mkdir(parents=True, mode=0o700)
    seen = {}

    async def fake_serve(keys, *a):
        seen["dir_fd"] = keys.cred_lifecycle._dir_fd    # what _serve's count_open_fds would see
        seen["reader"] = keys.creds
        keys.trigger()
        await keys.wait_done()
        return 0

    monkeypatch.setattr(K, "_serve", fake_serve)
    monkeypatch.setattr(K, "install_log", lambda lib_root: None)
    monkeypatch.setattr(K, "raise_nofile", lambda: None)
    rc = K.main(["--runtime-root", str(tmp_path), "--lib-root", str(tmp_path), "--control", str(tmp_path / "c"),
                 "--test-single-uid"])
    assert rc == 0 and seen["dir_fd"] is not None
    assert seen["reader"] is not None                  # the reader's directory too (the item flagged after P5)


def test_the_readers_lazy_directory_is_one_fd_and_open_cred_store_is_best_effort(tmp_path, caplog):
    from levain.broker import keys as K

    k = K.Keys(unmetered=True, lib_root=str(tmp_path))
    with caplog.at_level(logging.WARNING):
        assert k.open_cred_store() is False and k.creds is None     # no cred/ yet: silent, nothing opened
    assert "not open at start" not in caplog.text
    (tmp_path / "keys" / "cred").mkdir(parents=True, mode=0o700)
    base = K.count_open_fds()
    store = k._cred_store()                            # the lazy case TRANSIENT_NAMED names: exactly one fd
    try:
        assert K.count_open_fds() - base == 1
        assert k.open_cred_store() is True and k.creds is store     # already open: nothing more
        assert K.count_open_fds() - base == 1
    finally:
        os.close(store.dir_fd)


def test_p5_open_dir_without_a_credential_directory_leaves_it_to_the_first_commit(tmp_path):
    w = C.CredWriter(str(tmp_path / "absent"))
    try:
        assert w.open_dir() is False and w._dir_fd is None
    finally:
        w.close()


def _unlock_fails(monkeypatch):
    real = fcntl.flock

    def flock(fd, op):
        if op == fcntl.LOCK_UN:
            raise OSError(errno.EIO, "EIO")
        return real(fd, op)
    monkeypatch.setattr(fcntl, "flock", flock)


def test_p6_an_unlock_failure_never_replaces_the_outcome(cred_dir, monkeypatch):
    from levain.broker import keys as K

    _unlock_fails(monkeypatch)
    base = K.count_open_fds()
    out = _commit_now(cred_dir, _fields())
    assert out.result == C.COMMITTED
    assert K.count_open_fds() == base                    # the lock fd was closed: closing released the flock

    def bad_rename(src, dst, dir_fd):
        raise OSError(errno.EIO, "EIO")
    with pytest.raises(C.CommitFailed) as e:
        _commit_now(cred_dir, _fields("sk-two"), seams=C.WriteSeams(rename=bad_rename))
    assert e.value.name == "credential-commit-failed"
    monkeypatch.undo()
    assert _commit_now(cred_dir, _fields("sk-three"), lock_timeout_s=0.2).result == C.COMMITTED


def test_p7_the_sweep_matches_exactly_the_generated_temp_names(cred_dir):
    assert C._TMP_RE.fullmatch(C._tmp_name(NAME))
    never_ours = [f".{NAME}.7.a.tmp", f".{NAME}.07.abcdef01.tmp", f".{NAME}.0.abcdef01.tmp",
                  f".{NAME}.7.abcdef012.tmp", f".{NAME}.9.ABCDEF09.tmp", f".{NAME}.-7.abcdef01.tmp"]
    # (the upper-case case gets its own pid and hex: on a case-insensitive APFS it would otherwise BE `ours`)
    ours = cred_dir / f".{NAME}.7.abcdef01.tmp"
    old = time.time() - C.STALE_TMP_S - 5
    for n in never_ours + [ours.name]:
        (cred_dir / n).write_bytes(b"x")
        os.utime(cred_dir / n, (old, old))
    _commit_now(cred_dir, _fields())
    assert not ours.exists()
    assert all((cred_dir / n).exists() for n in never_ours)
