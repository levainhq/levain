"""The reservation ledger's open and replay (C1_DESIGN r4 §4, §2.6 "The flush", §3): torn tail, every corruption rule
by name and line, every I/O stage, the directory flush on every open (T8), the epoch (T9), the lock (T10), the strict
flush (T25) and the stdlib-only tripwire (T17)."""
from __future__ import annotations

import ast
import asyncio
import errno
import fcntl
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from levain.broker import creds
from levain.broker import ledger as L
from tests.ledger_support import (AMOUNT, CAP, E1, E2, LID, SCOPE, FlushCtl, Owner, file_lines, header, kinds,
                                  ledger_path, line, open_fds, reserved, until, write_file)

ROOT = Path(__file__).resolve().parents[1]


def _refusal(path) -> L.LedgerOpenError:
    with pytest.raises(L.LedgerOpenError) as ei:
        L.Ledger.open(path)
    return ei.value


def _close(led) -> None:
    asyncio.run(led.aclose(0))


def opened(seq, epoch=E1):
    return line("opened", seq, epoch=epoch)


def released(seq, ref):
    return line("released", seq, ref=ref)


# ---- T8: torn tail, empty, corrupt, newer ------------------------------------------------------------------------

def test_torn_tail_truncated_and_flushed(tmp_path, monkeypatch):
    path = ledger_path(tmp_path)
    good = header() + opened(1) + reserved(2)
    write_file(path, good, b'{"kind":"released","seq":3,"re')
    calls = []
    real = L._flush
    monkeypatch.setattr(L, "_flush", lambda fd: (calls.append(stat.S_ISDIR(os.fstat(fd).st_mode)), real(fd))[1])
    led = L.Ledger.open(path)
    assert calls == [False, True, False]            # flush-truncate, flush-dir, flush-opened
    with open(path, "rb") as f:
        assert f.read().startswith(good)
    assert kinds(path) == ["header", "opened", "reserved", "opened", "uncertain"]
    assert led.charged(SCOPE) == AMOUNT and led.totals(SCOPE).uncertain_restart == AMOUNT
    _close(led)


@pytest.mark.parametrize("content", [b"", b'{"kind":"hea'])
def test_empty_file_recreated(tmp_path, content):
    path = ledger_path(tmp_path)
    write_file(path, content)
    led = L.Ledger.open(path)
    lines = file_lines(path)
    assert [o["kind"] for o in lines] == ["header", "opened"]
    assert lines[0]["ledger_id"] == led.ledger_id and len(led.ledger_id) == 32
    _close(led)


def _meta_over():
    return {"k": "x" * L.META_MAX}


CORRUPT = [
    ("json", [header(), b"{not json\n"], 2),
    ("json", [header(), b'{"at":"2026-10-23T01:02:03Z","epoch":"' + E1.encode() + b'","kind":"opened","seq":1.5}\n'], 2),
    ("json", [header(), b'{"at":"2026-10-23T01:02:03Z","epoch":"' + E1.encode()
              + b'","kind":"opened","kind":"opened","seq":1}\n'], 2),
    ("json", [header(), b"\x00\x00\x00\x00\n"], 2),
    ("canonical", [header(), b'{"at": "2026-10-23T01:02:03Z","epoch":"' + E1.encode() + b'","kind":"opened","seq":1}\n'], 2),
    ("canonical", [header(), b'{"seq":1,"at":"2026-10-23T01:02:03Z","epoch":"' + E1.encode() + b'","kind":"opened"}\n'], 2),
    ("canonical", [header(), b'{"at":"2026-10-23T01:02:03Z","epoch":"\\u0061' + E1[1:].encode()
                   + b'","kind":"opened","seq":1}\n'], 2),
    ("header", [opened(0)], 1),
    ("header", [header(), opened(1), header(2)], 3),
    ("seq", [header(), opened(0)], 2),
    ("seq", [header(), opened(2), reserved(2)], 3),
    ("seq", [line("header", 1, format=L.FORMAT, ledger_id=LID, created_at="2026-10-23T01:02:03Z")], 1),
    ("key", [header(), line("opened", 1, epoch=E1, x=1)], 2),
    ("key", [header(), line("bogus", 1)], 2),
    ("key", [header(), L._encode({"kind": "opened", "seq": 1, "epoch": E1})], 2),
    ("key", [header(), b"[1]\n"], 2),
    ("type", [header(), opened(1, epoch="A" * 32)], 2),
    ("type", [header(), opened(1), line("reserved", 2, epoch=E1, scope=["Session", "s"], cap=1, amount=1,
                                        price_ref="r", meta={})], 3),
    ("type", [header(), opened(1), line("reserved", 2, epoch=E1, scope=list(SCOPE), cap=True, amount=1,
                                        price_ref="r", meta={})], 3),
    ("type", [header(), opened(1), line("reserved", 2, epoch=E1, scope=list(SCOPE), cap=1, amount=1,
                                        price_ref="a\nb", meta={})], 3),
    ("type", [header(), opened(1), reserved(2, meta=_meta_over())], 3),
    ("type", [header(), opened(1), reserved(2), line("uncertain", 3, ref=2, reason="Bad", meta={})], 4),
    ("type", [header(), line("opened", 1, epoch=E1, at="yesterday")], 2),
    ("type", [line("header", 0, format=L.FORMAT, ledger_id="c" * 31, created_at="2026-10-23T01:02:03Z")], 1),
    ("type", [line("header", 0, format="levain-reservation-ledger/1", ledger_id=LID,
                   created_at="2026-10-23T01:02:03Z")], 1),
    ("epoch", [header(), opened(1, E1), reserved(2, epoch=E2)], 3),
    ("epoch", [header(), reserved(1)], 2),
    ("epoch", [header(), opened(1, E1), opened(2, E2), reserved(3, epoch=E1)], 4),
    ("epoch-dup", [header(), opened(1, E1), opened(2, E1)], 3),
    ("orphan", [header(), opened(1), released(2, 99)], 3),
    ("orphan", [header(), opened(1), released(2, 1)], 3),
    ("orphan", [header(), opened(1), released(2, 3), reserved(3)], 3),       # the earlier-ref form
    ("double", [header(), opened(1), reserved(2), released(3, 2), released(4, 2)], 5),
    ("double", [header(), opened(1), reserved(2), released(3, 2),
                line("uncertain", 4, ref=2, reason="restart", meta={})], 5),
    ("amount", [header(), opened(1), reserved(2, amount=0)], 3),
    ("amount", [header(), opened(1), reserved(2, cap=-1)], 3),
    ("amount", [header(), opened(1), reserved(2), line("settled", 3, ref=2, settled=AMOUNT + 1, overrun=0,
                                                       meta={})], 4),
    ("amount", [header(), opened(1), reserved(2), line("settled", 3, ref=2, settled=1, overrun=-1, meta={})], 4),
]


@pytest.mark.parametrize("what,lines,n", CORRUPT, ids=["%s-%d" % (c[0], i) for i, c in enumerate(CORRUPT)])
def test_corrupt_refuses(tmp_path, what, lines, n):
    path = ledger_path(tmp_path)
    write_file(path, *lines)
    before = open(path, "rb").read()
    fds = open_fds()
    e = _refusal(path)
    assert e.name == "ledger-corrupt:%s:%d" % (what, n)
    assert open_fds() == fds
    assert open(path, "rb").read() == before                    # a refused file is never written


@pytest.mark.parametrize("extra", [[], [b"garbage that would be corrupt\n"]])
def test_format_newer_refused(tmp_path, extra):
    path = ledger_path(tmp_path)
    write_file(path, line("header", 0, format="levain-reservation-ledger/3", ledger_id=LID,
                          created_at="2026-10-23T01:02:03Z", lineage=True), *extra)
    assert _refusal(path).name == "ledger-format-newer"


def _newer_header() -> bytes:
    return line("header", 0, format="levain-reservation-ledger/3", ledger_id=LID, created_at="2026-10-23T01:02:03Z")


def _digest(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.mark.parametrize("name,parts", [
    ("ledger-format-newer", [_newer_header()[:-12]]),                              # a torn newer-format first line
    ("ledger-format-newer", [_newer_header(), b'{"kind":"opened","se']),             # a newer header, then a torn tail
    ("ledger-corrupt:json:2", [header(), b"{not json\n", b'{"kind":"opened","se']),  # a corrupt line, then a torn tail
], ids=["torn-newer-first-line", "newer-header-torn-tail", "corrupt-torn-tail"])
def test_a_refused_torn_file_is_never_written(tmp_path, name, parts):
    path = ledger_path(tmp_path)
    write_file(path, *parts)
    before = _digest(path)
    assert _refusal(path).name == name
    assert _digest(path) == before                  # no truncation, no header rewrite before the checks pass


def test_replay_rebuilds_every_term(tmp_path):
    path = ledger_path(tmp_path)
    other = ("session-route", "s1", "r-b")
    write_file(path, header(), opened(1), reserved(2), reserved(3), reserved(4), reserved(5), reserved(6, scope=other),
               released(7, 2), line("settled", 8, ref=3, settled=AMOUNT, overrun=25, meta={}),
               line("uncertain", 9, ref=4, reason="ended", meta={}), opened(10, E2))
    led = L.Ledger.open(path)
    t = led.totals(SCOPE)
    assert (t.settled, t.overrun, t.uncertain_other, t.uncertain_restart, t.outstanding) == (100, 25, 100, 100, 0)
    assert led.charged(SCOPE) == 325 and led.charged(other) == AMOUNT
    new = file_lines(path)[11:]
    assert [(o["kind"], o.get("ref")) for o in new] == [("opened", None), ("uncertain", 5), ("uncertain", 6)]
    assert [o["seq"] for o in new] == [11, 12, 13] and led._next_seq == 14
    _close(led)


# ---- T8: I/O stages -----------------------------------------------------------------------------------------------

def _boom(err=errno.EIO):
    def f(*a, **k):
        raise OSError(err, os.strerror(err))
    return f


def _file_boom(real):
    """A `_flush` that fails on a regular file only: the directory flush (`_dir_flush` -> `_flush`) still passes."""
    def f(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        real(fd)
    return f


def test_io_open_refuses(tmp_path):
    path = ledger_path(tmp_path)
    assert _refusal(str(tmp_path / "missing" / "ledger.jsonl")).name == "ledger-io:open:ENOENT"
    os.symlink(str(tmp_path / "elsewhere"), path)
    assert _refusal(path).name == "ledger-io:open:ELOOP"
    os.unlink(path)
    write_file(path, header())
    if os.geteuid() != 0:
        os.chmod(path, 0)
        assert _refusal(path).name == "ledger-io:open:EACCES"


@pytest.mark.parametrize("stage", ["read", "truncate", "write-header", "write-opened", "flush-header",
                                   "flush-truncate", "flush-dir", "flush-opened", "dir-open", "dir-close"])
def test_io_stage_refuses(tmp_path, monkeypatch, stage):
    path = ledger_path(tmp_path)
    if stage in ("read", "write-opened", "flush-dir", "flush-opened", "dir-open", "dir-close"):
        write_file(path, header())
    if stage in ("truncate", "flush-truncate"):
        write_file(path, header(), b'{"torn')
    name = {"read": "read", "truncate": "truncate", "write-header": "write", "write-opened": "write",
            "dir-open": "open", "dir-close": "open"}.get(stage, stage)
    if stage == "read":
        monkeypatch.setattr(os, "pread", _boom())
    elif stage == "truncate":
        monkeypatch.setattr(os, "ftruncate", _boom())
    elif stage.startswith("write"):
        monkeypatch.setattr(L, "_os_write", _boom(errno.ENOSPC))
    elif stage == "flush-dir":
        monkeypatch.setattr(L, "_dir_flush", _boom())
    elif stage.startswith("flush-"):
        monkeypatch.setattr(L, "_flush", _file_boom(L._flush))
    elif stage == "dir-open":
        real_open = os.open
        monkeypatch.setattr(os, "open", lambda p, fl, *a: _boom(errno.EACCES)() if fl & os.O_DIRECTORY
                            else real_open(p, fl, *a))
    elif stage == "dir-close":
        real_close = os.close
        monkeypatch.setattr(L, "_os_close", lambda fd: (real_close(fd), _boom()())[1])   # close(2) frees the fd
    fds = open_fds()
    e = _refusal(path)
    err = {"write-header": "ENOSPC", "write-opened": "ENOSPC", "dir-open": "EACCES"}.get(stage, "EIO")
    assert e.name == "ledger-io:%s:%s" % (name, err)
    assert open_fds() == fds                                   # the ledger fd and the directory fd: both closed
    if name.startswith("flush-") and sys.platform == "darwin":
        assert ("F_FULLFSYNC failed at %s (EIO)" % name) in e.line


def test_flush_dir_error_with_a_close_error_after_it_is_still_flush_dir(tmp_path, monkeypatch, caplog):
    path = ledger_path(tmp_path)
    write_file(path, header())
    monkeypatch.setattr(L, "_dir_flush", _boom())
    real_close = L._os_close
    closed = []
    monkeypatch.setattr(L, "_os_close", lambda fd: (closed.append(fd), real_close(fd), _boom(errno.EBADF)())[2])
    fds = open_fds()
    with caplog.at_level(logging.WARNING, logger="levain.broker"):
        assert _refusal(path).name == "ledger-io:flush-dir:EIO"
    assert len(closed) == 1 and open_fds() == fds
    assert any("directory close failed" in r.getMessage() for r in caplog.records)


def test_success_leaves_exactly_the_ledger_fd_open(tmp_path):
    fds = open_fds()
    led = L.Ledger.open(ledger_path(tmp_path))
    assert open_fds() == fds + 1
    _close(led)
    assert open_fds() == fds


def test_dir_flush_retried_after_failed_create(tmp_path, monkeypatch):
    """r4 (complement r3 D1, D4; build note 1): the directory flushed is the ledger file's own parent."""
    path = ledger_path(tmp_path)
    parent = os.stat(os.path.dirname(path))
    monkeypatch.setattr(L, "_dir_flush", _boom())
    assert _refusal(path).name == "ledger-io:flush-dir:EIO"
    lid = file_lines(path)[0]["ledger_id"]
    monkeypatch.undo()
    order = []
    real_f, real_d = L._flush, L._dir_flush

    def dflush(fd):
        st = os.fstat(fd)
        order.append(("dir", (st.st_dev, st.st_ino) == (parent.st_dev, parent.st_ino)))
        real_f(fd)
    monkeypatch.setattr(L, "_dir_flush", dflush)
    monkeypatch.setattr(L, "_flush", lambda fd: (order.append(("file", stat.S_ISREG(os.fstat(fd).st_mode))),
                                                 real_f(fd))[1])
    led = L.Ledger.open(path)
    assert led.ledger_id == lid
    assert order == [("dir", True), ("file", True)]           # a dir flush before flush-opened, on the parent
    _close(led)
    order.clear()
    _close(L.Ledger.open(path))                                # a cleanly created file's open flushes it too
    assert order == [("dir", True), ("file", True)]


# ---- T9 -----------------------------------------------------------------------------------------------------------

async def _admit_n(led, n, settle=True):
    out = []
    for _ in range(n):
        o = await Owner(led, amount=1).start()
        await o.hold.durable()
        if settle:
            o.hold.send_gate()
            o.hold.mark_sent()
            o.hold.settle(1)
        out.append(o)
    await until(lambda: not led._lines)
    return out


def _ids(lines):
    return {(o["epoch"], o["seq"]): o for o in lines if o["kind"] == "reserved"}


def test_epoch_never_reused_after_rewind(tmp_path):
    path = ledger_path(tmp_path)
    copy = str(tmp_path / "copy.jsonl")

    async def first():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        await _admit_n(led, 3)
        shutil.copyfile(path, copy)
        await _admit_n(led, 3)
        await led.aclose(2.0)
        return led.epoch
    e1 = asyncio.run(first())
    seg1 = file_lines(path)
    shutil.copyfile(copy, path)                                # the restore (keys stopped)

    async def again():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        await _admit_n(led, 2)
        await led.aclose(2.0)
        return led.epoch
    e2 = asyncio.run(again())
    seg2 = file_lines(path)
    assert e2 != e1 and e2 not in {o.get("epoch") for o in seg1}
    a, b = _ids(seg1), _ids(seg2)
    assert all(a[k] == b[k] for k in set(a) & set(b))          # a shared (epoch, seq) is the same line
    assert {k for k in b if k[0] == e2} and not ({k for k in b if k[0] == e2} & set(a))
    os.unlink(path)
    led = L.Ledger.open(path)
    assert led.ledger_id != seg1[0]["ledger_id"]               # recreate: a new file identity
    _close(led)


def test_a_crash_before_the_opened_flush_leaves_no_reserved_under_that_epoch(tmp_path, monkeypatch):
    path = ledger_path(tmp_path)
    _close(L.Ledger.open(path))
    real = L._flush
    monkeypatch.setattr(L, "_flush", _file_boom(real))
    assert _refusal(path).name == "ledger-io:flush-opened:EIO"
    dead = file_lines(path)[-1]
    assert dead["kind"] == "opened"                            # written, never flushed, nothing admitted under it
    monkeypatch.setattr(L, "_flush", real)
    led = L.Ledger.open(path)
    assert led.epoch != dead["epoch"]
    assert [o for o in file_lines(path) if o["kind"] == "reserved"] == []
    _close(led)


def test_a_replay_torn_after_its_opened_line_resolves_the_rest_once(tmp_path):
    """complement F6: a crash inside step 5 leaves a prefix (here `opened` + one restart line)."""
    path = ledger_path(tmp_path)
    write_file(path, header(), opened(1, E1), reserved(2), reserved(3), opened(4, E2),
               line("uncertain", 5, ref=2, reason="restart", meta={}))
    led = L.Ledger.open(path)
    new = file_lines(path)[6:]
    assert led.epoch not in (E1, E2)
    assert [(o["kind"], o.get("ref")) for o in new] == [("opened", None), ("uncertain", 3)]
    assert led.totals(SCOPE).uncertain_restart == 2 * AMOUNT
    _close(led)
    led = L.Ledger.open(path)                                  # and again: nothing left to resolve
    assert [o["kind"] for o in file_lines(path)[8:]] == ["opened"]
    _close(led)


def test_in_place_overwrite_while_open_repeats_no_identity(tmp_path):
    path = ledger_path(tmp_path)

    async def main():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        await _admit_n(led, 2)
        old = open(path, "rb").read()
        await _admit_n(led, 2)
        seen = _ids(file_lines(path))
        with open(path, "r+b") as f:                           # same inode: `cp backup ledger.jsonl`
            f.write(old)
            f.truncate()
        await _admit_n(led, 2)
        await led.aclose(2.0)
        return seen
    seen = asyncio.run(main())
    after = file_lines(path)
    later = [(o["epoch"], o["seq"]) for o in after if o["kind"] == "reserved"]
    assert len(later) == len(set(later))
    assert all(k not in seen or seen[k] == _ids(after)[k] for k in later)
    assert len({k for k in later if k not in seen}) == 2      # the two admits after the overwrite: new identities


# ---- T10 ----------------------------------------------------------------------------------------------------------

def test_flock_refuses_second_open(tmp_path, monkeypatch):
    path = ledger_path(tmp_path)
    led = L.Ledger.open(path)
    fds = open_fds()
    assert _refusal(path).name == "ledger-locked"
    assert open_fds() == fds
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    code = ("import sys\nfrom levain.broker import ledger as L\ntry:\n    L.Ledger.open(sys.argv[1])\n"
            "except L.LedgerOpenError as e:\n    print(e.name)\n")
    r = subprocess.run([sys.executable, "-c", code, path], env=env, capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "ledger-locked", r.stderr
    ctl = FlushCtl()
    ctl.errors = [OSError(errno.EIO, "Input/output error")]

    async def unhealthy():
        monkeypatch.setattr(L, "_flush", ctl)
        led.attach(asyncio.get_running_loop())
        o = await Owner(led).start()
        await until(lambda: not led.healthy)
        assert _refusal(path).name == "ledger-locked"          # E4: the dead writer keeps the lock
        await o.finish()
        await led.aclose(2.0)
    asyncio.run(unhealthy())
    monkeypatch.undo()
    _close(L.Ledger.open(path))


# ---- T25 ----------------------------------------------------------------------------------------------------------

F_FULLFSYNC = getattr(fcntl, "F_FULLFSYNC", 51)


class FakeFcntl:
    """`fcntl.fcntl` with F_FULLFSYNC counted per fd kind; call `fail_at` (1-based) raises `err`."""

    def __init__(self, fail_at=0, err=errno.EIO):
        self.real = fcntl.fcntl
        self.fail_at, self.err = fail_at, err
        self.kinds = []

    def __call__(self, fd, cmd, *a):
        if cmd != F_FULLFSYNC:
            return self.real(fd, cmd, *a)
        self.kinds.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        if len(self.kinds) == self.fail_at:
            raise OSError(self.err, os.strerror(self.err))
        return 0


@pytest.fixture()
def darwin(monkeypatch):
    fsyncs = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(fcntl, "F_FULLFSYNC", F_FULLFSYNC, raising=False)
    monkeypatch.setattr(os, "fsync", lambda fd: fsyncs.append(fd))
    monkeypatch.setattr(creds, "full_fsync", _boom())
    monkeypatch.setattr(creds, "dir_fsync", _boom())
    return fsyncs


PATHS = {"create": ["flush-header", "flush-dir", "flush-opened"],
         "torn": ["flush-truncate", "flush-dir", "flush-opened"],
         "clean": ["flush-dir", "flush-opened"]}
STRICT = [(p, i, e) for p in PATHS for i in range(len(PATHS[p]))
          for e in (errno.EIO, errno.ENOTSUP, errno.ENOTTY, errno.EINVAL, errno.ENODEV)]


@pytest.mark.parametrize("kind,i,err", STRICT, ids=["%s-%d-%s" % (p, i, errno.errorcode[e]) for p, i, e in STRICT])
def test_strict_flush_refuses_the_open_on_darwin(tmp_path, monkeypatch, darwin, kind, i, err):
    path = ledger_path(tmp_path)
    if kind == "torn":
        write_file(path, header(), b'{"to')
    elif kind == "clean":
        write_file(path, header())
    fake = FakeFcntl(fail_at=i + 1, err=err)
    monkeypatch.setattr(fcntl, "fcntl", fake)
    stage = PATHS[kind][i]
    e = _refusal(path)
    assert e.name == "ledger-io:%s:%s" % (stage, errno.errorcode[err])
    assert ("F_FULLFSYNC failed at %s (%s); the ledger's filesystem must support F_FULLFSYNC"
            % (stage, errno.errorcode[err])) in e.line
    assert darwin == []                                         # os.fsync never called: no fallback


def test_strict_flush_retry_after_a_refused_create_dir_flush(tmp_path, monkeypatch, darwin):
    path = ledger_path(tmp_path)
    monkeypatch.setattr(fcntl, "fcntl", FakeFcntl(fail_at=2))
    assert _refusal(path).name == "ledger-io:flush-dir:EIO"
    fake = FakeFcntl()
    monkeypatch.setattr(fcntl, "fcntl", fake)
    _close(L.Ledger.open(path))
    assert fake.kinds == ["dir", "file"] and darwin == []


def test_strict_flush_after_open_is_a_fault(tmp_path, monkeypatch, darwin):
    path = ledger_path(tmp_path)
    fake = FakeFcntl()
    monkeypatch.setattr(fcntl, "fcntl", fake)
    led = L.Ledger.open(path)
    fake.fail_at = len(fake.kinds) + 1

    async def main():
        led.attach(asyncio.get_running_loop())
        o = await Owner(led).start()
        await until(lambda: not led.healthy)
        assert led.fault_reason == "flush:EIO" and o.hold.state is L.STRANDED
        await o.finish()
        await led.aclose(2.0)
    asyncio.run(main())
    assert darwin == []


def test_off_darwin_every_flush_is_fsync(tmp_path, monkeypatch):
    path = ledger_path(tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    seen = []
    monkeypatch.setattr(fcntl, "fcntl", lambda fd, cmd, *a: seen.append(cmd))
    fsyncs = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (fsyncs.append(fd), real(fd))[1])
    _close(L.Ledger.open(path))
    assert len(fsyncs) == 3 and F_FULLFSYNC not in seen        # header, directory, opened
    os.unlink(path)
    monkeypatch.setattr(os, "fsync", _boom())
    e = _refusal(path)
    assert e.name == "ledger-io:flush-header:EIO" and "F_FULLFSYNC" not in e.line
    monkeypatch.setattr(os, "fsync", real)
    os.unlink(path)
    led = L.Ledger.open(path)
    monkeypatch.setattr(os, "fsync", _boom())

    async def main():
        led.attach(asyncio.get_running_loop())
        o = await Owner(led).start()
        await until(lambda: not led.healthy)
        assert led.fault_reason == "flush:EIO"
        await o.finish()
        await led.aclose(2.0)
    asyncio.run(main())


# ---- T17 ----------------------------------------------------------------------------------------------------------

STDLIB = {"__future__", "asyncio", "datetime", "enum", "errno", "fcntl", "functools", "inspect", "json", "logging",
          "os", "queue", "re", "secrets", "sys", "threading", "time", "typing"}
FORBIDDEN = {"policy", "codec", "routemap", "generation", "pin", "lineage", "full_fsync", "dir_fsync"}


def test_ledger_imports_only_stdlib():
    src = (ROOT / "levain" / "broker" / "ledger.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods = set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
            assert node.level == 0
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    assert mods <= STDLIB, mods - STDLIB
    parts = {p for n in names for p in n.lower().split("_") if p} | {n.lower().strip("_") for n in names}
    assert not (parts & FORBIDDEN), parts & FORBIDDEN
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_flush", "_dir_flush")):
        assert not [x for x in ast.walk(fn) if isinstance(x, (ast.Try, ast.ExceptHandler))], fn.name
    assert {n.name for n in tree.body if isinstance(n, ast.FunctionDef)} >= {"_flush", "_dir_flush"}
