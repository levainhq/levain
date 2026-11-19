"""Tests for the cockpit snapshot store (levain/cockpit/snapshots.py; contract in docs/cockpit_snapshot_contract.md)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from levain.cockpit import snapshots as S


def _pub(w, pid="a", body=None, *, status="ok", derived_at=1.0, **kw):
    return w.publish(
        pid, status=status, body=S.encode_body({"x": 1} if body is None else body), started_at=derived_at - 0.1,
        derived_at=derived_at, duration_ms=5.0, **kw,
    )


@pytest.fixture
def db(tmp_path):
    return tmp_path / S.DB_FILENAME


def test_encode_body_canonical():
    assert S.encode_body({"b": 1, "a": "é"}) == b'{"a":"\\u00e9","b":1}'
    assert S.encode_body({"a": 1, "b": 2}) == S.encode_body({"b": 2, "a": 1})


def test_encode_body_refuses_nan_and_survives_a_lone_surrogate():
    for bad in (float("nan"), float("inf"), {"x": [float("-inf")]}):
        with pytest.raises(ValueError):
            S.encode_body(bad)
    body = S.encode_body({"name": "a\ud83db"})
    assert body.isascii() and json.loads(body) == {"name": "a\ud83db"}


def test_round_trip(db):
    w = S.SnapshotWriter(db, pid=42, now=10.0)
    g = w.publish(
        "a", status="partial", body=S.encode_body({"k": [1, 2]}), started_at=1.0, derived_at=2.0, duration_ms=3.5,
        valid_until=99.0, inputs_fp="fp", write_gen=4, error="boom",
    )
    w.heartbeat(7.0)
    w.set_stream("inputs", state="degraded", last_event_at=6.0, detail="d")
    r = S.SnapshotReader(db)
    snap = r.read("a")
    assert g == snap.gen == 1
    assert (snap.started_at, snap.derived_at, snap.duration_ms, snap.valid_until) == (1.0, 2.0, 3.5, 99.0)
    assert (snap.inputs_fp, snap.write_gen, snap.status, snap.error) == ("fp", 4, "partial", "boom")
    assert snap.body == S.encode_body({"k": [1, 2]})
    assert snap.body_sha == hashlib.sha256(snap.body).hexdigest()
    assert r.decoded(snap) == {"k": [1, 2]}
    m = r.meta()
    assert m["contract"] == S.CONTRACT and m["worker_pid"] == "42" and float(m["heartbeat_at"]) == 7.0
    assert float(m["worker_started_at"]) == 10.0
    assert r.streams() == {"inputs": {"state": "degraded", "last_event_at": 6.0, "detail": "d"}}
    assert r.read("nope") is None
    assert set(r.read_all()) == {"a"}


def test_gen_unchanged_on_identical_body_timing_moves(db):
    w = S.SnapshotWriter(db)
    assert _pub(w, derived_at=1.0, valid_until=5.0, inputs_fp="x") == 1
    assert _pub(w, derived_at=9.0, valid_until=20.0, inputs_fp="y") == 1
    snap = S.SnapshotReader(db).read("a")
    assert (snap.gen, snap.derived_at, snap.valid_until, snap.inputs_fp) == (1, 9.0, 20.0, "y")


def test_gen_bumps_on_changed_body_and_status(db):
    w = S.SnapshotWriter(db)
    assert _pub(w, body={"x": 1}) == 1
    assert _pub(w, body={"x": 2}) == 2
    assert _pub(w, body={"x": 2}, status="error", error="e") == 3
    assert _pub(w, body={"x": 2}, status="error", error="e") == 3


def test_write_gen_keeps_max(db):
    w = S.SnapshotWriter(db)
    r = S.SnapshotReader(db)
    _pub(w, write_gen=5)
    _pub(w, write_gen=3)
    assert r.read("a").write_gen == 5
    _pub(w, body={"x": 9}, write_gen=8)
    assert r.read("a").write_gen == 8


def test_manifest_read_is_one_snapshot_under_concurrent_publish(db):
    """publish_seq moves with every gen bump, so inside one transaction it equals the sum of the rows' gens."""
    w = S.SnapshotWriter(db)
    for p in ("a", "b"):
        _pub(w, p, body={"n": 0})
    stop = threading.Event()
    errs: list = []

    def write():
        n = 0
        while not stop.is_set():
            n += 1
            for p in ("a", "b"):
                _pub(w, p, body={"n": n})

    def read():
        r = S.SnapshotReader(db)
        for _ in range(1500):
            meta, panels = r.read_manifest()
            total = sum(s.gen for s in panels.values())
            if int(meta["publish_seq"]) != total:
                errs.append((meta["publish_seq"], total))
                return
            for snap in panels.values():
                if hashlib.sha256(snap.body).hexdigest() != snap.body_sha:
                    errs.append(snap.id)
                    return

    t = threading.Thread(target=write)
    readers = [threading.Thread(target=read) for _ in range(2)]
    t.start()
    for x in readers:
        x.start()
    for x in readers:
        x.join(60)
    stop.set()
    t.join(30)
    assert not errs
    assert not t.is_alive() and not any(x.is_alive() for x in readers)


def test_publish_seq_moves_with_gen_bumps_only_and_heartbeat_moves_data_version(db):
    w = S.SnapshotWriter(db)
    r = S.SnapshotReader(db)
    assert r.meta()["publish_seq"] == "0"
    _pub(w)
    assert r.meta()["publish_seq"] == "1"
    _pub(w, derived_at=5.0)  # same body and status: no gen bump
    assert r.meta()["publish_seq"] == "1"
    _pub(w, body={"x": 2})
    _pub(w, body={"x": 2}, status="error", error="e")
    assert r.meta()["publish_seq"] == "3"
    v = r.data_version()
    w.heartbeat(9.0)
    assert r.data_version() != v and r.meta()["publish_seq"] == "3"


def test_set_stream_writes_only_on_a_change(db):
    w = S.SnapshotWriter(db)
    r = S.SnapshotReader(db)
    assert w.set_stream("inputs", state="ok", last_event_at=None) is True
    v = r.data_version()
    assert w.set_stream("inputs", state="ok", last_event_at=None) is False
    assert r.data_version() == v
    assert w.set_stream("inputs", state="degraded", last_event_at=None, detail="d") is True
    assert w.set_stream("inputs", state="degraded", last_event_at=3.0, detail="d") is True
    assert r.streams()["inputs"] == {"state": "degraded", "last_event_at": 3.0, "detail": "d"}


class _SpyDb:
    """Wraps the writer's connection: records every statement, fails the first COMMIT when asked."""

    def __init__(self, real, fail_commit=False):
        self.real, self.fail_commit, self.sql = real, fail_commit, []

    def execute(self, sql, *args):
        self.sql.append(sql)
        if sql == "COMMIT" and self.fail_commit:
            self.fail_commit = False
            raise sqlite3.OperationalError("database or disk is full")
        return self.real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_failed_commit_rolls_back_and_the_writer_keeps_working(db):
    w = S.SnapshotWriter(db)
    w._db = _SpyDb(w._db, fail_commit=True)
    with pytest.raises(sqlite3.OperationalError):
        _pub(w)
    assert S.SnapshotReader(db).read("a") is None
    assert _pub(w) == 1  # without the rollback this raises "cannot start a transaction within a transaction"
    assert S.SnapshotReader(db).read("a").gen == 1


def test_unchanged_body_is_not_rewritten(db):
    w = S.SnapshotWriter(db)
    _pub(w)
    spy = w._db = _SpyDb(w._db)
    assert _pub(w, derived_at=7.0, valid_until=9.0) == 1
    assert not any("INSERT OR REPLACE INTO panel" in q for q in spy.sql)
    snap = S.SnapshotReader(db).read("a")
    assert (snap.derived_at, snap.valid_until, snap.body) == (7.0, 9.0, S.encode_body({"x": 1}))


def test_journal_is_wal_and_size_limited(db):
    w = S.SnapshotWriter(db)
    assert w._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert w._db.execute("PRAGMA journal_size_limit").fetchone()[0] == S.JOURNAL_SIZE_LIMIT


def test_body_none_keeps_the_stored_body_across_a_writer_reopen(db):
    w = S.SnapshotWriter(db)
    _pub(w, body={"good": True})
    w.close()
    w2 = S.SnapshotWriter(db)
    g = w2.publish("a", status="error", body=None, started_at=1.0, derived_at=2.0, duration_ms=1.0, error="boom")
    snap = S.SnapshotReader(db).read("a")
    assert g == 2 and snap.status == "error" and snap.error == "boom" and snap.derived_at == 2.0
    assert snap.body == S.encode_body({"good": True}) and snap.body_sha == hashlib.sha256(snap.body).hexdigest()
    # a panel with no stored row gets the body null
    w2.publish("new", status="error", body=None, started_at=1.0, derived_at=2.0, duration_ms=1.0)
    assert S.SnapshotReader(db).read("new").body == b"null"


def test_second_writer_is_refused_in_process_and_across_processes(db, tmp_path):
    w = S.SnapshotWriter(db)
    with pytest.raises(S.WriterLocked):
        S.SnapshotWriter(db)
    script = tmp_path / "second_writer.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(S.__file__))))!r})\n"
        "from levain.cockpit import snapshots\n"
        "try:\n"
        "    snapshots.SnapshotWriter(sys.argv[1])\n"
        "except snapshots.WriterLocked:\n"
        "    sys.exit(7)\n"
    )
    res = subprocess.run([sys.executable, str(script), str(db)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 7, res.stderr
    w.close()
    S.SnapshotWriter(db).close()  # released with the first writer


def test_data_version_moves_after_publish_elsewhere(db):
    w = S.SnapshotWriter(db)
    r = S.SnapshotReader(db)
    v0 = r.data_version()
    assert r.data_version() == v0
    _pub(w)
    assert r.data_version() != v0


def test_decoded_cache_hit_and_bounded(db, monkeypatch):
    w = S.SnapshotWriter(db)
    r = S.SnapshotReader(db)
    _pub(w, body={"x": 1})
    snap = r.read("a")
    assert r.decoded(snap) is r.decoded(snap)
    _pub(w, body={"x": 2})
    snap2 = r.read("a")
    assert snap2.gen == 2 and r.decoded(snap2) == {"x": 2} and r.decoded(snap2) is not r.decoded(snap)
    monkeypatch.setattr(S, "DECODED_CACHE_MAX", 3)
    for i in range(10):
        r.decoded(S.PanelSnap(f"p{i}", 1, 0, 0, 0, None, "", 0, "ok", None, "", b"{}"))
    assert len(r._cache) == 3
    assert ("p9", 1, "") in r._cache and ("p0", 1, "") not in r._cache


def test_missing_db_is_empty(tmp_path):
    r = S.SnapshotReader(tmp_path / "absent.db")
    assert r.read("a") is None and r.read_all() == {} and r.meta() == {} and r.streams() == {}
    assert r.data_version() == 0


def _fake_major(db, value):
    c = sqlite3.connect(db)
    c.execute("UPDATE meta SET value=? WHERE key='contract'", (value,))
    c.commit()
    c.close()


def test_unknown_major_refused_by_writer_and_reader(db):
    S.SnapshotWriter(db).close()
    _fake_major(db, "levain-cockpit-snapshots/2")
    with pytest.raises(S.ContractMismatch):
        S.SnapshotWriter(db)
    r = S.SnapshotReader(db)
    for call in (lambda: r.read("a"), r.read_all, r.meta):
        with pytest.raises(S.ContractMismatch):
            call()
    # the refusal left the file alone
    assert sqlite3.connect(db).execute("SELECT value FROM meta WHERE key='contract'").fetchone()[0].endswith("/2")


def test_reopen_same_major_keeps_rows(db):
    w = S.SnapshotWriter(db)
    _pub(w)
    w.close()
    w2 = S.SnapshotWriter(db)
    assert _pub(w2) == 1


def test_reader_is_read_only(db):
    S.SnapshotWriter(db).close()
    r = S.SnapshotReader(db)
    r.meta()
    with pytest.raises(sqlite3.OperationalError):
        r._conn().execute("INSERT INTO meta(key, value) VALUES ('k', 'v')")


def test_one_connection_per_thread(db):
    S.SnapshotWriter(db).close()
    r = S.SnapshotReader(db)
    seen = []
    r.meta()
    seen.append(r._conn())
    t = threading.Thread(target=lambda: seen.append(r._conn()))
    t.start()
    t.join(10)
    assert seen[0] is not seen[1]


def test_reader_sees_file_created_later(tmp_path):
    p = tmp_path / S.DB_FILENAME
    r = S.SnapshotReader(p)
    assert r.read_all() == {}
    w = S.SnapshotWriter(p)
    _pub(w)
    assert set(r.read_all()) == {"a"}


def test_decoded_cache_keys_on_body_sha_so_a_recreated_store_never_serves_the_old_body(db):
    w = S.SnapshotWriter(db)
    r = S.SnapshotReader(db)
    _pub(w, body={"v": "old"})
    old = r.read("a")
    assert r.decoded(old) == {"v": "old"}
    other = S.PanelSnap("a", old.gen, 0, 0, 0, None, "", 0, "ok", None, "different-sha", S.encode_body({"v": "new"}))
    assert other.gen == old.gen and r.decoded(other) == {"v": "new"}


def test_db_recreated_under_a_live_reader_is_re_read(db, monkeypatch):
    monkeypatch.setattr(S, "READER_REVALIDATE_S", 0.0)
    w = S.SnapshotWriter(db)
    r = S.SnapshotReader(db)
    _pub(w, body={"v": "old"})
    assert r.decoded(r.read("a")) == {"v": "old"}
    w.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(str(db) + suffix):
            os.unlink(str(db) + suffix)
    assert r.read("a") is None and r.read_all() == {}  # the file is gone: absent, not the old inode
    w2 = S.SnapshotWriter(db)
    _pub(w2, body={"v": "new"})
    snap = r.read("a")
    assert snap.gen == 1 and r.decoded(snap) == {"v": "new"}


def test_revalidation_is_rate_limited(db, monkeypatch):
    S.SnapshotWriter(db).close()
    r = S.SnapshotReader(db)
    stats = []
    real = os.stat
    monkeypatch.setattr(S.os, "stat", lambda p, *a, **k: (stats.append(p), real(p, *a, **k))[1])
    for _ in range(5):
        r.meta()
    assert len(stats) == 1  # READER_REVALIDATE_S has not passed


def test_percent_and_reserved_characters_in_the_path(tmp_path):
    d = tmp_path / "a%20b#c?d"
    d.mkdir()
    p = d / S.DB_FILENAME
    w = S.SnapshotWriter(p)
    _pub(w)
    assert S.SnapshotReader(p).read("a").gen == 1


def test_busy_or_io_errors_raise_instead_of_reading_as_absent(db):
    S.SnapshotWriter(db).close()
    bad = S.SnapshotReader(db)
    bad._local.slot = (_Boom(), 0, 0, time.monotonic() + 1e6)
    for call in (lambda: bad.read("a"), bad.read_all, bad.meta, bad.streams, bad.data_version):
        with pytest.raises(S.SnapshotReadError):
            call()


class _Boom:
    def execute(self, sql, *a):
        raise sqlite3.OperationalError("database is locked")

    def close(self):
        pass


def test_missing_table_reads_as_absent_and_no_contract_row_reads_as_empty(tmp_path):
    p = tmp_path / "x.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE other(x)")
    c.commit()
    r = S.SnapshotReader(p)
    assert r.read("a") is None and r.read_all() == {} and r.streams() == {} and r.meta() == {}
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE panel(id)")
    c.commit()
    assert r.read("a") is None and r.read_all() == {}  # no contract row: empty, not a mismatch
    c.close()


def test_contract_name_must_match_not_only_the_major(db):
    S.SnapshotWriter(db).close()
    _fake_major(db, "some-other-product/1")
    with pytest.raises(S.ContractMismatch):
        S.SnapshotReader(db).meta()
    with pytest.raises(S.ContractMismatch):
        S.SnapshotReader(db).streams()


def test_reader_close_drops_this_threads_connection(db):
    S.SnapshotWriter(db).close()
    r = S.SnapshotReader(db)
    r.meta()
    first = r._conn()
    r.close()
    r.close()
    assert r.meta() and r._conn() is not first
