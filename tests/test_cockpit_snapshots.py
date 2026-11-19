"""Tests for the cockpit snapshot store (levain/cockpit/snapshots.py; contract in docs/cockpit_snapshot_contract.md)."""

from __future__ import annotations

import hashlib
import sqlite3
import threading

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
    assert S.encode_body({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()
    assert S.encode_body({"a": 1, "b": 2}) == S.encode_body({"b": 2, "a": 1})


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


def test_per_row_integrity_under_concurrent_publish(db):
    w = S.SnapshotWriter(db)
    for p in ("a", "b"):
        _pub(w, p, body={"n": 0, "pad": "z" * 2000})
    stop = threading.Event()
    errs: list = []

    def write():
        n = 0
        while not stop.is_set():
            n += 1
            for p in ("a", "b"):
                _pub(w, p, body={"n": n, "pad": "z" * 2000})

    def read():
        r = S.SnapshotReader(db)
        for _ in range(1500):
            for snap in r.read_all().values():
                if hashlib.sha256(snap.body).hexdigest() != snap.body_sha:
                    errs.append(snap.id)
                    return

    t = threading.Thread(target=write)
    readers = [threading.Thread(target=read) for _ in range(2)]
    t.start()
    for x in readers:
        x.start()
    for x in readers:
        x.join(30)
    stop.set()
    t.join(30)
    assert not errs
    assert not t.is_alive() and not any(x.is_alive() for x in readers)


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
    assert ("p9", 1) in r._cache and ("p0", 1) not in r._cache


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
