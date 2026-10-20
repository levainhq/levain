"""S1 session 2b: ``levain.slot.stage`` (CONTRACT §5.4, §5.6, V19, V24, V25) on REAL files. The reader runs on a
thread (``thread_run_as``: the suite is not root, so the drop is a VM row) and the destination ``fchown`` is a
recorded no-op."""
from __future__ import annotations

import hashlib
import os
import stat
import struct
import subprocess
import threading

import pytest

from levain.contracts import canon
from levain.slot import Refused, write_all
from levain.slot import stage as st

UID, GID = os.getuid() or 501, os.getgid() or 20
BUNDLE = b"# v2 git bundle\n" + b"0123456789abcdef0123456789abcdef01234567 refs/heads/main\n\n" + os.urandom(3000)


@pytest.fixture()
def dirs(tmp_path):
    staging = tmp_path / "staging"
    dest = tmp_path / "in"
    staging.mkdir()
    dest.mkdir()
    return staging, dest


def seams(**kw):
    chowned = kw.pop("chowned", [])
    base = dict(run_as=st.thread_run_as, chown=lambda fd, u, g: chowned.append((u, g)))
    base.update(kw)
    return base


def copy(staging, dest, data=None, *, name="repo.bundle", **kw):
    if data is not None:
        (staging / name).write_bytes(data)
    return st.stage_bundle(staging, dest, staging_uid=UID, staging_gid=GID, dest_gid=1234, **seams(**kw))


def refused(fn, *a, **kw) -> Refused:
    with pytest.raises(Refused) as ei:
        fn(*a, **kw)
    return ei.value


def _finished() -> st.Reader:
    t = threading.Thread(target=lambda: None)
    t.start()
    return st._Threaded(t, threading.Event())


def sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def test_copy_happy_path(dirs):
    staging, dest = dirs
    chowned: list = []
    r = copy(staging, dest, BUNDLE, chowned=chowned)
    out = dest / "repo.bundle"
    assert out.read_bytes() == BUNDLE and (r.size, r.digest) == (len(BUNDLE), sha(BUNDLE))
    assert stat.S_IMODE(out.stat().st_mode) == 0o640
    assert chowned == [(0, 1234)]                     # root : the slot group (§2.1)
    v3 = b"# v3 git bundle\n" + b"x" * 10
    (dest / "repo.bundle").unlink()
    assert copy(staging, dest, v3).size == len(v3)


def test_bundle_header_refused(dirs):
    staging, dest = dirs
    for bad in (b"# v4 git bundle\n" + b"x" * 100, b"PK\x03\x04" + b"x" * 100, b"# v2 git bundle\r" + b"x",
                b"# v2 git bundl"):
        e = refused(copy, staging, dest, bad)
        assert e.name == "bundle-header", bad
        assert not (dest / "repo.bundle").exists()      # the fresh destination is unlinked


def test_bundle_cap_refused(dirs, monkeypatch):
    staging, dest = dirs
    monkeypatch.setattr(st, "BUNDLE_CAP", len(BUNDLE) - 1)
    assert refused(copy, staging, dest, BUNDLE).name == "bundle-cap"
    assert not (dest / "repo.bundle").exists()
    monkeypatch.setattr(st, "BUNDLE_CAP", len(BUNDLE))
    assert copy(staging, dest, BUNDLE).size == len(BUNDLE)
    monkeypatch.undo()
    assert st.BUNDLE_CAP == 8 * 1024 ** 3                # V19


def test_root_rechecks_cap_the_reader_sent(dirs):
    """A reader that lies about the size (says more than the cap) is refused by root, not trusted."""
    staging, dest = dirs

    def liar(uid, gid, fn, wfd):
        write_all(wfd, b"S" + struct.pack(">Q", st.BUNDLE_CAP + 1))
        os.close(wfd)
        return _finished()

    assert refused(copy, staging, dest, BUNDLE, run_as=liar).name == "bundle-cap"


def test_bundle_copy_deadline(dirs):
    staging, dest = dirs
    ticks = iter([0.0] + [st.STAGE_DEADLINE_S + 1.0] * 1000)
    e = refused(copy, staging, dest, BUNDLE, clock=lambda: next(ticks))
    assert e.name == "staging-raced"
    assert not (dest / "repo.bundle").exists()
    assert st.STAGE_DEADLINE_S == 600                    # V24


def test_stalled_reader_hits_deadline(dirs):
    """A reader that sends the header and then stalls: root gives up at the deadline and unlinks."""
    staging, dest = dirs
    stop = threading.Event()

    def stall(uid, gid, fn, wfd):
        def body():
            write_all(wfd, b"S" + struct.pack(">Q", len(BUNDLE)) + BUNDLE[:100])
            stop.wait(10)
            os.close(wfd)
        t = threading.Thread(target=body, daemon=True)
        t.start()
        return st._Threaded(t, stop)

    now = [0.0]

    def clock():
        now[0] += 0.2
        return now[0]

    e = refused(copy, staging, dest, BUNDLE, run_as=stall, clock=clock, deadline_s=1.0, poll_slice=0.01)
    assert e.name == "staging-raced"
    assert not (dest / "repo.bundle").exists()


def test_fifo_refused_without_hanging(dirs):
    staging, dest = dirs
    os.mkfifo(staging / "repo.bundle")
    box: dict = {}

    def run():
        try:
            copy(staging, dest)
        except Refused as e:
            box["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "a FIFO hung stage_copy"
    assert box["e"].name == "staging-not-regular"
    assert not (dest / "repo.bundle").exists()


def test_symlink_and_dir_refused(dirs, tmp_path):
    staging, dest = dirs
    target = tmp_path / "operator_file"
    target.write_bytes(BUNDLE)
    before = target.stat()
    os.symlink(target, staging / "repo.bundle")
    assert refused(copy, staging, dest).name == "staging-not-regular"
    after = target.stat()
    assert (after.st_mode, after.st_uid, after.st_size) == (before.st_mode, before.st_uid, before.st_size)
    os.unlink(staging / "repo.bundle")
    (staging / "repo.bundle").mkdir()
    assert refused(copy, staging, dest).name == "staging-not-regular"
    os.rmdir(staging / "repo.bundle")
    assert refused(copy, staging, dest).name == "staging-missing"
    assert list(dest.iterdir()) == []


def test_hardlink_copies_bytes_never_touches_target(dirs, tmp_path):
    """A planted hardlink yields the lease peer's own bytes and nothing else: the target is only read."""
    staging, dest = dirs
    target = tmp_path / "linked"
    target.write_bytes(BUNDLE)
    os.link(target, staging / "repo.bundle")
    before = target.stat()
    copy(staging, dest)
    after = target.stat()
    assert (after.st_mode, after.st_uid, after.st_gid, after.st_nlink) == \
        (before.st_mode, before.st_uid, before.st_gid, before.st_nlink)
    assert (dest / "repo.bundle").stat().st_ino != target.stat().st_ino


def _fstat_hook(monkeypatch, path, act):
    real = os.fstat
    ino = path.stat().st_ino
    done = []

    def hooked(fd):
        s = real(fd)
        if not done and stat.S_ISREG(s.st_mode) and s.st_ino == ino:
            done.append(1)
            act()
        return s

    monkeypatch.setattr(st.os, "fstat", hooked)


def test_short_file_after_fstat_raced(dirs, monkeypatch):
    staging, dest = dirs
    p = staging / "repo.bundle"
    p.write_bytes(BUNDLE)
    _fstat_hook(monkeypatch, p, lambda: os.truncate(p, 500))
    assert refused(copy, staging, dest).name == "staging-raced"
    assert not (dest / "repo.bundle").exists()


def test_grown_after_fstat_raced(dirs, monkeypatch):
    staging, dest = dirs
    p = staging / "repo.bundle"
    p.write_bytes(BUNDLE)

    def grow():
        with open(p, "ab") as f:
            f.write(b"!")

    _fstat_hook(monkeypatch, p, grow)
    assert refused(copy, staging, dest).name == "staging-raced"
    assert not (dest / "repo.bundle").exists()


def test_shrunk_below_header_after_fstat_raced(dirs, monkeypatch):
    staging, dest = dirs
    p = staging / "repo.bundle"
    p.write_bytes(BUNDLE)
    _fstat_hook(monkeypatch, p, lambda: os.truncate(p, 4))
    assert refused(copy, staging, dest).name == "staging-raced"


def test_input_bundle_mismatch_refused(dirs):
    staging, dest = dirs
    e = refused(copy, staging, dest, BUNDLE, input_bundle_digest=sha(BUNDLE + b"x"))
    assert e.name == "input-bundle-mismatch"
    assert not (dest / "repo.bundle").exists()
    assert copy(staging, dest, input_bundle_digest=sha(BUNDLE)).digest == sha(BUNDLE)
    assert refused(copy, staging, dest, input_bundle_digest="sha256:XYZ").name == "bad-digest"


def _driver(staging, dest, data, digest, **kw):
    if data is not None:
        (staging / "driver_input.json").write_bytes(data)
    return st.stage_driver_input(staging, dest, staging_uid=UID, staging_gid=GID, dest_gid=1234,
                                 driver_input_digest=digest, **seams(**kw))


def test_driver_input_digest_mismatch_refused(dirs, monkeypatch):
    staging, dest = dirs
    good = canon.canonical_bytes({"artifact": "x", "n": 3, "deep": [[[["ok"]]]]})
    e = refused(_driver, staging, dest, good, sha(good + b" "))
    assert e.name == "driver-input-mismatch" and not (dest / "input.json").exists()
    r = _driver(staging, dest, None, sha(good))
    assert (dest / "input.json").read_bytes() == good and r.digest == sha(good)
    assert stat.S_IMODE((dest / "input.json").stat().st_mode) == 0o440      # §5.6
    (dest / "input.json").unlink()
    for bad in (b'{"a":1.5}', b'{"a": 1}', b"[" * 65 + b"]" * 65, b"not json"):
        e = refused(_driver, staging, dest, bad, sha(bad))      # the digest matches; the content is refused
        assert e.name == "driver-input-mismatch", bad
        assert not (dest / "input.json").exists()
    deep = b"[" * 64 + b"]" * 64
    assert _driver(staging, dest, deep, sha(deep)).size == 128
    (dest / "input.json").unlink()
    monkeypatch.setattr(st, "DRIVER_INPUT_CAP", 10)
    assert refused(_driver, staging, dest, good, sha(good)).name == "driver-input-mismatch"
    monkeypatch.undo()
    assert st.DRIVER_INPUT_CAP == 256 * 1024             # V25
    assert refused(_driver, staging, dest, good, None).name == "driver-input-mismatch"


def test_destination_is_fresh(dirs):
    staging, dest = dirs
    (dest / "repo.bundle").write_bytes(b"theirs")
    assert refused(copy, staging, dest, BUNDLE).name == "staging-dest-exists"
    assert (dest / "repo.bundle").read_bytes() == b"theirs"     # never written over, never unlinked
    os.unlink(dest / "repo.bundle")
    os.symlink("/etc/passwd", dest / "repo.bundle")
    assert refused(copy, staging, dest).name == "staging-dest-exists"
    assert os.path.islink(dest / "repo.bundle")


def test_reader_refusal_names_allowlisted(dirs):
    staging, dest = dirs

    def bogus(uid, gid, fn, wfd):
        write_all(wfd, b"R\x05hello")
        os.close(wfd)
        return _finished()

    assert refused(copy, staging, dest, BUNDLE, run_as=bogus).name == "staging-reader-failed"

    def silent(uid, gid, fn, wfd):
        os.close(wfd)
        return _finished()

    assert refused(copy, staging, dest, BUNDLE, run_as=silent).name == "staging-reader-failed"


def test_reader_runs_as_staging_uid(dirs):
    """The read is the reader's: root's side passes the lease peer's ids to run_as and opens nothing in staging."""
    staging, dest = dirs
    seen = []

    def record(uid, gid, fn, wfd):
        seen.append((uid, gid))
        return st.thread_run_as(uid, gid, fn, wfd)

    copy(staging, dest, BUNDLE, run_as=record)
    assert seen == [(UID, GID)]
    for bad in (0,):
        e = refused(st.stage_bundle, staging, dest, staging_uid=bad, staging_gid=GID, dest_gid=1, **seams())
        assert e.name == "staging-uid-root"


def test_drop_refuses_root_target():
    with pytest.raises(PermissionError):
        st.drop_and_assert(0, 20)
    with pytest.raises(PermissionError):
        st.drop_and_assert(501, 0)


def test_reader_opens_nofollow_nonblock_noctty():
    for flag in ("O_NOFOLLOW", "O_NONBLOCK", "O_NOCTTY"):
        assert st._O_READ & getattr(os, flag)
    assert st._O_DEST & os.O_EXCL and st._O_DEST & os.O_CREAT and st._O_DEST & os.O_NOFOLLOW


# --- make_bundle -> a real git clone ---------------------------------------------------------------------------

def _git(*a, cwd=None):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def test_make_bundle_clones_head(tmp_path, dirs):
    staging, dest = dirs
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", "-b", "work", str(src))
    (src / "a.txt").write_text("committed\n")
    _git("add", "a.txt", cwd=src)
    _git("commit", "-q", "-m", "one", cwd=src)
    _git("remote", "add", "origin", "https://user:secret@example.invalid/x.git", cwd=src)
    (src / "untracked.txt").write_text("never leaves\n")
    (src / "a.txt").write_text("uncommitted edit\n")
    hooks = src / ".git" / "hooks"
    marker = tmp_path / "hook_ran"
    (hooks / "post-checkout").write_text(f"#!/bin/sh\ntouch {marker}\n")
    os.chmod(hooks / "post-checkout", 0o755)
    out = staging / "repo.bundle"
    st.make_bundle(src, "work", out)
    assert out.read_bytes()[:16] in st.BUNDLE_HEADERS
    heads = _git("bundle", "list-heads", str(out))
    assert "refs/heads/work" in heads and "HEAD" in heads
    assert "secret" not in out.read_bytes().decode("latin-1")
    # the copy path takes it, then a real clone of the copy checks out HEAD
    copy(staging, dest)
    clone = tmp_path / "clone"
    _git("-c", "core.hooksPath=/dev/null", "clone", "-q", "--template=", str(dest / "repo.bundle"), str(clone))
    assert (clone / "a.txt").read_text() == "committed\n"
    assert not (clone / "untracked.txt").exists()
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone).strip() == "work"
    assert not marker.exists()
    assert refused(st.make_bundle, src, "-x", staging / "b2").name == "bad-branch"


def test_make_bundle_runs_no_hook_of_the_callers_config(tmp_path, monkeypatch):
    """The operator's own global ``core.hooksPath`` (a reference-transaction hook fires on the clone's ref writes)
    never runs: ``-c core.hooksPath=/dev/null`` on every git call."""
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", "-b", "main", str(src))
    (src / "f").write_text("x\n")
    _git("add", "f", cwd=src)
    _git("commit", "-q", "-m", "c", cwd=src)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "ran"
    for h in ("reference-transaction", "post-checkout", "post-merge"):
        (hooks / h).write_text(f"#!/bin/sh\necho {h} >> {marker}\n")
        os.chmod(hooks / h, 0o755)
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[core]\n\thooksPath = {hooks}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    _git("-C", str(src), "update-ref", "refs/heads/probe", "HEAD")     # positive control: the hook does run
    assert marker.exists()
    marker.unlink()
    st.make_bundle(src, "main", tmp_path / "b.bundle")
    assert not marker.exists()
    assert (tmp_path / "b.bundle").read_bytes()[:16] in st.BUNDLE_HEADERS
    assert refused(st.make_bundle, src, "a..b", tmp_path / "b2").name == "bad-branch"
    assert refused(st.make_bundle, src, "nope", tmp_path / "b2").name == "bundle-failed"
    assert not (tmp_path / "b2").exists()
