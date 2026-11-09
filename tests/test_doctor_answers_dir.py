"""`levain doctor`'s ``queue answers`` row: the answers directory must be the operator's, in the broker's group, with
no write bit for any other principal. An install still in the old shape works, but dispatch could create a file
there: a warning, never a failure."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from levain import doctor

ME, GID = 501, 4321


def st(mode=0o2750, uid=ME, gid=GID, kind=stat.S_IFDIR):
    return os.stat_result((kind | mode, 0, 0, 0, uid, gid, 0, 0, 0, 0))


@pytest.fixture
def store(tmp_path):
    (tmp_path / "policy").mkdir()
    return tmp_path


def row(store, stat_result=None, *, uid=ME, missing=False, gid=GID):
    def lstat(p):
        assert Path(p) == store / "queue" / "answers"
        if missing:
            raise FileNotFoundError(p)
        return stat_result
    rows = doctor._check_queue_answers(host="darwin", store=store, uid=uid, lstat=lstat, qbroker_gid=lambda: gid)
    assert len(rows) == 1
    return rows[0]


def test_no_row_before_the_isolation_install(tmp_path):
    assert doctor._check_queue_answers(host="darwin", store=tmp_path, uid=ME) == []


@pytest.mark.parametrize("mode", [0o2750, 0o750])
def test_the_right_shape_passes(store, mode):
    r = row(store, st(mode=mode))
    assert r.ok and not r.warn and not r.neutral


@pytest.mark.parametrize("what,kw", [
    ("old queue group", {"gid": GID + 1}),
    ("old mode", {"mode": 0o2770}),
    ("world writable", {"mode": 0o2757}),
    ("another owner", {"uid": ME + 1}),
])
def test_the_old_shape_warns_and_does_not_fail(store, what, kw):
    r = row(store, st(**kw))
    assert r.ok and r.warn, what
    assert "dispatch can still create files" in r.detail and "levain upgrade" in r.hint


def test_not_a_directory_warns(store):
    r = row(store, st(kind=stat.S_IFLNK))
    assert r.ok and r.warn


def test_a_missing_directory_is_a_failure(store):
    r = row(store, missing=True)
    assert not r.ok and "levain upgrade" in r.hint


def test_run_as_root_is_not_asked(store):
    r = row(store, st(), uid=0)
    assert r.neutral and r.ok
