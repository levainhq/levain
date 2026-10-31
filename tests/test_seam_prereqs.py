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
from levain.coordinator.store import CoordError, CoordStore

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


# ---------------------------------------------------------------------- 4. no wall clock reaches the output
# the real world of a busy morning: an open question, an UNDECIDED consent request with a coordinator SIGN on it (and a
# levain sign request file of the same id, so the sign-menu expiry is judged), a budget-extension SIGN, a review item.
import json
import sys
import datetime as _dt
from datetime import timedelta

from levain.consent import sign
from levain.contracts.namespaces import REVIEW_CLEAR_NAMESPACE
from levain.coordinator import review
from tests.test_band_authority import ask, human_terminal, night, qdir, undecided  # noqa: F401 - fixtures
from tests.test_band_authority import cq  # noqa: F401
from tests.test_consent_queue import keys  # noqa: F401 - fixtures
from tests.test_coordinator_envelope import ctx, repo, snapshot  # noqa: F401 - fixtures


class WallClockRead(BaseException):
    """BaseException on purpose: no `except Exception` in the band, the card or the sources can swallow it."""


def _no_wall_clock(monkeypatch):
    """Every ``datetime.now()`` / ``utcnow()`` / ``today()`` and ``time.time()`` raises: the ``datetime`` name of every
    levain module that holds the real class (and the stdlib module's own, for a function-local import) is replaced by a
    subclass whose clock reads raise. ``time.monotonic()`` and ``time.time_ns()`` stay: neither reaches an output."""
    real = _dt.datetime

    class Boom(real):
        @classmethod
        def now(cls, tz=None):
            raise WallClockRead("datetime.now() read")

        @classmethod
        def utcnow(cls):
            raise WallClockRead("datetime.utcnow() read")

        @classmethod
        def today(cls):
            raise WallClockRead("datetime.today() read")

    def boom():
        raise WallClockRead("time.time() read")

    for name, mod in list(sys.modules.items()):
        if name.startswith(("levain", "tests")) and getattr(mod, "datetime", None) is real:
            monkeypatch.setattr(mod, "datetime", Boom)
    monkeypatch.setattr(_dt, "datetime", Boom)
    monkeypatch.setattr(time, "time", boom)


def _clone_request(qdir, src, new_id):
    """A second row in ``queue.db`` with ``new_id``, a copy of the request ``src`` (the SIGN's request, whose sign file
    the sign menu reads, is a consent request of its own id)."""
    with sqlite3.connect(qdir / "queue.db") as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(requests)")]
        row = dict(zip(cols, c.execute("SELECT * FROM requests WHERE id = ?", (src,)).fetchone()))
        row["id"] = new_id
        c.execute(f"INSERT INTO requests ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                  [row[k] for k in cols])


@pytest.fixture
def busy(night, cq, qdir):  # noqa: F811
    qs = QuestionStore(qdir)                                  # the DEFAULT clock: the wall clock
    ask(qs, "rename helper?")
    req = undecided(cq, night)                                # an UNDECIDED request with a coordinator SIGN on it
    review.push_sign(night.store, night.T, req.id, "land thread", rec="a: tests green")
    live = cq.file(agent="claude_code", agent_session_id="sess-live", tool_name="Bash",
                   tool_input={"command": "echo live"}, cwd=night.c.path, hold=timedelta(hours=3))   # a PENDING one
    lv = sign.file_levain_request(qdir, REVIEW_CLEAR_NAMESPACE, "levain review clear rv-" + "ab" * 16 + " open none",
                                  title="t", ttl_s=10 * 365 * 86400)    # open far past T: its expiry is judged
    _clone_request(qdir, live.id, lv)                         # and a SIGN on it: the sign menu reads its file
    review.push_sign(night.store, night.T, lv, "clear the item", rec="a: signed on the sheet")
    night.calls(7)                                            # past its budget: a budget-extension SIGN
    night.leg()
    review.file_know(night.store, night.T, "the coordinator noticed a thing", by="service")
    return CoordStore(night.store.directory), qs, qdir        # a CoordStore on the DEFAULT clock too


def test_card_band_and_ready_read_no_wall_clock_and_are_byte_identical(busy, monkeypatch):
    st, qs, qdir = busy
    T = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)      # an hour into the budget fixture's world (its stop is ahead)
    # everything lazily imported is imported BEFORE the clock is taken away (a lazy import under the patch would keep it)
    morning.card(st, queue_dir=qdir, now=T)
    band_view.band_snapshot_readonly(qs, now=T)
    _no_wall_clock(monkeypatch)
    cards = [morning.card(st, queue_dir=qdir, now=T) for _ in range(2)]
    bands = [band_view.band_snapshot_readonly(qs, now=T) for _ in range(2)]
    with read_transaction(st.open_readonly) as c:
        board = [ready.list_ready(st, all=True, generated="g", conn=c) for _ in range(2)]
    for pair in (cards, bands, board):
        assert json.dumps(pair[0], sort_keys=True) == json.dumps(pair[1], sort_keys=True)
    c = cards[0]
    # the world is not empty: the question, the SIGN on the request (its menu judged at T), the extension SIGN, the item
    assert c["errors"] == [] and c["band"]["error"] is None
    assert "rename helper?" in c["band"]["text"] and "extend a thread budget" in c["band"]["text"]
    assert "SIGN: land thread" in c["band"]["text"] and "NEEDS YOU · 4 open" in c["band"]["text"]
    assert len(c["review"]) == 1 and c["waiting"]["threads"]
    assert {i["kind"] for i in bands[0]["items"]} >= {"information", "authority"} and len(bands[0]["items"]) == 4
    assert bands[0]["failed"] is False and bands[0]["note"] != "" and bands[0]["pending"] is True


def test_the_same_probe_fails_without_now(busy, monkeypatch):
    st, qs, qdir = busy
    morning.card(st, queue_dir=qdir)                 # the lazy imports, with the clock still there
    _no_wall_clock(monkeypatch)
    with pytest.raises(WallClockRead):
        morning.card(st, queue_dir=qdir)
    with pytest.raises(WallClockRead):
        band_view.band_snapshot_readonly(qs)


# ------------------------------------------------------------------ 2/3/6. the connection, the stamp, the edges
def test_readonly_band_with_coord_conn_opens_no_second_coordinator_read(busy, monkeypatch):
    st, qs, qdir = busy
    T = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)
    opens = []
    real = st.open_readonly
    monkeypatch.setattr(CoordStore, "open_readonly", lambda self, *a, **k: opens.append(1) or real(*a, **k))
    own = band_view.band_snapshot_readonly(qs, now=T)
    assert opens                                            # the default path opens its own coordinator read
    opens.clear()
    with read_transaction(st.open_readonly) as conn:
        n = len(opens)
        shared = band_view.band_snapshot_readonly(qs, now=T, coord_conn=conn)
        assert len(opens) == n                              # none more: the caller's transaction is read
    assert json.dumps(shared, sort_keys=True) == json.dumps(own, sort_keys=True)


def test_card_hands_its_connection_and_now_to_the_band(busy, monkeypatch):
    st, qs, qdir = busy
    T = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)
    seen = {}
    real = band_view.render_full
    monkeypatch.setattr(band_view, "render_full", lambda *a, **k: seen.update(k) or real(*a, **k))
    morning.card(st, queue_dir=qdir, now=T)
    assert seen["now"] == T and seen["numbering"] is False and seen["coord_conn"] is not None


def test_list_ready_with_conn_refuses_to_stamp_itself(world):
    st, qs, qdir = world
    with read_transaction(st.open_readonly) as conn:
        for missing in (None, ""):
            with pytest.raises(CoordError, match="generated"):
                ready.list_ready(st, all=True, generated=missing, conn=conn)
    assert ready.list_ready(st, all=True)["generated"]       # no conn: the board stamps itself, as ever


def test_list_ready_with_conn_trusts_the_connection_a_tableless_one_reads_empty(tmp_path):
    """Documented in the docstring: with ``conn`` the caller does the table-presence check (the default path names a
    store it cannot open as ``unreadable``; a bare connection to a database with no board tables is just empty)."""
    (tmp_path / "dispatch").mkdir(mode=0o700)
    st = CoordStore(tmp_path / "dispatch")
    c = sqlite3.connect(":memory:")
    c.execute("BEGIN")
    out = ready.list_ready(st, all=True, generated="g", conn=c)
    assert out["items"] == [] and out["unreadable"] == []


def test_card_with_now_on_a_pre_band_store_creates_no_band_table(world):
    st, qs, qdir = world
    before = _bytes(qdir)
    assert _tables(qs).isdisjoint({"band_items", "band_views", "band_closed_shown"})
    morning.card(st, queue_dir=qdir, now=NOW)
    assert _tables(qs).isdisjoint({"band_items", "band_views", "band_closed_shown"}) and _bytes(qdir) == before


def test_readonly_band_of_an_unreadable_questions_store_raises_it_and_the_card_says_it(world):
    import os

    from levain.consent.questions import QuestionStoreError

    st, qs, qdir = world
    try:
        os.chmod(qdir, 0o555)                               # reachable by group or others: QuestionStore refuses it
        with pytest.raises(QuestionStoreError):
            band_view.band_snapshot_readonly(qs)
        c = morning.card(st, queue_dir=qdir, now=NOW)       # the card names it, never raises it
        assert c["band"]["error"].startswith("QuestionStoreError")
        os.chmod(qdir, 0o500)
        os.chmod(qs.path, 0o400)                            # a read-only file: sqlite would write its WAL index
        with pytest.raises(sqlite3.OperationalError):
            band_view.band_snapshot_readonly(qs)
        assert morning.card(st, queue_dir=qdir, now=NOW)["band"]["error"].startswith("OperationalError")
    finally:
        os.chmod(qdir, 0o700)
        os.chmod(qs.path, 0o600)
