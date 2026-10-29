"""G3-1: end.return_move, the keep/ -> session-tree return (DESIGN g3 §1, tests 1-5)."""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import end as E
from levain.slot.end import EndFacts, park_move, return_move
from levain.slot.ids import SlotId

ME = os.getuid()
NAME = ".credentials.json"
BODY = b'{"claudeAiOauth":{"accessToken":"x"}}'
POINTS = ("return:checked", "return:renamed", "return:source-synced", "return:done")


def mine(st) -> bool:
    return st.st_uid == ME


class Crash(Exception):
    pass


class W:
    def __init__(self, tmp: Path, kind: str = "interactive") -> None:
        self.store = tmp
        self.facts = EndFacts("s-g31", SlotId("egress", 3), ME, os.getgid(), tmp, None, 1, None, "claude",
                              "passthrough", kind)
        self.keep = self.facts.keep
        self.tree = tmp / "live" / "s-g31" / "config" / "claude"
        self.keep.mkdir(parents=True, mode=0o700)
        self.tree.mkdir(parents=True, mode=0o700)

    def put_keep(self) -> Path:
        p = self.keep / NAME
        p.write_bytes(BODY)
        p.chmod(0o600)
        return p

    def run(self, **kw):
        return return_move(self.facts, owner_ok=mine, **kw)

    def state(self):
        return (sorted(os.listdir(self.keep)), sorted(os.listdir(self.tree)))


@pytest.fixture
def w(tmp_path):
    return W(tmp_path)


def test_return_renames_keeps_inode_and_never_opens(w, monkeypatch):
    src = w.put_keep()
    before = os.lstat(src)
    real_open = os.open

    def guard(path, *a, **k):
        if os.fsdecode(path) == NAME:
            raise AssertionError("the credential was opened")
        return real_open(path, *a, **k)

    synced = []
    real_sync = os.fsync

    def rec(fd):
        synced.append(os.fstat(fd).st_ino)
        return real_sync(fd)

    monkeypatch.setattr(os, "open", guard)
    monkeypatch.setattr(os, "fsync", rec)
    assert w.run() == "moved"
    monkeypatch.undo()
    after = os.lstat(w.tree / NAME)
    assert (after.st_ino, after.st_dev, after.st_mode, after.st_uid) == (before.st_ino, before.st_dev, before.st_mode, before.st_uid)
    assert (w.tree / NAME).read_bytes() == BODY
    assert os.listdir(w.keep) == []
    assert synced == [os.stat(w.keep).st_ino, os.stat(w.tree).st_ino]


def test_fault_points_are_hit_in_order(w):
    w.put_keep()
    seen = []
    w.run(fault=seen.append)
    assert tuple(seen) == POINTS


@pytest.mark.parametrize("point", POINTS)
def test_crash_at_every_point_then_park_move_recovers(tmp_path, point):
    w = W(tmp_path)
    w.put_keep()

    def boom(p):
        if p == point:
            raise Crash(p)

    with pytest.raises(Crash):
        w.run(fault=boom)
    # one copy, byte-equal, in keep/ or the tree: never neither, never both
    k, t = w.state()
    assert len(k) + len(t) == 1
    held = (w.keep if k else w.tree) / NAME
    assert held.read_bytes() == BODY
    park_move(w.facts, owner_ok=mine)
    assert w.state() == ([NAME], [])
    assert (w.keep / NAME).read_bytes() == BODY
    assert w.run() == "moved"          # a fresh start after END
    assert w.state() == ([], [NAME])


def test_crash_before_rename_leaves_keep_and_park_is_a_noop(tmp_path):
    w = W(tmp_path)
    w.put_keep()

    def boom(p):
        if p == "return:checked":
            raise Crash

    with pytest.raises(Crash):
        w.run(fault=boom)
    assert w.state() == ([NAME], [])
    park_move(w.facts, owner_ok=mine)
    assert w.state() == ([NAME], [])


def test_retry_with_tree_holding_it_moves_nothing_and_resyncs(w, monkeypatch):
    w.put_keep()
    w.run()
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_ino), real(fd))[1])
    ino = os.lstat(w.tree / NAME).st_ino
    assert w.run() == "already"
    assert synced == [os.stat(w.keep).st_ino, os.stat(w.tree).st_ino]
    assert os.lstat(w.tree / NAME).st_ino == ino
    assert w.state() == ([], [NAME])


def test_retry_runs_the_shape_check_on_the_tree_entry(w):
    (w.tree / NAME).write_bytes(b"x" * (E.V14_BYTES + 1))
    with pytest.raises(Refused) as e:
        w.run()
    assert e.value.name == "keep-shape"
    assert w.state() == ([], [NAME])


def test_both_present_is_a_conflict_and_nothing_changes(w):
    w.put_keep()
    (w.tree / NAME).write_bytes(b"other")
    with pytest.raises(Refused) as e:
        w.run()
    assert e.value.name == "keep-return-conflict"
    assert (w.keep / NAME).read_bytes() == BODY
    assert (w.tree / NAME).read_bytes() == b"other"


def test_both_absent_is_no_credential(w):
    with pytest.raises(Refused) as e:
        w.run()
    assert e.value.name == "no-credential:e3"
    assert w.state() == ([], [])


def _plant(w: W, shape: str) -> None:
    p = w.keep / NAME
    if shape == "symlink":
        target = w.store / "elsewhere"
        target.write_bytes(BODY)
        p.symlink_to(target)
    elif shape == "fifo":
        os.mkfifo(p)
    elif shape == "dir":
        p.mkdir()
    elif shape == "links":
        w.put_keep()
        os.link(p, w.store / "second-name")
    elif shape == "big":
        p.write_bytes(b"x" * (E.V14_BYTES + 1))


@pytest.mark.parametrize("shape", ["symlink", "fifo", "dir", "links", "big"])
def test_bad_shape_refuses_keep_shape_and_changes_nothing(w, shape):
    _plant(w, shape)
    before = os.lstat(w.keep / NAME)
    with pytest.raises(Refused) as e:
        w.run()
    assert e.value.name == "keep-shape"
    assert w.state() == ([NAME], [])
    after = os.lstat(w.keep / NAME)
    assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode)


def test_wrong_owner_refuses_keep_shape(w):
    w.put_keep()
    with pytest.raises(Refused) as e:
        w.run(slot_owner_ok=lambda st: False)
    assert e.value.name == "keep-shape"
    assert w.state() == ([NAME], [])


def test_default_owner_rule_is_the_alloc_slot_uid(tmp_path):
    w = W(tmp_path)
    w.put_keep()
    other = EndFacts(**{**w.facts.__dict__, "slot_uid": ME + 1})
    with pytest.raises(Refused) as e:
        return_move(other, owner_ok=mine)
    assert e.value.name == "keep-shape"
    assert return_move(w.facts, owner_ok=mine) == "moved"


def test_keep_not_root_owned_is_bad_root(w):
    w.put_keep()
    with pytest.raises(Refused) as e:
        return_move(w.facts, owner_ok=lambda st: False)
    assert e.value.name == "bad-root"
    assert w.state() == ([NAME], [])


@pytest.mark.parametrize("err", [errno.EIO, errno.EACCES, errno.ENOTDIR])
def test_a_presence_check_that_errors_is_bad_root_not_a_raw_oserror(w, monkeypatch, err):
    """L3 r1 (complement): ``_present``'s fstatat errors other than ENOENT are ``bad-root``, like the rest."""
    w.put_keep()
    real = os.stat

    def stat_(path, *a, **k):
        if path == NAME.encode() and k.get("dir_fd") is not None:
            raise OSError(err, "injected")
        return real(path, *a, **k)
    monkeypatch.setattr(os, "stat", stat_)
    with pytest.raises(Refused) as e:
        w.run()
    monkeypatch.undo()
    assert e.value.name == "bad-root"
    assert w.state() == ([NAME], [])


@pytest.mark.parametrize("point", ["return:renamed", "return:source-synced", "return:done"])
def test_an_error_after_the_rename_is_bad_root_with_the_credential_in_the_tree(w, point):
    """The docstring's claim, held: ``bad-root`` after the rename leaves the one copy in the tree (the caller parks
    it back); before it, nothing moved."""
    w.put_keep()

    def boom(p):
        if p == point:
            raise OSError(errno.EIO, "injected")
    with pytest.raises(Refused) as e:
        w.run(fault=boom)
    assert e.value.name == "bad-root"
    assert w.state() == ([], [NAME])
    park_move(w.facts, owner_ok=mine)
    assert w.state() == ([NAME], [])


def test_other_kinds_never_touch_keep(tmp_path):
    w = W(tmp_path, kind="login")
    w.put_keep()
    with pytest.raises(Refused):
        w.run()
    assert w.state() == ([NAME], [])


def test_exported():
    assert "return_move" in E.__all__
