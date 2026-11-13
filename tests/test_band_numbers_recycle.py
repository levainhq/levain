"""levain#3: band numbers recycle after a 14-day quarantine. A freed number goes back to the pool 14 days after it was
freed, a new row takes the LOWEST free number (else max+1), a number freed less than 14 days ago is never reissued, and a
typed number resolves only against OPEN rows (the TERMSIGN F1 footer view id stays on). Every test runs on a scratch
store under ``tmp_path`` with an injected clock; none touches a real Levain home."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from levain import band_view
from levain.consent.questions import QuestionStore

OPTS = [("fix", "fixed 30 s"), ("exp", "exponential")]
VIEWER = "claude_code:seat-1"
OTHER = "claude_code:seat-2"
T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, delta):
        self.now = self.now + delta


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir, clock):
    return QuestionStore(qdir, clock=clock)


@pytest.fixture
def env(qdir):
    return {"LEVAIN_HOME": str(qdir.parent)}


def ask(store, what):
    return store.file(agent="claude_code", agent_session_id="sess-1", what=what, options=OPTS, rec="exp",
                      blocks="the PR", cwd="/tmp/proj")


def see(store, viewer=VIEWER):
    """What a hook does: render the band (numbering what is new) and record the viewer's view of it."""
    r = band_view.render_full(store)
    seq = band_view.record_view(store, viewer, r.hwm, r.shown, gen=r.gen, block=r.text)
    return r, seq


def close(store, q):
    store.answer(q.id, option="fix", channel="cli")


def rows(store):
    with sqlite3.connect(store.path) as c:
        return c.execute("SELECT n, source, ref FROM band_items ORDER BY n").fetchall()


def number_of(store, q):
    return {ref: n for n, _src, ref in rows(store)}.get(q.id)


def retired(store):
    with sqlite3.connect(store.path) as c:
        return c.execute("SELECT n, source, ref, numbered_at, freed_at, retired_at, closed_shown_at "
                         "FROM band_items_retired ORDER BY n, numbered_at").fetchall()


def answered(store, q):
    return next(x for x in store.recent() if x.id == q.id).answer is not None


# --- (a) a number freed less than 14 days ago is never reissued -------------------------------------------------------
@pytest.mark.parametrize("wait, reissued", [(timedelta(days=13, hours=23, minutes=59), False),
                                           (timedelta(days=14), True),
                                           (timedelta(days=14, seconds=1), True),
                                           (timedelta(days=40), True)])
def test_a_number_is_reissued_only_once_14_days_have_passed_since_it_was_freed(store, clock, wait, reissued):
    q1, q2 = ask(store, "one"), ask(store, "two")
    see(store)
    assert (number_of(store, q1), number_of(store, q2)) == (1, 2)
    close(store, q1)                                    # freed at T0
    clock.advance(wait)
    q3 = ask(store, "three")
    see(store)
    assert number_of(store, q3) == (1 if reissued else 3)
    assert (number_of(store, q1) is None) == reissued    # the old row left band_items only when its number went back
    assert bool(retired(store)) == reissued


def test_a_closed_number_stays_held_while_young_whatever_else_is_numbered(store, clock):
    qs = [ask(store, f"q{i}") for i in range(4)]
    see(store)
    close(store, qs[0])
    clock.advance(timedelta(days=7))
    close(store, qs[1])
    clock.advance(timedelta(days=7, seconds=-1))        # qs[0]: 14d minus 1 s, qs[1]: 7d
    fresh = [ask(store, "x"), ask(store, "y")]
    see(store)
    assert [number_of(store, q) for q in fresh] == [5, 6]
    assert retired(store) == []


# --- (b) the lowest free number, else max+1 ---------------------------------------------------------------------------
def test_new_rows_take_the_lowest_free_numbers_then_max_plus_one(store, clock):
    qs = [ask(store, f"q{i}") for i in range(5)]
    see(store)
    for i in (3, 1, 4):                                 # numbers 4, 2, 5 freed (out of order)
        close(store, qs[i])
    clock.advance(timedelta(days=15))
    new = [ask(store, f"n{i}") for i in range(4)]
    see(store)
    assert [number_of(store, q) for q in new] == [2, 4, 5, 6]     # 2, 4, 5 recycled lowest first, then max+1
    assert sorted(n for n, *_ in rows(store)) == [1, 2, 3, 4, 5, 6]


def test_with_no_free_number_the_next_is_max_plus_one(store):
    qs = [ask(store, f"q{i}") for i in range(3)]
    see(store)
    q4 = ask(store, "four")
    see(store)
    assert [number_of(store, q) for q in (*qs, q4)] == [1, 2, 3, 4]


def test_the_top_number_freed_is_reissued_before_a_new_one_is_made(store, clock):
    a, b = ask(store, "a"), ask(store, "b")
    see(store)
    close(store, b)
    clock.advance(timedelta(days=14))
    c = ask(store, "c")
    d = ask(store, "d")
    see(store)
    assert (number_of(store, c), number_of(store, d)) == (2, 3)


def test_the_archive_keeps_what_the_number_meant(store, clock):
    q1 = ask(store, "one")
    see(store)
    numbered = rows(store)
    close(store, q1)
    freed = clock.now
    clock.advance(timedelta(days=20))
    q2 = ask(store, "two")
    see(store)
    (n, source, ref, numbered_at, freed_at, retired_at, shown_at), = retired(store)
    assert (n, source, ref) == (1, "question", q1.id) and numbered == [(1, "question", q1.id)]
    assert freed_at == band_view.utc_stamp(freed) and retired_at == band_view.utc_stamp(clock.now)
    assert numbered_at == band_view.utc_stamp(T0) and shown_at is None
    assert rows(store) == [(1, "question", q2.id)]


# --- (c) a typed number resolves only against open rows ---------------------------------------------------------------
def test_a_typed_number_whose_row_closed_and_was_not_reissued_is_refused(store, env, clock):
    q1, q2 = ask(store, "one"), ask(store, "two")
    see(store)
    close(store, q1)
    close(store, q2)
    clock.advance(timedelta(days=15))
    q3 = ask(store, "three")                            # takes 1; 2 is free and unissued
    see(store)
    assert number_of(store, q3) == 1 and number_of(store, q1) is None
    echo = band_view._seat_answer(store, VIEWER, "2a", env, None)
    assert "refused: 2 " in echo and "recorded: nothing" in echo
    assert not answered(store, q3)


def test_a_reissued_number_typed_against_the_new_view_reaches_the_new_row_only(store, env, clock):
    q1 = ask(store, "one")
    see(store)
    close(store, q1)
    clock.advance(timedelta(days=15))
    q2 = ask(store, "two")
    see(store)
    assert number_of(store, q2) == 1
    echo = band_view._seat_answer(store, VIEWER, "1a", env, None)
    assert "recorded: 1a" in echo
    assert answered(store, q2)
    old = next(x for x in store.recent() if x.id == q1.id)
    assert old.answer.value == "fix" and old.answer.answered_at < band_view.utc_stamp(clock.now)   # q1 untouched


def test_a_reply_typed_against_an_idle_viewers_old_view_is_refused_after_a_recycle(store, env, clock):
    """The stale-reply danger the quarantine exists for: seat 2 was last shown the band when 1 was q1. 1 is freed, held
    14 days, reissued to q2. Seat 2 types '1a' against ITS view: it must not land on q2."""
    q1 = ask(store, "one")
    see(store, OTHER)                                   # seat 2's newest view: [1]
    close(store, q1)
    clock.advance(timedelta(days=15))
    q2 = ask(store, "two")
    see(store, VIEWER)                                  # seat 1 sees the new band; seat 2 does not
    assert number_of(store, q2) == 1
    echo = band_view._seat_answer(store, OTHER, "1a", env, None)
    assert "refused: 1 was not open in the band you saw" in echo and "recorded: nothing" in echo
    assert not answered(store, q2)
    # and the same number from the viewer that WAS shown q2 still works
    assert "recorded: 1a" in band_view._seat_answer(store, VIEWER, "1a", env, None)
    assert answered(store, q2)


def test_the_footer_view_id_check_holds_across_a_recycle(store, clock):
    q1, keep = ask(store, "one"), ask(store, "keep")
    r1, v1 = see(store, "phone:dev-1")
    sid = band_view.QuestionStore.store_id.fget(store)
    close(store, q1)
    clock.advance(timedelta(days=15))
    q2 = ask(store, "two")
    r2, v2 = see(store, "phone:dev-1")
    assert v2 != v1 and number_of(store, q2) == 1 and number_of(store, keep) == 2
    with pytest.raises(band_view.StaleBand):            # the footer typed under the old view id: refused, nothing written
        band_view.answer_text(store, "1a", viewer="phone:dev-1", channel="phone", store_id=sid, view_id=v1)
    with pytest.raises(band_view.StaleBand):            # an UNRECYCLED number is refused by the view id alone
        band_view.answer_text(store, "2a", viewer="phone:dev-1", channel="phone", store_id=sid, view_id=v1)
    assert not answered(store, q2) and not answered(store, keep)
    done = band_view.answer_text(store, "1a", viewer="phone:dev-1", channel="phone", store_id=sid, view_id=v2)
    assert done.recorded and answered(store, q2)


def test_the_same_numbers_in_a_new_view_are_a_new_view_not_a_dedupe(store, clock):
    """A polling surface writes no row when nothing changed; a recycled number is a change."""
    q1 = ask(store, "one")
    r1, v1 = band_view.render_full(store), None
    v1 = band_view.record_view(store, "phone:dev-1", r1.hwm, r1.shown, gen=r1.gen, block=r1.text, dedupe=True)
    assert band_view.record_view(store, "phone:dev-1", r1.hwm, r1.shown, dedupe=True) == v1   # unchanged: no new row
    close(store, q1)
    clock.advance(timedelta(days=15))
    ask(store, "two")
    r2 = band_view.render_full(store)
    assert r2.shown == r1.shown == [1]
    assert band_view.record_view(store, "phone:dev-1", r2.hwm, r2.shown, gen=r2.gen, block=r2.text, dedupe=True) != v1


def test_the_viewers_delta_names_a_reissued_number_as_new_and_the_old_one_closed(store, clock):
    q1 = ask(store, "one")
    see(store)
    prev = band_view.last_view(store, VIEWER)
    close(store, q1)
    clock.advance(timedelta(days=15))
    ask(store, "two")
    r = band_view.render_full(store)
    assert r.shown == [1]
    line = band_view._delta_line(store, prev, r.shown)
    assert "new 1" in line and "closed 1 (answered via cli)" in line


def test_a_closed_notice_unseen_for_14_days_is_dropped_never_shown_beside_a_reissued_number(store, clock):
    q1, q2 = ask(store, "one"), ask(store, "two")
    band_view.render_full(store)
    close(store, q1)                                    # its `closed 1` notice is never consumed
    assert band_view.render_full(store).closed == [1]
    clock.advance(timedelta(days=15))
    ask(store, "three")
    r = band_view.render_full(store)
    assert r.closed == [] and "closed" not in r.text.splitlines()[0]
    assert r.shown == [1, 2] and number_of(store, q2) == 2


def test_a_snooze_of_a_free_number_says_so(store, clock):
    q1, q2 = ask(store, "one"), ask(store, "two")
    see(store)
    close(store, q1)
    close(store, q2)
    clock.advance(timedelta(days=15))
    ask(store, "three")
    see(store)
    with pytest.raises(band_view.ConsentError, match="not in use"):
        band_view.question_of_band_number(store, 2)
    with pytest.raises(band_view.ConsentError, match="never shown"):
        band_view.question_of_band_number(store, 9)


# --- (d) every other edit of the number space still aborts ------------------------------------------------------------
def _recycled_store(store, clock):
    q1, q2 = ask(store, "one"), ask(store, "two")
    see(store)
    band_view.mark_closed_shown(store, [1])
    close(store, q1)
    clock.advance(timedelta(days=15))
    ask(store, "three")
    see(store)
    return q1, q2


@pytest.mark.parametrize("sql", [
    "UPDATE band_items SET ref = 'x' WHERE n = 2",
    "UPDATE band_items SET n = 9 WHERE n = 2",
    "UPDATE band_items SET numbered_at = 'x' WHERE n = 2",
    "DELETE FROM band_items WHERE n = 2",                                  # open: nothing archived
    "INSERT OR REPLACE INTO band_items (n, source, ref, numbered_at) VALUES (2, 'question', 'z', 't')",
    "INSERT INTO band_items (n, source, ref, numbered_at) VALUES (1, 'question', 'z', 't')",   # a held number
    "UPDATE band_items_retired SET ref = 'x'",
    "DELETE FROM band_items_retired",
    "INSERT INTO band_items_retired (n, source, ref, numbered_at, freed_at, retired_at) VALUES "
    "(77, 'question', 'q', 't', 't', 't')",                                # an archive row of no row
    "UPDATE band_closed_shown SET at = 'x'",
    "DELETE FROM band_closed_shown",
    "UPDATE band_freed SET freed_at = 'x'",
    "DELETE FROM band_freed",
])
def test_a_direct_edit_of_the_number_space_still_aborts(store, clock, sql):
    _recycled_store(store, clock)
    band_view.allocate(store, "host", ["h"])
    band_view.release(store, "host", ["h"])
    with sqlite3.connect(store.path) as c:
        c.execute("INSERT OR IGNORE INTO band_closed_shown (n, at) VALUES (2, 't')")
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(sql)


def test_a_row_cannot_leave_behind_an_archive_row_of_a_young_number(store, clock):
    """The one transition (a delete behind an archive row) still needs the archive row to say 14 days had passed."""
    q1 = ask(store, "one")
    see(store)
    close(store, q1)
    with sqlite3.connect(store.path) as c:
        c.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("DELETE FROM band_items WHERE n = 1")
        c.execute("INSERT INTO band_items_retired (n, source, ref, numbered_at, freed_at, retired_at) "
                  "SELECT n, source, ref, numbered_at, '2026-11-01T12:00:00.000000Z', '2026-11-15T11:59:59.000000Z' "
                  "FROM band_items WHERE n = 1")
        with pytest.raises(sqlite3.DatabaseError):                         # 13d23h59m59s on the archive row's own stamps
            c.execute("DELETE FROM band_items WHERE n = 1")


# --- sources: authority items and host items --------------------------------------------------------------------------
def test_a_host_number_is_freed_only_by_a_release_and_then_after_14_days(store, clock):
    got = band_view.allocate(store, "host", ["np-a", "np-b"], now=clock.now)
    assert got == {"np-a": 1, "np-b": 2}
    clock.advance(timedelta(days=60))                   # no release: nothing is ever freed
    assert band_view.allocate(store, "host", ["np-c"]) == {"np-c": 3}
    assert band_view.release(store, "host", ["np-a", "np-nobody"]) == {"np-a": 1}      # freed now (T0 + 60d)
    first = band_view.release(store, "host", ["np-a"], now=clock.now + DAY)            # idempotent: first stamp kept
    assert first == {"np-a": 1}
    clock.advance(timedelta(days=14, seconds=-1))
    assert band_view.allocate(store, "host", ["np-d"]) == {"np-d": 4}
    clock.advance(timedelta(seconds=1))
    assert band_view.allocate(store, "host", ["np-e"]) == {"np-e": 1}                  # 14d0m after the release
    assert band_view.allocate(store, "host", ["np-b"]) == {"np-b": 2}                  # never released: kept
    assert band_view.allocate(store, "host", ["np-e", "np-d"]) == {"np-e": 1, "np-d": 4}


def test_a_released_ref_asked_for_again_keeps_its_number_while_it_is_asked_for(store, clock):
    band_view.allocate(store, "host", ["np-a", "np-b"])
    band_view.release(store, "host", ["np-a", "np-b"])
    clock.advance(timedelta(days=30))
    # np-b is in use by this very call: not retired out from under the caller; np-a is, and np-z takes its number
    assert band_view.allocate(store, "host", ["np-b", "np-z"]) == {"np-b": 2, "np-z": 1}


def test_release_takes_only_an_allocatable_source_and_real_refs(store):
    with pytest.raises(ValueError):
        band_view.release(store, "question", ["x"])
    with pytest.raises(ValueError):
        band_view.release(store, "host", [""])
    assert band_view.release(store, "host", []) == {}


def test_an_authority_number_is_freed_only_when_its_source_was_read_and_it_is_not_open(store, clock):
    key = ("consent", "req-1")
    band_view._number(store, [key], band_view.time.monotonic() + 2)
    band_view.mark_closed_shown(store, [1])
    clock.advance(timedelta(days=15))
    # the source not read in full (no `gone` key): held, however old
    assert band_view._number(store, [("host", "h1")], band_view.time.monotonic() + 2) == {("host", "h1"): 2}
    assert [r[0] for r in rows(store)] == [1, 2]
    # read in full and not open: retired and the lowest free number goes to the next row
    assert band_view._number(store, [("host", "h2")], band_view.time.monotonic() + 2, None, [key]) == {("host", "h2"): 1}
    assert rows(store) == [(1, "host", "h2"), (2, "host", "h1")]
    (n, source, ref, _numbered, freed, _retired, shown), = retired(store)
    assert (n, source, ref) == (1, "consent", "req-1") and shown == freed


class _Authority:
    """A stand-in for the authority sources: the consent items open now, read in full."""
    def __init__(self, clock):
        self.refs: list[str] = []
        self.clock = clock

    def __call__(self, *a, **k):
        stamp = band_view.utc_stamp(self.clock.now)
        return band_view.Extras(items=[band_view.Extra("consent", r, stamp, "t", "w", ("a", "b"), "a", "a", "b", "o")
                                       for r in self.refs], complete={"consent"})


def test_an_authority_number_is_recycled_through_the_real_render_and_a_stale_view_cannot_reach_it(
        store, clock, monkeypatch):
    auth = _Authority(clock)
    monkeypatch.setattr(band_view, "read_extras", auth)
    auth.refs = ["req-1"]
    r, _ = see(store, OTHER)                                      # seat 2 is shown req-1 as 1
    assert r.shown == [1]
    auth.refs = []
    r = band_view.render_full(store)
    assert r.closed == [1]
    band_view.mark_closed_shown(store, [1])
    clock.advance(timedelta(days=14))
    auth.refs = ["req-2"]
    r, _ = see(store, VIEWER)
    assert r.shown == [1] and rows(store) == [(1, "consent", "req-2")]
    assert [x[2] for x in retired(store)] == ["req-1"]
    # the viewer shown req-2 reaches it by 1; the one shown req-1 does not (its view is older than the new row)
    assert set(band_view._extras_of(store, band_view.last_view(store, VIEWER))[0]) == {1}
    assert band_view._extras_of(store, band_view.last_view(store, OTHER)) == ({}, set())


# --- (e) a store at the PREVIOUS schema migrates, twice, to the same thing --------------------------------------------
# the DDL of origin/main 8d21607f2 (`git show origin/main:levain/band_view.py`, BAND_SCHEMA), kept verbatim
PREVIOUS_DDL = (
    "CREATE TABLE IF NOT EXISTS band_items (n INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL CHECK (source "
    "IN ('question','consent','sign','extend','host')), ref TEXT NOT NULL, numbered_at TEXT NOT NULL, "
    "UNIQUE (source, ref))",
    "CREATE TABLE IF NOT EXISTS band_closed_shown (n INTEGER PRIMARY KEY REFERENCES band_items(n), at TEXT NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS band_items_frozen BEFORE UPDATE ON band_items "
    "BEGIN SELECT RAISE(ABORT, 'band numbers are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_items_kept BEFORE DELETE ON band_items "
    "BEGIN SELECT RAISE(ABORT, 'band numbers are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_items_no_replace BEFORE INSERT ON band_items "
    "WHEN EXISTS (SELECT 1 FROM band_items WHERE (source = NEW.source AND ref = NEW.ref) OR n = NEW.n) "
    "BEGIN SELECT RAISE(ABORT, 'a band number is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS band_closed_shown_frozen BEFORE UPDATE ON band_closed_shown "
    "BEGIN SELECT RAISE(ABORT, 'band numbers are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_closed_shown_kept BEFORE DELETE ON band_closed_shown "
    "BEGIN SELECT RAISE(ABORT, 'band numbers are append-only'); END",
)


def _previous_store(store, clock):
    """A store as the previous code left it: three numbered questions (one answered and its notice shown), a host row."""
    qs = [ask(store, f"q{i}") for i in range(3)]
    stamp = band_view.utc_stamp(T0)
    with sqlite3.connect(store.path) as c:
        for stmt in PREVIOUS_DDL:
            c.execute(stmt)
        for q in qs:
            c.execute("INSERT INTO band_items (source, ref, numbered_at) VALUES ('question', ?, ?)", (q.id, stamp))
        c.execute("INSERT INTO band_items (source, ref, numbered_at) VALUES ('host', 'np-a', ?)", (stamp,))
        c.execute("INSERT INTO band_closed_shown (n, at) VALUES (1, ?)", (stamp,))
    store.answer(qs[0].id, option="fix", channel="cli")
    return qs


def _shape(store):
    with sqlite3.connect(store.path) as c:
        return (c.execute("SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'band%' ORDER BY name").fetchall(),
                c.execute("SELECT * FROM band_items ORDER BY n").fetchall(),
                c.execute("SELECT * FROM sqlite_sequence WHERE name = 'band_items'").fetchall())


def test_a_store_at_the_previous_schema_migrates_in_place_and_twice_the_same(store, clock):
    qs = _previous_store(store, clock)
    before_rows = rows(store)
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'band_items_retired'").fetchone() is None
        old_trigger = c.execute("SELECT sql FROM sqlite_master WHERE name = 'band_items_kept'").fetchone()[0]
    assert "band_items_retired" not in old_trigger
    new = ask(store, "new")
    band_view._number(store, [new.id], band_view.time.monotonic() + 2)             # the migrating write
    once = _shape(store)
    band_view._number(store, [new.id], band_view.time.monotonic() + 2)
    assert _shape(store) == once                                                    # idempotent
    names = {r[1] for r in once[0]}
    assert {"band_items_retired", "band_freed", "band_items_retired_of_a_row"} <= names
    assert dict((r[1], r[2]) for r in once[0])["band_items_kept"].count("band_items_retired") == 1
    assert rows(store)[:4] == before_rows                                           # every number kept, none moved
    assert number_of(store, new) == 5 and once[2] == [("band_items", 5)]
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT value FROM meta WHERE key = 'band_numbers'").fetchone() == ("recycle-14d-1",)
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("DELETE FROM band_items WHERE n = 1")                         # still guarded after migration
    clock.advance(timedelta(days=15))                                               # q0 was answered at T0
    q5 = ask(store, "five")
    band_view._number(store, [q5.id], band_view.time.monotonic() + 2)
    assert number_of(store, q5) == 1 and number_of(store, qs[0]) is None
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT COUNT(*) FROM band_closed_shown WHERE n = 1").fetchone() == (0,)
        assert c.execute("SELECT closed_shown_at FROM band_items_retired WHERE n = 1").fetchone() == (band_view.utc_stamp(T0),)


def test_an_older_binary_on_a_migrated_store_numbers_max_plus_one_and_breaks_nothing(store, clock):
    q1 = ask(store, "one")
    see(store)
    close(store, q1)
    clock.advance(timedelta(days=15))
    ask(store, "two")
    see(store)                                          # recycled: 1 is q2 now; 1 had been retired
    with sqlite3.connect(store.path) as c:
        for stmt in PREVIOUS_DDL:                       # what the previous binary runs before every write
            c.execute(stmt)
        assert "band_items_retired" in c.execute("SELECT sql FROM sqlite_master WHERE name = 'band_items_kept'").fetchone()[0]
        c.execute("INSERT INTO band_items (source, ref, numbered_at) VALUES ('host', 'old-binary', 't')")
        assert c.execute("SELECT n FROM band_items WHERE ref = 'old-binary'").fetchone() == (2,)   # max+1, no collision


# --- (f) two allocations racing never issue one number twice ----------------------------------------------------------
def test_racing_allocations_never_issue_a_number_twice(qdir, clock):
    s0 = QuestionStore(qdir, clock=clock)
    band_view.allocate(s0, "host", [f"seed-{i}" for i in range(6)])
    band_view.release(s0, "host", [f"seed-{i}" for i in range(6)])
    clock.advance(timedelta(days=15))                   # six free numbers waiting in the pool
    results: list[dict[str, int]] = []
    errors: list[BaseException] = []
    gate = threading.Barrier(4)

    def worker(w):
        s = QuestionStore(qdir, clock=clock)
        gate.wait()
        try:
            for i in range(5):
                results.append(band_view.allocate(s, "host", [f"w{w}-{i}"], deadline=band_view.time.monotonic() + 20))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    got = {ref: n for r in results for ref, n in r.items()}
    assert len(got) == 20 and len(set(got.values())) == 20                 # one number per ref, no number twice
    assert sorted(got.values()) == list(range(1, 21))                      # the 6 recycled lowest first, then 7..20
    assert sorted(n for n, *_ in rows(s0)) == list(range(1, 21))


def test_two_writers_allocating_the_same_ref_get_one_number(qdir, clock):
    s0 = QuestionStore(qdir, clock=clock)
    out: list[int] = []
    gate = threading.Barrier(2)

    def worker():
        s = QuestionStore(qdir, clock=clock)
        gate.wait()
        out.append(band_view.allocate(s, "host", ["same"], deadline=band_view.time.monotonic() + 20)["same"])

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert out == [1, 1] and rows(s0) == [(1, "host", "same")]
