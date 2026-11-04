"""The room's own record (RM-A): one open room, his bytes as the drop's words, one commit, expiry on rotation, a note
that never touches ``words_sha256``, append-only tables, and the non-room refusal.

``launches.role`` does not yet admit ``room`` on this base (the launches rebuild is another lane's), so the fixture
widens that one CHECK in the test store's schema text and turns a worker launch into a room launch on a raw connection.
That is test scaffolding only: ``room.py`` compares ``role`` as text and never needs the rebuild."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.coordinator import chat, room
from levain.coordinator.chat import ChatStore
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import CoordError, Part, words_sha256

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir, monkeypatch):
    s = EnvelopeStore(qdir, clock=Clock())
    s.coord_set_agent("none")
    return s


@pytest.fixture
def cs(store):
    c = ChatStore(store)
    c._connect().close()
    return c


def raw(qdir):
    c = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    c.execute("PRAGMA foreign_keys = ON")
    return c


def make_thread(store, title="the room"):
    d = store.accept_drop(title, surface="cli")[0]
    return store.split(d.drop_id, [Part("note", title)])[0].thread_id


def make_launch(store, qdir, room_thread, *, role="room"):
    token = store.coord_take()
    ln = store.launch_request(token, harness="claude", role="worker", threads=[room_thread])
    got, why = store.launch_activate(ln.launch_id, harness_session_id="sess-" + ln.launch_id[-6:])
    assert why is None
    if role != "worker":
        c = raw(qdir)
        c.execute("DROP TRIGGER launches_frozen")
        c.execute("UPDATE launches SET role = ? WHERE launch_id = ?", (role, ln.launch_id))
        c.close()
    return ln.launch_id


def say(cs, thread, body, *, n, author="operator"):
    kind = {"operator": dict(author_kind="operator", author_surface="cockpit", client_id="tab-a"),
            "coordinator": dict(author_kind="coordinator", author_surface="coordinator", client_id="coord")}[author]
    with cs._write() as (conn, now):
        m, _ = chat.record_message(conn, now, thread_id=thread, recipient="thread", outbox_id=f"ob-{n:08d}",
                                   body=body, normalized=body, launch_id=None, **kind)
    return m.message_id


@pytest.fixture
def world(store, cs, qdir):
    thread = make_thread(store)
    c = raw(qdir)
    room.ensure(c)
    room.open_room_thread(c, thread)
    c.close()
    return {"thread": thread, "launch": make_launch(store, qdir, thread)}


def db(store, fn):
    with store._write() as (conn, now):
        return fn(conn)


# --- one open room ------------------------------------------------------------------------------------------------
def test_one_room_is_open_at_a_time(store, qdir, world):
    other = make_thread(store, "another")
    with pytest.raises(CoordError, match="open"):
        db(store, lambda c: room.open_room_thread(c, other))
    c = raw(qdir)
    # the trigger holds it for any writer, not only this module
    with pytest.raises(sqlite3.IntegrityError, match="one room is open at a time"):
        c.execute("INSERT INTO room_events (gseq, room_thread, kind, at, by) VALUES (999999, ?, 'opened', 'x', 'service')",
                  (other,))
    assert room.current_room(c) == world["thread"]


def test_rotate_closes_the_old_and_opens_the_new(store, world):
    new = make_thread(store, "next")
    old = db(store, lambda c: room.rotate(c, new))
    assert old == world["thread"]
    assert db(store, room.current_room) == new
    with pytest.raises(CoordError, match="opened once|open"):
        db(store, lambda c: room.open_room_thread(c, world["thread"]))
    third = make_thread(store, "third")
    db(store, lambda c: room.rotate(c, third))
    with pytest.raises(sqlite3.IntegrityError, match="a room thread is opened once"):
        # an old thread never reopens, even by raw SQL after a rotation
        db(store, lambda c: c.execute("INSERT INTO room_events (gseq, room_thread, kind, at, by) VALUES "
                                      "(999998, ?, 'opened', 'x', 'service')", (world["thread"],)))


def test_no_room_to_rotate(store):
    new = make_thread(store)
    with pytest.raises(CoordError, match="no room is open"):
        db(store, lambda c: room.rotate(c, new))


# --- words are his bytes ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("body", ["plain words", "café ☃ \U0001f600 日本語", "two\n\nlines\n",
                                  "  leading and trailing  \n\n\n", "tabs\tand\r\ncrlf\r\n"])
def test_the_drop_carries_his_bytes_exactly(store, cs, world, body):
    mid = say(cs, world["thread"], body, n=1)
    p = db(store, lambda c: room.propose(c, world["launch"], [mid], "the room's gist"))
    assert p.words == body and p.words_sha256 == words_sha256(body)
    drop, created = room.commit(store, p.proposal_id, p.words, "ob-commit01", "cockpit")
    assert created and drop.words == body and drop.words_sha256 == words_sha256(body)
    assert drop.words.encode("utf-8") == body.encode("utf-8")


def test_several_quotes_join_by_a_blank_line_in_the_order_given(store, cs, world):
    a, b = say(cs, world["thread"], "first\n", n=1), say(cs, world["thread"], "second", n=2)
    p = db(store, lambda c: room.propose(c, world["launch"], [b, a], "gist"))
    assert p.words == "second\n\nfirst\n"


def test_the_room_cannot_quote_its_own_or_another_threads_words(store, cs, world):
    own = say(cs, world["thread"], "the entity said this", n=1, author="coordinator")
    elsewhere = say(cs, make_thread(store, "elsewhere"), "wrong room", n=2)
    for bad in (own, elsewhere, "cm-" + "0" * 32, "not an id"):
        with pytest.raises(CoordError):
            db(store, lambda c: room.propose(c, world["launch"], [bad], "gist"))
    for bad in ([], [own, own], "cm-abc"):
        with pytest.raises(CoordError):
            db(store, lambda c: room.propose(c, world["launch"], bad, "gist"))


def test_an_edit_before_the_commit_is_what_the_drop_carries(store, cs, world):
    mid = say(cs, world["thread"], "ship it friday", n=1)
    p = db(store, lambda c: room.propose(c, world["launch"], [mid], "gist"))
    drop, _ = room.commit(store, p.proposal_id, "ship it thursday", "ob-commit01", "phone")
    assert drop.words == "ship it thursday" and drop.surface == "phone"
    assert db(store, lambda c: room.get_proposal(c, p.proposal_id)).state == "committed"


# --- the note never enters the words ------------------------------------------------------------------------------
def test_the_note_is_filed_beside_the_drop_and_never_enters_its_hash(store, cs, world, qdir):
    mid = say(cs, world["thread"], "send the report", n=1)
    p = db(store, lambda c: room.propose(c, world["launch"], [mid], "the room's own summary of the chat"))
    drop, _ = room.commit(store, p.proposal_id, p.words, "ob-commit01", "cockpit")
    c = raw(qdir)
    note = c.execute("SELECT body, author, drop_id, proposal_id FROM drop_notes").fetchall()
    assert note == [("the room's own summary of the chat", "room", drop.drop_id, p.proposal_id)]
    stored_words, stored_sha = c.execute("SELECT words, words_sha256 FROM drops WHERE drop_id = ?",
                                         (drop.drop_id,)).fetchone()
    assert stored_words == "send the report" and stored_sha == words_sha256("send the report")
    assert "summary" not in stored_words
    # a note for any other text, or for a drop that proposal did not make, is refused by the table itself
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO drop_notes (drop_id, gseq, author, room_thread, launch_id, proposal_id, body, "
                  "body_sha256, at) VALUES (?, 777777, 'room', ?, ?, ?, 'other text', 'x', 'x')",
                  (drop.drop_id, world["thread"], world["launch"], p.proposal_id))


# --- double commit, dismiss, rotation ------------------------------------------------------------------------------
def test_a_second_commit_refuses_and_writes_nothing(store, cs, world, qdir):
    mid = say(cs, world["thread"], "once", n=1)
    p = db(store, lambda c: room.propose(c, world["launch"], [mid], "gist"))
    first, _ = room.commit(store, p.proposal_id, p.words, "ob-commit01", "cockpit")
    with pytest.raises(CoordError, match="committed"):
        room.commit(store, p.proposal_id, p.words, "ob-commit02", "cockpit")
    c = raw(qdir)
    assert c.execute("SELECT count(*) FROM drops").fetchone()[0] == 2     # the room-thread drop + this one
    assert c.execute("SELECT drop_id FROM proposal_events").fetchall() == [(first.drop_id,)]
    # a resend of the first outbox id is the stored drop, nothing more
    again, created = room.commit(store, p.proposal_id, p.words, "ob-commit01", "cockpit")
    assert not created and again.drop_id == first.drop_id


def test_a_resend_that_names_another_drops_outbox_id_refuses(store, cs, world):
    other = store.accept_drop("unrelated", surface="cockpit", outbox_id="ob-other001")[0]
    mid = say(cs, world["thread"], "unrelated", n=1)
    p = db(store, lambda c: room.propose(c, world["launch"], [mid], "gist"))
    with pytest.raises(CoordError, match="not this proposal's|not .*committed|open"):
        room.commit(store, p.proposal_id, "unrelated", other.outbox_id, "cockpit")
    assert db(store, lambda c: room.get_proposal(c, p.proposal_id)).state == "open"


def test_dismiss_settles_once(store, cs, world):
    mid = say(cs, world["thread"], "never mind", n=1)
    p = db(store, lambda c: room.propose(c, world["launch"], [mid], "gist"))
    assert db(store, lambda c: room.dismiss(c, p.proposal_id)).state == "dismissed"
    with pytest.raises(CoordError, match="dismissed"):
        db(store, lambda c: room.dismiss(c, p.proposal_id))
    with pytest.raises(CoordError, match="dismissed"):
        room.commit(store, p.proposal_id, p.words, "ob-commit01", "cockpit")


def test_a_proposal_expires_when_its_room_rotates(store, cs, world, qdir):
    mid = say(cs, world["thread"], "too late", n=1)
    p = db(store, lambda c: room.propose(c, world["launch"], [mid], "gist"))
    new = make_thread(store, "next")
    db(store, lambda c: room.rotate(c, new))
    assert db(store, lambda c: room.get_proposal(c, p.proposal_id)).state == "expired"
    drops_before = raw(qdir).execute("SELECT count(*) FROM drops").fetchone()[0]
    with pytest.raises(CoordError, match="expired"):
        room.commit(store, p.proposal_id, p.words, "ob-commit01", "cockpit")
    assert raw(qdir).execute("SELECT count(*) FROM drops").fetchone()[0] == drops_before
    # the old room's launch proposes no more
    with pytest.raises(CoordError, match="open room"):
        db(store, lambda c: room.propose(c, world["launch"], [mid], "gist"))
    # the trigger holds expiry against a writer that skips the module
    with pytest.raises(sqlite3.IntegrityError, match="expires"):
        db(store, lambda c: c.execute("INSERT INTO proposal_events (proposal_id, kind, drop_id, at, by) VALUES "
                                      "(?, 'committed', (SELECT drop_id FROM drops LIMIT 1), 'x', 'operator')",
                                      (p.proposal_id,)))


# --- the launch must be a room launch ------------------------------------------------------------------------------
def test_a_worker_launch_cannot_propose(store, cs, qdir, world):
    worker = make_launch(store, qdir, world["thread"], role="worker")
    mid = say(cs, world["thread"], "hello", n=1)
    with pytest.raises(CoordError, match="not a room launch"):
        db(store, lambda c: room.propose(c, worker, [mid], "gist"))
    with pytest.raises(CoordError, match="no launch"):
        db(store, lambda c: room.propose(c, "ln-" + "0" * 32, [mid], "gist"))
    with pytest.raises(sqlite3.IntegrityError, match="room launch"):
        db(store, lambda c: c.execute(
            "INSERT INTO drop_proposals (proposal_id, gseq, room_thread, launch_id, quoted, words, words_sha256, "
            "summary, summary_sha256, about, at) VALUES ('pr-' || hex(randomblob(16)), 555555, ?, ?, '[]', 'w', 'x', "
            "'s', 'x', NULL, 'x')", (world["thread"], worker)))


def test_a_summary_and_about_are_checked(store, cs, world):
    mid = say(cs, world["thread"], "hi", n=1)
    for bad in ("", "   ", "x" * (room.SUMMARY_MAX + 1), "nul\x00", None):
        with pytest.raises(CoordError):
            db(store, lambda c: room.propose(c, world["launch"], [mid], bad))
    with pytest.raises(CoordError, match="about"):
        db(store, lambda c: room.propose(c, world["launch"], [mid], "gist", "th-" + "0" * 32))
    p = db(store, lambda c: room.propose(c, world["launch"], [mid], "x" * room.SUMMARY_MAX, world["thread"]))
    assert p.about == world["thread"]


# --- append-only ----------------------------------------------------------------------------------------------------
def test_every_table_refuses_update_and_delete(store, cs, world, qdir):
    mid = say(cs, world["thread"], "keep me", n=1)
    p = db(store, lambda c: room.propose(c, world["launch"], [mid], "gist"))
    room.commit(store, p.proposal_id, p.words, "ob-commit01", "cockpit")
    c = raw(qdir)
    for table, col in (("room_events", "by"), ("drop_proposals", "summary"), ("proposal_events", "by"),
                       ("drop_notes", "body")):
        assert c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= 1, table
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute(f"UPDATE {table} SET {col} = {col}")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute(f"DELETE FROM {table}")


def test_ensure_is_idempotent_and_adds_only_its_own_objects(store, qdir):
    c = raw(qdir)
    before = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    room.ensure(c)
    room.ensure(c)
    after = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert before < after and {"room_events", "drop_proposals", "proposal_events", "drop_notes"} <= after - before
    assert all(n.split("_")[0] in ("room", "drop", "proposal", "sqlite") for n in after - before if not n.startswith("sqlite_"))


# --- L3 r1 fixes ---------------------------------------------------------------------------------------------------
def test_insert_or_replace_cannot_rewrite_any_room_table(store, cs, world, qdir):
    mid = say(cs, world["thread"], "keep me", n=1)
    p = db(store, lambda c: room.propose(c, world["launch"], [mid], "gist"))
    drop, _ = room.commit(store, p.proposal_id, p.words, "ob-commit01", "cockpit")
    c = raw(qdir)
    before = {t: c.execute(f"SELECT * FROM {t}").fetchall()
              for t in ("room_events", "drop_proposals", "proposal_events", "drop_notes")}
    gs = lambda t: c.execute(f"SELECT gseq FROM {t}").fetchone()[0]
    attempts = [
        "INSERT OR REPLACE INTO room_events (seq, gseq, room_thread, kind, at, by) VALUES "
        f"(1, 880001, '{world['thread']}', 'rotated', 'x', 'service')",
        "INSERT OR REPLACE INTO room_events (gseq, room_thread, kind, at, by) VALUES "
        f"({gs('room_events')}, '{world['thread']}', 'rotated', 'x', 'service')",
        "INSERT OR REPLACE INTO drop_proposals (proposal_id, gseq, room_thread, launch_id, quoted, words, words_sha256, "
        f"summary, summary_sha256, about, at) VALUES ('{p.proposal_id}', 880002, '{world['thread']}', '{world['launch']}', "
        "'[]', 'w', 'x', 'changed', 'x', NULL, 'x')",
        "INSERT OR REPLACE INTO proposal_events (seq, proposal_id, kind, drop_id, at, by) VALUES "
        f"(1, '{p.proposal_id}', 'dismissed', NULL, 'x', 'operator')",
        "INSERT OR REPLACE INTO proposal_events (proposal_id, kind, drop_id, at, by) VALUES "
        f"('{p.proposal_id}', 'dismissed', NULL, 'x', 'operator')",
        "INSERT OR REPLACE INTO drop_notes (drop_id, gseq, author, room_thread, launch_id, proposal_id, body, body_sha256, "
        f"at) VALUES ('{drop.drop_id}', 880003, 'room', '{world['thread']}', '{world['launch']}', '{p.proposal_id}', "
        "'gist', 'x', 'x')",
    ]
    for sql in attempts:
        with pytest.raises(sqlite3.IntegrityError, match="never replaced"):
            c.execute(sql)
    after = {t: c.execute(f"SELECT * FROM {t}").fetchall() for t in before}
    assert after == before
    assert db(store, lambda cn: room.get_proposal(cn, p.proposal_id)).state == "committed"


def test_an_empty_room_events_table_refuses_a_rotation_and_a_proposal(store, qdir):
    thread = make_thread(store)
    launch = make_launch(store, qdir, thread)
    c = raw(qdir)
    room.ensure(c)
    assert c.execute("SELECT count(*) FROM room_events").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match="only the open room rotates"):
        c.execute("INSERT INTO room_events (gseq, room_thread, kind, at, by) VALUES (990001, ?, 'rotated', 'x', 'service')",
                  (thread,))
    with pytest.raises(sqlite3.IntegrityError, match="open room"):
        c.execute("INSERT INTO drop_proposals (proposal_id, gseq, room_thread, launch_id, quoted, words, words_sha256, "
                  "summary, summary_sha256, about, at) VALUES ('pr-' || hex(randomblob(16)), 990002, ?, ?, '[]', 'w', 'x', "
                  "'s', 'x', NULL, 'x')", (thread, launch))


def test_a_concurrent_dismiss_is_a_named_error(store, cs, world, qdir):
    mid = say(cs, world["thread"], "never mind", n=1)
    p = db(store, lambda c: room.propose(c, world["launch"], [mid], "gist"))
    stale = room.get_proposal(raw(qdir), p.proposal_id)
    assert stale.state == "open"
    db(store, lambda c: room.dismiss(c, p.proposal_id))
    # the loser read 'open' before the winner settled: its insert hits the unique key, not a raw sqlite3 error
    real = room._proposal
    try:
        room._proposal = lambda conn, pid: stale
        with pytest.raises(CoordError, match="already settled"):
            db(store, lambda c: room.dismiss(c, p.proposal_id))
    finally:
        room._proposal = real


@pytest.mark.parametrize("bad", [[[]], [["cm-" + "0" * 32]], [{}], [None], [1], [b"cm-x"]])
def test_an_unhashable_or_non_string_quote_is_a_coord_error(store, cs, world, bad):
    with pytest.raises(CoordError):
        db(store, lambda c: room.propose(c, world["launch"], bad, "gist"))


def test_commit_surfaces_a_drops_table_integrity_error_as_a_coord_error(store, cs, world, qdir, monkeypatch):
    mid = say(cs, world["thread"], "once", n=1)
    p = db(store, lambda c: room.propose(c, world["launch"], [mid], "gist"))

    def boom(self, conn, now, **kw):
        raise sqlite3.IntegrityError("CHECK constraint failed: drops")
    monkeypatch.setattr(EnvelopeStore, "_insert_drop", boom)
    with pytest.raises(CoordError, match="CHECK constraint"):
        room.commit(store, p.proposal_id, p.words, "ob-commit01", "cockpit")
