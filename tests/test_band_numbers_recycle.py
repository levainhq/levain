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
    """What a hook does: render the band (numbering what is new) and record the viewer's view of it, with the lives the
    render read (BAND-FIX1)."""
    r = band_view.render_full(store)
    seq = band_view.record_view(store, viewer, r.hwm, r.shown, gen=r.gen, block=r.text, lives=r.lives)
    return r, seq


def deliver(store):
    """What a delivered hook render does with its `closed N` notices: acknowledges them (their rows, BAND-FIX1)."""
    r = band_view.render_full(store)
    band_view.mark_closed_shown(store, r.closed)
    return r


def close(store, q, channel="cli"):
    """Answer ``q`` and deliver its `closed N` notice at the same clock reading: a question's number is freed only once
    both have happened (BAND-FIX1: never retired with its notice unseen)."""
    store.answer(q.id, option="fix", channel=channel)
    deliver(store)


def answer_only(store, q):
    store.answer(q.id, option="fix", channel="cli")


def life_of(store, n):
    with sqlite3.connect(store.path) as c:
        row = c.execute("SELECT COALESCE(life, 0) FROM band_items WHERE n = ?", (n,)).fetchone()
    return row[0] if row else None


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
    assert numbered_at == band_view.utc_stamp(T0) and shown_at == band_view.utc_stamp(freed)
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
    v1 = band_view.record_view(store, "phone:dev-1", r1.hwm, r1.shown, gen=r1.gen, block=r1.text, dedupe=True,
                               lives=r1.lives)
    assert band_view.record_view(store, "phone:dev-1", r1.hwm, r1.shown, dedupe=True, lives=r1.lives) == v1   # no new row
    close(store, q1)
    clock.advance(timedelta(days=15))
    ask(store, "two")
    r2 = band_view.render_full(store)
    assert r2.shown == r1.shown == [1]
    assert r2.lives != r1.lives                                                     # the same number, another row
    assert band_view.record_view(store, "phone:dev-1", r2.hwm, r2.shown, gen=r2.gen, block=r2.text, dedupe=True,
                                 lives=r2.lives) != v1


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


def test_a_closed_notice_unseen_for_14_days_is_kept_and_its_number_held_until_it_is_shown(store, clock):
    """complement MED 2 / deepseek MED 2: a question answered while nobody rendered for 15 days keeps its number and its
    `closed N` notice: the next render shows the notice (never swallowed), and the number is freed 14 days after it."""
    q1, q2 = ask(store, "one"), ask(store, "two")
    band_view.render_full(store)
    answer_only(store, q1)                              # its `closed 1` notice is not consumed
    clock.advance(timedelta(days=15))
    q3 = ask(store, "three")
    r = band_view.render_full(store)                    # a numbering write: q1 is NOT retired, its notice is shown
    assert r.closed == [1] and "closed 1" in r.text.splitlines()[0]
    assert number_of(store, q1) == 1 and number_of(store, q3) == 3 and retired(store) == []
    band_view.mark_closed_shown(store, r.closed)
    clock.advance(timedelta(days=14, seconds=-1))
    q4 = ask(store, "four")
    see(store)
    assert number_of(store, q4) == 4 and number_of(store, q1) == 1  # 14 d after the notice, less 1 s: held
    clock.advance(timedelta(seconds=1))
    q5 = ask(store, "five")
    see(store)
    assert number_of(store, q5) == 1 and number_of(store, q2) == 2


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
        c.execute("INSERT INTO band_items_retired (n, source, ref, numbered_at, freed_at, retired_at, life) "
                  "SELECT n, source, ref, numbered_at, '2026-11-01T12:00:00.000000Z', '2026-11-15T11:59:59.000000Z', "
                  "life FROM band_items WHERE n = 1")
        with pytest.raises(sqlite3.DatabaseError):                         # 13d23h59m59s on the archive row's own stamps
            c.execute("DELETE FROM band_items WHERE n = 1")
        with pytest.raises(sqlite3.DatabaseError):                         # an archive row naming another life: refused
            c.execute("INSERT INTO band_items_retired (n, source, ref, numbered_at, freed_at, retired_at, life) "
                      "SELECT n, source, ref, numbered_at, '2026-11-01T12:00:00.000000Z', "
                      "'2026-11-15T12:00:00.000000Z', life + 99 FROM band_items WHERE n = 1")


# --- sources: authority items and host items --------------------------------------------------------------------------
def test_a_host_number_is_freed_only_by_a_release_and_then_after_14_days(store, clock):
    got = band_view.allocate(store, "host", ["np-a", "np-b"], now=clock.now)
    assert got == {"np-a": 1, "np-b": 2}
    clock.advance(timedelta(days=60))                   # no release: nothing is ever freed
    assert band_view.allocate(store, "host", ["np-c"]) == {"np-c": 3}
    assert band_view.release(store, "host", ["np-a", "np-nobody"]) == {"np-a": 1}      # freed now (T0 + 60d)
    clock.advance(DAY)
    first = band_view.release(store, "host", ["np-a"])                                 # idempotent: first stamp kept
    assert first == {"np-a": 1}
    clock.advance(-DAY)
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
    band_view.mark_closed_shown(store, [band_view.ClosedNotice(1, "consent", "req-1", life_of(store, 1))])
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
    band_view.mark_closed_shown(store, r.closed)
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
        assert c.execute("SELECT value FROM meta WHERE key = 'band_numbers'").fetchone() == ("recycle-14d-2",)
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


# --- BAND-FIX1 (levain#3 L3 round 1): a number's identity is (n, life), never a timestamp -----------------------------
def test_codex_high_1_a_view_committed_after_its_number_was_reissued_still_names_the_row_it_showed(store, env, clock, qdir):
    """codex HIGH 1: the desk renders q1 as 1 and stages the view; q1 closes, 1 is retired and reissued to q2; THEN the
    Stop hook commits the old view (its stamp now later than q2's numbering). '1a' typed against it is refused."""
    q1 = ask(store, "one")
    old = band_view.render_full(store)                             # render: 1 is q1
    band_view._stage_view(qdir, "sess-desk", old, old.text)
    close(store, q1)                                               # close (and its notice shown)
    clock.advance(timedelta(days=15))
    q2 = ask(store, "two")
    see(store, OTHER)                                              # retire and reissue: 1 is q2
    assert number_of(store, q2) == 1 and number_of(store, q1) is None
    clock.advance(timedelta(hours=1))
    assert band_view._stop_desk(qdir, store, VIEWER, {"session_id": "sess-desk"}, old.text) is None   # the late commit
    view = band_view.last_view(store, VIEWER)
    assert view.open_list == (1,) and dict(view.lives) == dict(old.lives) != {1: life_of(store, 1)}
    echo = band_view._seat_answer(store, VIEWER, "1a", env, None)
    assert "refused: 1 was not open in the band you saw" in echo and "recorded: nothing" in echo
    assert not answered(store, q2)


def test_codex_high_1_the_same_late_commit_through_record_view_directly(store, env, clock):
    q1 = ask(store, "one")
    old = band_view.render_full(store)
    close(store, q1)
    clock.advance(timedelta(days=15))
    q2 = ask(store, "two")
    see(store, OTHER)
    band_view.record_view(store, VIEWER, old.hwm, old.shown, gen=old.gen, block=old.text, lives=old.lives)
    assert "recorded: nothing" in band_view._seat_answer(store, VIEWER, "1a", env, None)
    assert not answered(store, q2)


def test_complement_med_1_a_commit_clock_ahead_of_the_render_and_a_clock_rollback_cannot_reach_a_reissued_number(
        store, env, clock):
    """complement MED 1 (and a clock rollback): the viewer's view is committed by a clock AHEAD of the one that later
    numbers the reissue. Comparing stamps would call the reissue older than the view; comparing lives does not."""
    q1 = ask(store, "one")
    r_v = band_view.render_full(store)                             # render clock: T0
    clock.advance(timedelta(days=30))                              # the commit clock is ahead
    band_view.record_view(store, VIEWER, r_v.hwm, r_v.shown, gen=r_v.gen, lives=r_v.lives)
    clock.now = T0                                                 # ROLLBACK to T0
    close(store, q1)
    clock.advance(timedelta(days=15))                              # T0 + 15 d: earlier than the view's own stamp
    q2 = ask(store, "two")
    see(store, OTHER)
    with sqlite3.connect(store.path) as c:
        (view_at,), = c.execute("SELECT at FROM band_views WHERE viewer = ?", (VIEWER,)).fetchall()
        (numbered_at,), = c.execute("SELECT numbered_at FROM band_items WHERE n = 1").fetchall()
    assert number_of(store, q2) == 1 and numbered_at < view_at   # a timestamp test would let '1a' through
    echo = band_view._seat_answer(store, VIEWER, "1a", env, None)
    assert "refused: 1 was not open in the band you saw" in echo and not answered(store, q2)


def test_the_quarantine_is_judged_by_the_clock_read_inside_the_write_never_the_renders_now(store, clock):
    """A render's ``now`` (its start, or a caller's) never decides a retirement: a render handed a ``now`` 30 days ahead
    retires nothing the store clock does not say is due; one handed a ``now`` behind does not hold back what is."""
    q1, _ = ask(store, "one"), ask(store, "two")
    see(store)
    close(store, q1)
    clock.advance(timedelta(days=2))
    q3 = ask(store, "three")
    band_view.render_full(store, now=clock.now + timedelta(days=30))
    assert number_of(store, q3) == 3 and number_of(store, q1) == 1 and retired(store) == []
    clock.advance(timedelta(days=12))                              # 14 d by the store clock
    q4 = ask(store, "four")
    band_view.render_full(store, now=T0)
    assert number_of(store, q4) == 1


def test_codex_high_2_a_stale_closed_acknowledgement_cannot_mark_a_reissued_number(store, clock):
    """codex HIGH 2 (the ABA): render A carries q1's `closed 1`; another render acknowledges it, 1 is retired and
    reissued to q2; then A's delivery acknowledges ITS notice: nothing is recorded for q2."""
    q1 = ask(store, "one")
    see(store)
    answer_only(store, q1)
    a = band_view.render_full(store)
    assert a.closed == [1] and (a.closed[0].source, a.closed[0].ref) == ("question", q1.id)
    deliver(store)                                                 # another seat delivers the same notice
    clock.advance(timedelta(days=15))
    q2 = ask(store, "two")
    see(store)
    assert number_of(store, q2) == 1
    band_view.mark_closed_shown(store, a.closed)                   # A's late acknowledgement
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT COUNT(*) FROM band_closed_shown WHERE n = 1").fetchone() == (0,)
    answer_only(store, q2)
    r = band_view.render_full(store)
    assert r.closed == [1] and r.closed[0].ref == q2.id            # q2's own notice is still owed, not suppressed


def test_a_bare_number_is_not_an_acknowledgement(store):
    q1 = ask(store, "one")
    see(store)
    answer_only(store, q1)
    with pytest.raises(TypeError):
        band_view.mark_closed_shown(store, [1])


def test_codex_med_3_identity_is_source_and_ref_a_sign_request_id_equal_to_a_question_id_is_another_item(store):
    """codex MED 3: a notice names (source, ref, life), so a sign request whose id equals a question's is never taken
    for it, even where both rows predate lives (life 0)."""
    band_view._number(store, [("host", "seed")], band_view.time.monotonic() + 2)   # the schema, migrated
    with sqlite3.connect(store.path) as c:
        c.execute("INSERT INTO band_items (source, ref, numbered_at) VALUES ('sign', 'same-id', 't')")   # life NULL
        (n,), = c.execute("SELECT n FROM band_items WHERE ref = 'same-id'").fetchall()
    as_question = band_view.ClosedNotice(n, "question", "same-id", 0)
    as_sign = band_view.ClosedNotice(n, "sign", "same-id", 0)
    with sqlite3.connect(store.path) as c:
        assert band_view._current_notices(c, [as_question, as_sign]) == [as_sign]
    band_view.mark_closed_shown(store, [as_question])
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT COUNT(*) FROM band_closed_shown").fetchone() == (0,)
    band_view.mark_closed_shown(store, [as_sign])
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT n FROM band_closed_shown").fetchall() == [(n,)]


def test_codex_low_4_release_checks_every_ref_before_it_deduplicates(store):
    for bad in ([[]], [{}], ["ok", []], [None], [""]):
        with pytest.raises(ValueError, match="non-empty string"):
            band_view.release(store, "host", bad)


def test_deepseek_med_3_a_delta_names_the_exact_life_the_previous_view_showed_and_never_guesses(store, clock):
    """deepseek MED 3: 1 was q1 (answered via cli, retired), then q2 (answered via phone, retired). A previous view that
    recorded q1's life says q1's channel; one with no lives says "gone", never the newest retired life."""
    q1 = ask(store, "one")
    see(store)
    with_lives = band_view.last_view(store, VIEWER)
    sid = with_lives.store_id
    close(store, q1, channel="cli")
    clock.advance(timedelta(days=15))
    q2 = ask(store, "two")
    see(store, OTHER)
    close(store, q2, channel="phone")
    clock.advance(timedelta(days=15))
    ask(store, "three")
    r = band_view.render_full(store)
    assert [x[2] for x in retired(store)] == [q1.id, q2.id] and r.shown == [1]
    line = band_view._delta_line(store, with_lives, r.shown, lives=r.lives)
    assert "closed 1 (answered via cli)" in line and "new 1" in line
    legacy = band_view.View(1, (1,), sid, None)
    assert "closed 1 (gone)" in band_view._delta_line(store, legacy, r.shown, lives=r.lives)


def test_deepseek_med_5_the_delta_reads_what_replaced_a_number_and_how_it_closed_in_one_snapshot(store, clock,
                                                                                                monkeypatch):
    q1 = ask(store, "one")
    see(store)
    prev = band_view.last_view(store, VIEWER)
    close(store, q1)
    clock.advance(timedelta(days=15))
    ask(store, "two")
    r = band_view.render_full(store)
    real, peeks = store._peek, []

    def counted():
        peeks.append(1)
        return real()
    monkeypatch.setattr(store, "_peek", counted)
    monkeypatch.setattr(band_view, "answer_badges", lambda *a, **k: {})
    line = band_view._delta_line(store, prev, r.shown)            # no lives given: the store's, in that same snapshot
    assert "new 1" in line and "closed 1 (answered via cli)" in line
    assert len(peeks) == 1


@pytest.mark.parametrize("answered_at, due", [("2026-11-01 12:00:00", True), ("2026-11-01T12:00:00+00:00", True),
                                              ("2026-11-01T12:00:00.000000Z", True), ("2026-11-01", True),
                                              ("2026-11-01T12:00:00 UTC", False), ("not a time", False)])
def test_deepseek_med_4_an_answer_stamp_in_another_format_is_compared_by_julianday_and_never_aborts_numbering(
        store, clock, answered_at, due):
    q1 = ask(store, "one")
    see(store)
    with sqlite3.connect(store.path) as c:                         # another writer's format for answered_at
        cols = [r[1] for r in c.execute("PRAGMA table_info(answers)")]
        row = {"question_id": q1.id, "answered_at": answered_at, "channel": "cli", "kind": "option", "value": "fix"}
        use = [k for k in cols if k in row]
        c.execute(f"INSERT INTO answers ({', '.join(use)}) VALUES ({', '.join('?' * len(use))})", [row[k] for k in use])
    deliver(store)                                                 # its notice shown at T0
    clock.advance(timedelta(days=15))
    q2 = ask(store, "two")
    r = band_view.render_full(store)                               # the numbering write commits either way
    assert number_of(store, q2) == (1 if due else 2) and not r.pending


def test_complement_low_3_one_answer_per_question_so_the_retirement_join_is_one_row(store):
    q1 = ask(store, "one")
    answer_only(store, q1)
    with sqlite3.connect(store.path) as c:
        pk = [r[1] for r in c.execute("PRAGMA table_info(answers)") if r[5]]
        assert pk == ["question_id"]
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO answers (question_id, answered_at, channel, kind, value) "
                      "SELECT question_id, answered_at, channel, kind, value FROM answers")


def test_lives_only_grow_and_a_new_row_never_takes_a_life_twice(store, clock):
    qs = [ask(store, f"q{i}") for i in range(3)]
    see(store)
    lives = [life_of(store, n) for n in (1, 2, 3)]
    assert lives == sorted(lives) and len(set(lives)) == 3 and min(lives) >= 1
    close(store, qs[0])
    clock.advance(timedelta(days=15))
    ask(store, "again")
    see(store)
    assert life_of(store, 1) > max(lives)                          # the reissued 1 is a new life
    with sqlite3.connect(store.path) as c:
        for sql in ("UPDATE band_lives SET last = last - 1", "UPDATE band_lives SET last = last",
                    "DELETE FROM band_lives", "INSERT INTO band_lives (one, last) VALUES (1, 99)"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)


def test_a_view_from_before_lives_is_stale_only_for_a_number_retired_since(store, env, clock):
    """A view written without lives (before the migration) fails closed for exactly the numbers retired since, and keeps
    answering every number that was never retired."""
    q1, q2 = ask(store, "one"), ask(store, "two")
    r = band_view.render_full(store)
    band_view.record_view(store, VIEWER, r.hwm, r.shown, gen=r.gen)   # no lives: a view as an older binary wrote it
    assert band_view.last_view(store, VIEWER).lives is None
    close(store, q1)
    clock.advance(timedelta(days=15))
    q3 = ask(store, "three")
    see(store, OTHER)
    assert number_of(store, q3) == 1
    echo = band_view._seat_answer(store, VIEWER, "1a 2a", env, None)
    assert "refused: 1 was not open in the band you saw" in echo and "2a" in echo.split("refused")[0]
    assert answered(store, q2) and not answered(store, q3)


# --- BAND-FIX1 migrations: the main schema (8d21607f2) and levain#3's first cut (8319d11b4), each twice ---------------
# the band DDL of 8319d11b4 (`git show 8319d11b4:levain/band_view.py`, BAND_SCHEMA with _QUARANTINED expanded), verbatim
BANDNUM_DDL = (
    "CREATE TABLE IF NOT EXISTS band_items (n INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL CHECK (source IN "
    "('question','consent','sign','extend','host')), ref TEXT NOT NULL, numbered_at TEXT NOT NULL, UNIQUE (source, ref))",
    "CREATE TABLE IF NOT EXISTS band_closed_shown (n INTEGER PRIMARY KEY REFERENCES band_items(n), at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS band_items_retired (n INTEGER NOT NULL, source TEXT NOT NULL, ref TEXT NOT NULL, "
    "numbered_at TEXT NOT NULL, freed_at TEXT NOT NULL, retired_at TEXT NOT NULL, closed_shown_at TEXT, PRIMARY KEY (n, "
    "numbered_at))",
    "CREATE TABLE IF NOT EXISTS band_freed (source TEXT NOT NULL, ref TEXT NOT NULL, numbered_at TEXT NOT NULL, freed_at "
    "TEXT NOT NULL, PRIMARY KEY (source, ref, numbered_at))",
    "CREATE TRIGGER IF NOT EXISTS band_items_frozen BEFORE UPDATE ON band_items BEGIN SELECT RAISE(ABORT, 'band numbers "
    "are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_items_kept BEFORE DELETE ON band_items WHEN NOT EXISTS (SELECT 1 FROM "
    "band_items_retired r WHERE r.n = OLD.n AND r.source = OLD.source AND r.ref = OLD.ref AND r.numbered_at = "
    "OLD.numbered_at AND strftime('%Y-%m-%d %H:%M:%f', r.freed_at, '+14 days') <= strftime('%Y-%m-%d %H:%M:%f', "
    "r.retired_at)) BEGIN SELECT RAISE(ABORT, 'band numbers are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_items_retired_of_a_row BEFORE INSERT ON band_items_retired WHEN NOT EXISTS "
    "(SELECT 1 FROM band_items b WHERE b.n = NEW.n AND b.source = NEW.source AND b.ref = NEW.ref AND b.numbered_at = "
    "NEW.numbered_at) BEGIN SELECT RAISE(ABORT, 'only a numbered row can be retired'); END",
    "CREATE TRIGGER IF NOT EXISTS band_items_retired_frozen BEFORE UPDATE ON band_items_retired BEGIN SELECT "
    "RAISE(ABORT, 'band numbers are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_items_retired_kept BEFORE DELETE ON band_items_retired BEGIN SELECT RAISE(ABORT, "
    "'band numbers are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_freed_frozen BEFORE UPDATE ON band_freed BEGIN SELECT RAISE(ABORT, 'band numbers "
    "are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_freed_kept BEFORE DELETE ON band_freed BEGIN SELECT RAISE(ABORT, 'band numbers "
    "are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_items_no_replace BEFORE INSERT ON band_items WHEN EXISTS (SELECT 1 FROM band_items "
    "WHERE (source = NEW.source AND ref = NEW.ref) OR n = NEW.n) BEGIN SELECT RAISE(ABORT, 'a band number is never "
    "replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS band_closed_shown_frozen BEFORE UPDATE ON band_closed_shown BEGIN SELECT RAISE(ABORT, "
    "'band numbers are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_closed_shown_kept BEFORE DELETE ON band_closed_shown WHEN NOT EXISTS (SELECT 1 "
    "FROM band_items_retired r WHERE r.n = OLD.n AND r.closed_shown_at = OLD.at AND strftime('%Y-%m-%d %H:%M:%f', "
    "r.freed_at, '+14 days') <= strftime('%Y-%m-%d %H:%M:%f', r.retired_at)) BEGIN SELECT RAISE(ABORT, 'band numbers "
    "are append-only'); END",
    "CREATE TABLE IF NOT EXISTS band_views (seq INTEGER PRIMARY KEY AUTOINCREMENT, viewer TEXT NOT NULL, seen_through "
    "INTEGER NOT NULL, open_list TEXT NOT NULL, store_id TEXT NOT NULL, at TEXT NOT NULL, gen INTEGER NOT NULL)",
)
OLD_VIEWS = BANDNUM_DDL[-1]                     # band_views is the same in 8d21607f2 and 8319d11b4 (no `lives`)


def _full_shape(store):
    with sqlite3.connect(store.path) as c:
        return (c.execute("SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'band%' ORDER BY name").fetchall(),
                {t: c.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                 for t in ("band_items", "band_items_retired", "band_freed", "band_closed_shown", "band_lives")},
                c.execute("SELECT * FROM sqlite_sequence WHERE name = 'band_items'").fetchall())


def _bandnum_store(store, clock):
    """A store as 8319d11b4 left it: 1 was q0 (answered, notice shown, retired) and is q3 now; 2 is q1 (open); 3 is a
    released host row; a view of seat 2 recorded while 1 was q0, and one of seat 1 after the reissue."""
    qs = [ask(store, f"q{i}") for i in range(4)]
    t, later = band_view.utc_stamp(T0), band_view.utc_stamp(T0 + timedelta(days=20))
    with sqlite3.connect(store.path) as c:
        for stmt in BANDNUM_DDL:
            c.execute(stmt)
        sid = c.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()[0]
        c.execute("INSERT INTO band_items (source, ref, numbered_at) VALUES ('question', ?, ?)", (qs[0].id, t))
        c.execute("INSERT INTO band_items (source, ref, numbered_at) VALUES ('question', ?, ?)", (qs[1].id, t))
        c.execute("INSERT INTO band_items (source, ref, numbered_at) VALUES ('host', 'np-a', ?)", (t,))
        c.execute("INSERT INTO band_freed (source, ref, numbered_at, freed_at) VALUES ('host', 'np-a', ?, ?)", (t, t))
        c.execute("INSERT INTO band_closed_shown (n, at) VALUES (1, ?)", (t,))
        c.execute("INSERT INTO band_views (viewer, seen_through, open_list, store_id, at, gen) VALUES (?, 2, '[1, 2]', ?, "
                  "?, 1)", (OTHER, sid, t))
    store.answer(qs[0].id, option="fix", channel="cli")
    with sqlite3.connect(store.path) as c:                           # what 8319d11b4's retirement wrote, by its rules
        c.execute("INSERT INTO band_items_retired (n, source, ref, numbered_at, freed_at, retired_at, closed_shown_at) "
                  "VALUES (1, 'question', ?, ?, ?, ?, ?)", (qs[0].id, t, t, later, t))
        c.execute("DELETE FROM band_closed_shown WHERE n = 1")
        c.execute("DELETE FROM band_items WHERE n = 1")
        c.execute("INSERT INTO band_items (n, source, ref, numbered_at) VALUES (1, 'question', ?, ?)", (qs[3].id, later))
        c.execute("INSERT INTO band_views (viewer, seen_through, open_list, store_id, at, gen) VALUES (?, 3, '[1, 2]', ?, "
                  "?, 2)", (VIEWER, sid, later))
    return qs


def test_a_store_at_the_bandnum_schema_migrates_in_place_twice_the_same_and_keeps_every_row(store, env, clock):
    qs = _bandnum_store(store, clock)
    clock.now = T0 + timedelta(days=21)
    with sqlite3.connect(store.path) as c:                           # reads before any write: no `life`, no `lives`
        assert not band_view._has_col(c, "band_items", "life") and not band_view._has_col(c, "band_views", "lives")
    assert band_view.last_view(store, VIEWER).lives is None           # a read of an unmigrated store works
    before = rows(store)
    new = ask(store, "new")
    band_view._number(store, [new.id], band_view.time.monotonic() + 2)   # the migrating write
    once = _full_shape(store)
    band_view._number(store, [new.id], band_view.time.monotonic() + 2)
    band_view.record_view(store, "x", 0, [])                          # every writer runs the same prelude
    assert _full_shape(store) == once                                 # idempotent
    sql = {r[1]: r[2] for r in once[0]}
    assert "julianday(" in sql["band_items_kept"] and "julianday(" in sql["band_closed_shown_kept"]
    assert "b.life" in sql["band_items_retired_of_a_row"] and "strftime" not in sql["band_items_kept"]
    # every row kept but the host row released 21 days ago: due, retired by this write, and `new` takes its 3
    assert rows(store) == [*before[:2], (3, "question", new.id)]
    assert [(r[0], r[2], r[-1]) for r in once[1]["band_items_retired"]] == [(1, qs[0].id, 0), (3, "np-a", 0)]
    assert once[1]["band_lives"] == [(1, 1)]                          # one new life, issued to `new`
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT value FROM meta WHERE key = 'band_numbers'").fetchone() == ("recycle-14d-2",)
    # the old views name no lives: seat 2's (1 was q0 then) is refused 1, keeps 2; seat 1's is refused 1 too (fail closed)
    echo = band_view._seat_answer(store, OTHER, "1a", env, None)
    assert "refused: 1 was not open" in echo and not answered(store, qs[3])
    assert "recorded: 2a" in band_view._seat_answer(store, OTHER, "2a", env, None)
    # and the store recycles on: q3 (a legacy row, life 0) closes, 14 days, a new row takes 1 with a new life
    close(store, qs[3])
    clock.advance(timedelta(days=14))
    nxt = ask(store, "next")
    see(store)
    assert number_of(store, nxt) == 1 and life_of(store, 1) == 3          # q2 took life 2 in the same render
    assert {(r[0], r[2]) for r in retired(store)} == {(1, qs[0].id), (1, qs[3].id), (2, qs[1].id), (3, "np-a")}
    band_view.allocate(store, "host", ["np-b"])
    assert sorted(n for n, *_ in rows(store)) == list(range(1, len(rows(store)) + 1))   # lowest free first: no gap


def test_a_store_at_the_previous_schema_gains_lives_and_its_old_views_stay_usable(store, env, clock):
    qs = _previous_store(store, clock)
    with sqlite3.connect(store.path) as c:
        c.execute(OLD_VIEWS)
        sid = c.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()[0]
        c.execute("INSERT INTO band_views (viewer, seen_through, open_list, store_id, at, gen) VALUES (?, 3, '[2, 3]', ?, "
                  "?, 1)", (VIEWER, sid, band_view.utc_stamp(T0)))
    new = ask(store, "new")
    band_view._number(store, [new.id], band_view.time.monotonic() + 2)
    once = _full_shape(store)
    band_view._number(store, [new.id], band_view.time.monotonic() + 2)
    assert _full_shape(store) == once
    with sqlite3.connect(store.path) as c:
        assert all(band_view._has_col(c, t, col) for t, col, _ in band_view._ADDED_COLUMNS)
        assert c.execute("SELECT n, life FROM band_items ORDER BY n").fetchall()[:4] == [(1, None), (2, None),
                                                                                      (3, None), (4, None)]
    assert life_of(store, 5) == 1
    assert "recorded: 2a" in band_view._seat_answer(store, VIEWER, "2a", env, None)   # nothing retired: usable as before
    assert answered(store, qs[1])


def test_deepseek_med_3_a_legacy_view_never_takes_the_answer_of_the_row_that_holds_its_number_now(store, clock):
    """1 was q1 (closed, retired), and is q2 now, answered and not yet retired. A previous view that showed q1 as 1:
    with lives it names q1's channel; without lives it says "gone", never q2's channel."""
    q1 = ask(store, "one")
    see(store)
    with_lives = band_view.last_view(store, VIEWER)
    close(store, q1, channel="cli")
    clock.advance(timedelta(days=15))
    q2 = ask(store, "two")
    see(store, OTHER)
    store.answer(q2.id, option="fix", channel="phone")             # 1 is q2's, closed, held (its notice unseen)
    r = band_view.render_full(store)
    assert r.shown == [] and r.closed == [1] and number_of(store, q2) == 1
    assert "closed 1 (answered via cli)" in band_view._delta_line(store, with_lives, r.shown, lives=r.lives)
    legacy = band_view.View(1, (1,), with_lives.store_id, None)
    line = band_view._delta_line(store, legacy, r.shown, lives=r.lives)
    assert "closed 1 (gone)" in line and "phone" not in line


def test_a_notice_whose_number_the_write_itself_retired_is_not_shown_beside_the_reissue(store, clock, qdir, monkeypatch):
    """The render reads q1's `closed 1` as owed; before its numbering write, another seat (its clock 15 days behind)
    delivers it; the write then retires 1 and reissues it. The render does not show `closed 1` beside the new 1."""
    q1 = ask(store, "one")
    see(store)
    answer_only(store, q1)
    life1 = life_of(store, 1)
    clock.advance(timedelta(days=15))
    q2 = ask(store, "two")
    real = band_view._number_tx

    def other_seat_first(*a, **k):
        other = QuestionStore(qdir, clock=lambda: T0)
        other.ready_by(band_view.time.monotonic() + 2)
        band_view.mark_closed_shown(other, [band_view.ClosedNotice(1, "question", q1.id, life1)])
        return real(*a, **k)
    monkeypatch.setattr(band_view, "_number_tx", other_seat_first)
    r = band_view.render_full(store)
    assert number_of(store, q2) == 1 and r.shown == [1]
    assert r.closed == [] and "closed" not in r.text.splitlines()[0]


def test_a_host_release_frees_only_the_life_it_named_even_when_a_clock_set_back_repeats_a_stamp(store, clock):
    """Clock rollback: np-a is released, retired, then numbered again at the SAME stamp. Its first release names the old
    life, so the new np-a is not released and is never retired."""
    assert band_view.allocate(store, "host", ["np-a"]) == {"np-a": 1}
    band_view.release(store, "host", ["np-a"])
    clock.advance(timedelta(days=14))
    assert band_view.allocate(store, "host", ["np-z"]) == {"np-z": 1}   # np-a retired, 1 reissued
    clock.now = T0                                                       # set back: the same numbered_at as before
    assert band_view.allocate(store, "host", ["np-a"]) == {"np-a": 2}
    clock.advance(timedelta(days=30))
    assert band_view.allocate(store, "host", ["np-y"]) == {"np-y": 3}   # np-a (never released in this life) kept 2
    assert ("host", "np-a") in {(s, r) for _, s, r in rows(store)}
    band_view.release(store, "host", ["np-a"])                           # its own release, its own life
    clock.advance(timedelta(days=14))
    assert band_view.allocate(store, "host", ["np-x"]) == {"np-x": 2}
