"""keys' credential writer (design 1.8, session 3b-5a): the one commit discipline and `CredWriter`, the real
`CredLifecycle` keys' shutdown calls.

The commit runs on `CredWriter`'s one writer thread: `flock` (non-blocking, retried, bounded -> `credential-lock-timeout`),
the stale-temp sweep, the `rev` compare (mismatch or gone = DISCARD), a unique temp fsynced, the rename, the directory
fsync (a failure there is success with the warning `credential-commit-not-durable`), unlock. Every file the writer makes
is 0600 and parses as the reader parses it, so the next exchange's observation picks the new `rev` up."""
from __future__ import annotations

import asyncio
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
        assert C.remove(fd, NAME) is True
        assert C.remove(fd, NAME) is False   # already gone
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
