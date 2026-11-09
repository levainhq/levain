"""`levain doctor`'s ``queue answers`` row: the answers directory must be the operator's, in the broker's group, with
no write bit for any other principal. Only the exact old shape warns; every other shape fails."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from levain import doctor

ME, GID, OLD = 501, 4321, 8765


def st(mode=0o2750, uid=ME, gid=GID, kind=stat.S_IFDIR):
    return os.stat_result((kind | mode, 0, 0, 0, uid, gid, 0, 0, 0, 0))


@pytest.fixture
def store(tmp_path):
    (tmp_path / "policy").mkdir()
    return tmp_path


def row(store, stat_result=None, *, uid=ME, missing=False, gid=GID, host="darwin"):
    def lstat(p):
        assert Path(p) == store / "queue" / "answers"
        if missing:
            raise FileNotFoundError(p)
        return stat_result
    rows = doctor._check_queue_answers(host=host, store=store, uid=uid, lstat=lstat, qbroker_gid=lambda: gid,
                                     legacy_gid=lambda: OLD)
    assert len(rows) == 1
    return rows[0]


def test_no_row_before_the_isolation_install(tmp_path):
    assert doctor._check_queue_answers(host="darwin", store=tmp_path, uid=ME) == []


@pytest.mark.parametrize("mode", [0o2750, 0o750])
def test_the_right_shape_passes(store, mode):
    r = row(store, st(mode=mode), host="darwin")
    assert r.ok and not r.warn and not r.neutral


def test_the_exact_old_shape_warns_and_does_not_fail(store):
    r = row(store, st(gid=OLD, mode=0o2770))
    assert r.ok and r.warn
    assert "dispatch can still create files" in r.detail and "levain upgrade" in r.hint


@pytest.mark.parametrize("what,kw", [
    ("dispatch-owned 2750", {"uid": ME + 1}),
    ("root:root 0700", {"uid": 0, "gid": 0, "mode": 0o700}),
    ("a regular file", {"kind": stat.S_IFREG}),
    ("a symlink", {"kind": stat.S_IFLNK}),
    ("legacy group, mode 0750", {"gid": OLD, "mode": 0o750}),
    ("legacy group, 2750", {"gid": OLD, "mode": 0o2750}),
    ("an unknown group", {"gid": GID + 1}),
    ("world writable", {"mode": 0o2757}),
    ("group writable in the right group", {"mode": 0o2770}),
    ("operator cannot write", {"mode": 0o2550}),
    ("broker cannot read", {"mode": 0o2700}),
    ("old owner not the operator", {"gid": OLD, "mode": 0o2770, "uid": ME + 1}),
])
def test_any_other_shape_fails(store, what, kw):
    r = row(store, st(**kw))
    assert not r.ok and not r.warn, what
    assert "levain upgrade" in r.hint


def test_0750_is_right_on_macos_only(store):
    assert row(store, st(mode=0o750), host="darwin").ok
    r = row(store, st(mode=0o750), host="linux")
    assert not r.ok


def test_2750_passes_on_linux(store):
    r = row(store, st(mode=0o2750), host="linux")
    assert r.ok and not r.warn


def test_the_ok_row_says_acls_were_not_read(store):
    r = row(store, st())
    assert "ACL" in r.detail and "not read" in r.detail


def test_a_missing_directory_is_a_failure(store):
    r = row(store, missing=True)
    assert not r.ok and "levain upgrade" in r.hint


def test_run_as_root_is_not_asked(store):
    r = row(store, st(), uid=0)
    assert r.neutral and r.ok
