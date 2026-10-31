"""C15 S2a: the three prerequisite changes the seam's ``open_brief`` reads through: ``morning.card(now=)``, the
non-recording non-numbering band read, and ``ready.list_ready(conn=)``. Each default path is unchanged."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

import pytest

from levain import band_view
from levain.autonomic.durable_sqlite import read_transaction
from levain.consent.questions import QuestionStore
from levain.coordinator import morning, ready
from levain.coordinator.store import CoordStore

OPTS = [("a", "A"), ("b", "B")]
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path):
    (tmp_path / "dispatch").mkdir(mode=0o700)
    (tmp_path / "consent").mkdir(mode=0o700)
    st = CoordStore(tmp_path / "dispatch")
    tok = st.coord_take()
    ready.add(st, tok, "x1", repo="levain", title="t", who="operator", why="w")
    qs = QuestionStore(tmp_path / "consent")
    qs.file(agent="claude_code", agent_session_id="s", what="Which?", options=OPTS, rec="a", blocks="x", cwd="/tmp")
    return st, qs, tmp_path / "consent"


def _bytes(qdir):
    """The queue directory's logical content: every file name, and the full SQL dump of each database (the raw bytes
    move on a plain read: a WAL store's -shm is touched by any reader)."""
    out = {}
    for p in sorted(qdir.iterdir()):
        if p.name.endswith(("-wal", "-shm")):
            continue                                  # sidecars of any reader, including this probe
        if p.suffix == ".db":
            c = sqlite3.connect(p)
            try:
                out[p.name] = list(c.iterdump())
            finally:
                c.close()
        else:
            out[p.name] = p.read_bytes()
    return out


def _tables(qs):
    c = sqlite3.connect(qs.path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        c.close()


# ---------------------------------------------------------------------------------------------- 1. card(now=)
def test_card_with_now_is_deterministic_and_never_reads_the_wall_clock(world, monkeypatch):
    st, qs, qdir = world
    band_view.band_payload(qs, "v")                         # numbered already, so the card's band is a plain read
    monkeypatch.setattr(time, "time", lambda: (_ for _ in ()).throw(AssertionError("wall clock read")))
    a = morning.card(st, queue_dir=qdir, now=NOW)
    b = morning.card(st, queue_dir=qdir, now=NOW)
    assert a == b
    assert a["at"] == "2026-10-31T12:00:00.000000Z"
    assert a["errors"] == [] and a["band"]["error"] is None and a["band"]["shown"] == [1]


def test_card_with_now_reads_presence_at_now(world, monkeypatch):
    from levain.webpush.presence import set_word
    from levain.webpush.quote import Match

    st, qs, qdir = world
    seat = {"agent": "claude_code", "session": "s-1", "role": "seat"}
    t = NOW.timestamp() - 600
    set_word(qdir, "away", by=seat, said="out", now=t, match=Match(True, t - 5, "ok"))
    monkeypatch.setattr(time, "time", lambda: (_ for _ in ()).throw(AssertionError("wall clock read")))
    assert morning.card(st, queue_dir=qdir, now=NOW)["presence"]["said"] == "out"


def test_card_without_now_still_reads_the_wall_clock(world, monkeypatch):
    st, qs, qdir = world
    calls = []
    real = time.time
    monkeypatch.setattr(time, "time", lambda: calls.append(1) or real())
    c = morning.card(st, queue_dir=qdir)
    assert calls and c["at"].startswith("20")


def test_card_with_now_refuses_a_naive_datetime(world):
    st, qs, qdir = world
    with pytest.raises(Exception, match="aware"):
        morning.card(st, queue_dir=qdir, now=datetime(2026, 10, 31, 12, 0))


def test_card_with_now_does_not_number_the_band(world):
    st, qs, qdir = world
    before = _bytes(qdir)
    c = morning.card(st, queue_dir=qdir, now=NOW)
    assert "new (number pending)" in c["band"]["text"] and c["band"]["shown"] == []
    assert _bytes(qdir) == before and "band_items" not in _tables(qs)


# ---------------------------------------------------------------------------------------- 2. the band read
def test_readonly_band_writes_nothing_and_numbers_nothing(world):
    st, qs, qdir = world
    before = _bytes(qdir)
    r = band_view.band_snapshot_readonly(qs)
    assert _bytes(qdir) == before                           # no queue file changed, none created
    assert _tables(qs).isdisjoint({"band_items", "band_views", "band_closed_shown"})
    assert r["view_id"] is None and r["pending"] is True and r["read_only"] is True
    assert [it["n"] for it in r["items"]] == [None] and r["items"][0]["what"] == "Which?"
    assert r["items"][0]["actions"] == []


def test_the_same_probe_fails_on_the_recording_path(world):
    st, qs, qdir = world
    before = _bytes(qdir)
    p = band_view.band_payload(qs, "v")
    assert _bytes(qdir) != before                           # the probe can fail: band_payload writes
    assert {"band_items", "band_views"} <= _tables(qs)
    assert p["view_id"] == 1 and [it["n"] for it in p["items"]] == [1]


def test_render_full_default_numbers_and_numbering_false_does_not(world):
    st, qs, qdir = world
    before = _bytes(qdir)
    r = band_view.render_full(qs, numbering=False)
    assert r.shown == [] and r.pending and _bytes(qdir) == before
    r2 = band_view.render_full(qs)
    assert r2.shown == [1] and _bytes(qdir) != before


def test_readonly_band_after_numbering_lists_the_number(world):
    st, qs, qdir = world
    band_view.band_payload(qs, "v")
    before = _bytes(qdir)
    r = band_view.band_snapshot_readonly(qs)
    assert [it["n"] for it in r["items"]] == [1] and r["pending"] is False and r["seen_through"] == 1
    assert _bytes(qdir) == before


def test_readonly_band_of_an_absent_store_creates_nothing(tmp_path):
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    r = band_view.band_snapshot_readonly(QuestionStore(qdir))
    assert r["items"] == [] and r["store_id"] is None and list(qdir.iterdir()) == []


# ---------------------------------------------------------------------------------------- 3. list_ready(conn=)
def test_list_ready_with_conn_opens_nothing_and_matches_the_default(world, monkeypatch):
    st, qs, qdir = world
    default = ready.list_ready(st, all=True, generated="g")
    opens = []
    real = st.open_readonly
    monkeypatch.setattr(st, "open_readonly", lambda *a, **k: opens.append(1) or real(*a, **k))
    with read_transaction(st.open_readonly) as conn:
        assert len(opens) == 1
        shared = ready.list_ready(st, all=True, generated="g", conn=conn)
        assert len(opens) == 1                              # the board took no second open
    assert shared == default and [i["id"] for i in shared["items"]] == ["x1"]
    ready.list_ready(st, all=True, generated="g")
    assert len(opens) == 2                                  # the default path opens its own


def test_list_ready_with_conn_reads_the_callers_snapshot_not_a_later_write(world):
    st, qs, qdir = world
    tok = st.coord_take()
    with read_transaction(st.open_readonly) as conn:
        conn.execute("SELECT COUNT(*) FROM ready_items").fetchone()           # the snapshot starts at the first read
        ready.add(st, tok, "x2", repo="levain", title="t2", who="operator", why="w")   # committed after the snapshot
        held = ready.list_ready(st, all=True, generated="g", conn=conn)
    assert [i["id"] for i in held["items"]] == ["x1"]
    assert [i["id"] for i in ready.list_ready(st, all=True, generated="g")["items"]] == ["x1", "x2"]
