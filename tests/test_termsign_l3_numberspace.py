"""TERMSIGN L3: one number space. A footer number a host (flow) allocated through ``band_view.allocate`` is never a
Levain item, so a seat that types it gets no Levain refusal line; the store migrates from the schema before ``host``.
"""
from __future__ import annotations

import inspect
import sqlite3
import time

import pytest

from levain import band_view
from levain.consent.questions import QuestionStore

OPTS = [("fix", "fixed 30 s"), ("exp", "exponential")]
VIEWER = "claude_code:seat-1"
SESSION = "seat-1"

OLD_ITEMS = ("CREATE TABLE band_items (n INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL CHECK (source "
             "IN ('question','consent','sign','extend')), ref TEXT NOT NULL, numbered_at TEXT NOT NULL, "
             "UNIQUE (source, ref))")


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir):
    return QuestionStore(qdir)


@pytest.fixture
def env(qdir):
    return {"LEVAIN_HOME": str(qdir.parent)}


def ask(store, what):
    return store.file(agent="claude_code", agent_session_id="sess-1", what=what, options=OPTS, rec="exp",
                      blocks="the PR", cwd="/tmp/proj")


def show(store, env, n=2):
    for i in range(n):
        ask(store, f"question {i}")
    out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "hello"}, env)
    commit()
    assert f"band@{n}" in out


def seat(store, env, text):
    return band_view._seat_answer(store, VIEWER, text, env, None)


def items(store):
    with sqlite3.connect(store.path) as c:
        return c.execute("SELECT n, source, ref FROM band_items ORDER BY n").fetchall()


# --- the reproduced failure: before L3 a number Levain does not own got a refusal line -----------------------------
def test_a_host_number_typed_at_a_seat_produces_no_levain_refusal(store, env):
    show(store, env)
    host = band_view.allocate(store, "host", ["np-aaaa1111", "np-bbbb2222"])
    assert sorted(host.values()) == [3, 4]
    assert seat(store, env, "3a") == ""
    assert seat(store, env, "4a, 3b") == ""
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0


def test_a_mixed_line_records_the_levain_item_and_is_silent_on_the_host_one(store, env):
    show(store, env)
    host = band_view.allocate(store, "host", ["np-aaaa1111"])
    echo = seat(store, env, f"1a, {host['np-aaaa1111']}a")
    assert "recorded: 1a" in echo and "refused" not in echo


def test_a_number_nobody_owns_is_still_refused(store, env):
    show(store, env)
    band_view.allocate(store, "host", ["np-aaaa1111"])
    assert "refused: 9 was filed after" in seat(store, env, "9a")


def test_host_numbers_never_appear_in_the_levain_band(store, env):
    ask(store, "one")
    band_view.render(store)
    band_view.allocate(store, "host", ["np-aaaa1111"])
    ask(store, "two")
    out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "hello"}, env)
    commit()
    assert "np-aaaa1111" not in out and "] one" in out and "] two" in out
    # the numbers are one space: the host took 2, so the second question is 3
    assert [(r[0], r[1]) for r in items(store)] == [(1, "question"), (2, "host"), (3, "question")]


def test_a_host_number_is_not_an_authority_extra(store, env):
    show(store, env)
    n = band_view.allocate(store, "host", ["np-aaaa1111"])["np-aaaa1111"]
    view = band_view.last_view(store, VIEWER)
    extras, unknown = band_view._extras_of(store, view._replace(open_list=(*view.open_list, n))
                                           if hasattr(view, "_replace") else view)
    assert n not in extras and n not in unknown


# --- allocate: the public contract ----------------------------------------------------------------------------------
def test_allocate_signature_is_stable():
    sig = inspect.signature(band_view.allocate)
    assert list(sig.parameters) == ["store", "source", "refs", "deadline", "now"]
    assert [p.kind.name for p in sig.parameters.values()] == ["POSITIONAL_OR_KEYWORD"] * 3 + ["KEYWORD_ONLY"] * 2
    assert sig.parameters["deadline"].default is None and sig.parameters["now"].default is None
    assert band_view.ALLOCATABLE_SOURCES == ("host",)
    assert {"allocate", "ALLOCATABLE_SOURCES"} <= set(band_view.__all__)


def test_allocate_is_idempotent_and_ordered(store):
    a = band_view.allocate(store, "host", ["x", "y"])
    assert a == {"x": 1, "y": 2}
    assert band_view.allocate(store, "host", ["y", "z", "x"]) == {"y": 2, "z": 3, "x": 1}
    assert band_view.allocate(store, "host", ["x", "x"]) == {"x": 1}


def test_allocate_empty_touches_nothing(store):
    assert band_view.allocate(store, "host", []) == {}
    assert not store.path.exists() or "band_items" not in {
        r[0] for r in sqlite3.connect(store.path).execute("SELECT name FROM sqlite_master")}


@pytest.mark.parametrize("source", ["question", "consent", "sign", "extend", "", "nope", None])
def test_allocate_refuses_an_unknown_source(store, source):
    with pytest.raises(ValueError, match="unknown band source"):
        band_view.allocate(store, source, ["x"])
    assert not store.path.exists() or not items(store) if store.path.exists() else True


@pytest.mark.parametrize("bad", [[""], [1], [None], ["ok", ""]])
def test_allocate_refuses_a_bad_ref(store, bad):
    with pytest.raises(ValueError):
        band_view.allocate(store, "host", bad)


def test_allocate_busy_lock_returns_empty_within_the_bound(store):
    ask(store, "q")
    band_view.allocate(store, "host", ["x"])
    blocker = sqlite3.connect(store.path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        t0 = time.monotonic()
        assert band_view.allocate(store, "host", ["new"], deadline=time.monotonic() + 0.3) == {}
        assert time.monotonic() - t0 < 2.0
        assert band_view.allocate(store, "host", ["new"]) == {}          # the default bound is 2 s
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert band_view.allocate(store, "host", ["new"]) == {"new": 2}


def test_a_host_ref_and_a_question_id_do_not_collide(store):
    q = ask(store, "q")
    band_view.render(store)
    assert band_view.allocate(store, "host", [q.id]) == {q.id: 2}


# --- the migration ---------------------------------------------------------------------------------------------------
def old_store(qdir):
    """A store whose band_items is the schema before L3: made by the current code, then rebuilt with the old CHECK."""
    store = QuestionStore(qdir)
    qs = [ask(store, f"q{i}") for i in range(3)]
    band_view.render(store)
    with sqlite3.connect(store.path, isolation_level=None) as c:
        rows = c.execute("SELECT n, source, ref, numbered_at FROM band_items ORDER BY n").fetchall()
        c.execute("DROP TABLE band_items")
        c.execute(OLD_ITEMS)
        c.executemany("INSERT INTO band_items VALUES (?, ?, ?, ?)", rows)
        for stmt in band_view.BAND_SCHEMA:
            if "TRIGGER" in stmt and "band_items" in stmt.split(" ON ")[1][:12]:
                c.execute(stmt)
    return store, qs


def test_an_old_schema_store_migrates_keeps_numbers_and_triggers(qdir):
    store, qs = old_store(qdir)
    with sqlite3.connect(store.path) as c:
        assert "'host'" not in c.execute("SELECT sql FROM sqlite_master WHERE name = 'band_items'").fetchone()[0]
        before = c.execute("SELECT n, source, ref, numbered_at FROM band_items ORDER BY n").fetchall()
    got = band_view.allocate(store, "host", ["np-1"])
    assert got == {"np-1": 4}
    with sqlite3.connect(store.path) as c:
        assert "'host'" in c.execute("SELECT sql FROM sqlite_master WHERE name = 'band_items'").fetchone()[0]
        assert c.execute("SELECT n, source, ref, numbered_at FROM band_items WHERE n <= 3 ORDER BY n"
                         ).fetchall() == before
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert {"band_items_frozen", "band_items_kept", "band_items_no_replace", "answer_context_frozen",
                "answer_context_kept", "band_closed_shown_frozen", "band_closed_shown_kept"} <= names
        assert "band_items_new" not in {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    with sqlite3.connect(store.path) as c:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("UPDATE band_items SET ref = 'z' WHERE n = 1")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("DELETE FROM band_items WHERE n = 1")
        with pytest.raises(sqlite3.DatabaseError, match="never replaced"):
            c.execute("INSERT OR REPLACE INTO band_items (n, source, ref, numbered_at) VALUES (1, 'host', 'q', 't')")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO band_items (source, ref, numbered_at) VALUES ('bogus', 'q', 't')")


def test_answer_context_triggers_still_refuse_after_migration(qdir, env):
    store, qs = old_store(qdir)
    band_view.allocate(store, "host", ["np-1"])
    out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "hello"}, env)
    commit()
    assert "recorded: 1" in seat(store, env, "1a")
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT COUNT(*) FROM answer_context").fetchone()[0] == 1
        c.execute("INSERT INTO band_closed_shown (n, at) VALUES (1, 't')")
    with sqlite3.connect(store.path) as c:
        for stmt in ("UPDATE answer_context SET note = 'x'", "DELETE FROM answer_context"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                c.execute(stmt)
        for stmt in ("UPDATE band_closed_shown SET at = 'x'", "DELETE FROM band_closed_shown"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                c.execute(stmt)


def test_the_numbering_counter_survives_the_rebuild(qdir):
    store, qs = old_store(qdir)
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE sqlite_sequence SET seq = 40 WHERE name = 'band_items'")
    assert band_view.allocate(store, "host", ["np-1"]) == {"np-1": 41}


def test_migration_is_idempotent_and_the_old_store_still_renders(qdir, env):
    store, qs = old_store(qdir)
    out = band_view.render(store)
    assert "q0" in out
    band_view.allocate(store, "host", ["a"])
    band_view.allocate(store, "host", ["b"])
    assert [r[1] for r in items(store)] == ["question"] * 3 + ["host", "host"]
