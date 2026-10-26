"""CH-D1b-2: ``chat_echoes`` and the §5.4 outside epoch (§7.6 echo suppression; B3's echo tests, B12), the timeline's
echo join, the one ensure-schema path a released store is brought current by (head ruling Q-A), the cursor read-back
and the unfenced interrupt. Real store, real sqlite files, :class:`ScriptedSession` as the vendor.

The released shape is :data:`RELEASED_CHAT_SCHEMA_V0_11_25`, the v0.11.25 ``CHAT_SCHEMA`` frozen as text at the end
of this file (``levain/coordinator/chat.py`` was byte-identical at v0.11.25 and at this lane's base)."""
from __future__ import annotations

import ast
import io
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain
from levain.coordinator import chat
from levain.coordinator.chat import ChatService, ChatStore, record_check
from levain.coordinator.driver import CommandResult, InterruptCommand, ReadBatch, ScriptedSession, event
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import CoordError, Part, next_gseq

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
ME = "cockpit:tab-a"
CHAT_TABLES = ("chat_messages", "chat_outbox_events", "chat_events", "chat_cursors", "chat_anomalies",
               "chat_launch_checks", "turn_lease_events")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


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
    return EnvelopeStore(qdir, clock=clock)


def _world(store: EnvelopeStore) -> dict:
    store.coord_set_agent("none")
    token = store.coord_take()
    d = store.accept_drop("fix the build", surface="cli")[0]
    thread = store.split(d.drop_id, [Part("work", "fix the build")])[0]
    ln = store.launch_request(token, harness="claude", role="worker", threads=[thread.thread_id])
    got, why = store.launch_activate(ln.launch_id, harness_session_id="sess-worker-1")
    assert why is None and got.state == "active"
    return {"thread": thread.thread_id, "launch": ln.launch_id}


@pytest.fixture
def world(store):
    return _world(store)


@pytest.fixture
def cs(store):
    s = ChatStore(store)
    s._connect().close()
    return s


def raw(qdir):
    c = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    c.execute("PRAGMA foreign_keys = ON")
    return c


def ready(cs, world, session):
    with cs._write() as (conn, now):
        record_check(conn, now, world["launch"], "live", "activation", "test")
    svc = ChatService(cs)
    svc.attach(world["launch"], session)
    svc.read_next(world["launch"])
    return svc


def send(svc, cs, world, body="go", *, outbox="ob-00000001", owner=ME):
    epoch = cs.turn_view(world["launch"], owner).epoch or cs.turn_claim(world["launch"], owner)
    surface, client = owner.split(":", 1)
    return svc.send(world["launch"], author_kind="operator", author_surface=surface, client_id=client,
                    outbox_id=outbox, body=body, expected_epoch=epoch)


def outbox_rows(cs, message_id):
    with cs._write() as (conn, _now):
        return conn.execute("SELECT seq, state, cause, vendor_turn_id, vendor_item_id FROM chat_outbox_events WHERE "
                            "message_id = ? ORDER BY seq", (message_id,)).fetchall()


def echoes(cs, launch):
    with cs._write() as (conn, _now):
        return conn.execute("SELECT vendor_event_key, message_id, basis FROM chat_echoes WHERE launch_id = ? "
                            "ORDER BY gseq", (launch,)).fetchall()


def outside_epochs(cs, launch):
    return [r for r in cs.turn_history(launch) if r[1] == "outside"]


# --- §7.6 / §5.4: the echo and the outside epoch (B3) ---------------------------------------------------------------
def test_own_echo_opens_no_outside_epoch_when_rd_false(cs, world):
    # R-D false: no correlation field, no idempotency key. The vendor acks with a turn id; its copy of "go" in that
    # turn is an echo: no outside epoch, the composer stays yours, and M's state came from SendResult alone
    s = ScriptedSession(sends=("accept_silent",), correlation_field=None, idempotency_key=False)
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    before = outbox_rows(cs, r.message_id)
    assert before[-1][1] == "accepted" and before[-1][2] == "accepted by the scripted vendor"
    u = s.show("user_item", body="go", turn_id="t-1")
    assert svc.read_next(world["launch"]) == 1
    assert outside_epochs(cs, world["launch"]) == []
    assert cs.turn_view(world["launch"], ME).state == "yours"
    assert [(k, m) for k, m, _b in echoes(cs, world["launch"])] == [(u.key, r.message_id)]
    assert outbox_rows(cs, r.message_id) == before


def test_two_candidates_open_outside_epoch(cs, world):
    s = ScriptedSession(sends=("accept_silent",))
    svc = ready(cs, world, s)
    send(svc, cs, world, outbox="ob-00000001")
    send(svc, cs, world, outbox="ob-00000002")
    s.show("user_item", body="go", turn_id="t-1")
    svc.read_next(world["launch"])
    assert echoes(cs, world["launch"]) == []
    assert len(outside_epochs(cs, world["launch"])) == 1
    assert cs.turn_view(world["launch"], ME).state == "taken"


def test_echo_outside_window_opens_epoch(cs, world, clock):
    s = ScriptedSession(sends=("accept_silent",))
    svc = ready(cs, world, s)
    send(svc, cs, world)
    clock.advance(seconds=61)
    s.show("user_item", body="go", turn_id="t-1")
    svc.read_next(world["launch"])
    assert echoes(cs, world["launch"]) == [] and len(outside_epochs(cs, world["launch"])) == 1


def test_echo_at_the_window_edge_is_an_echo(cs, world, clock):
    s = ScriptedSession(sends=("accept_silent",))
    svc = ready(cs, world, s)
    send(svc, cs, world)
    clock.advance(seconds=60)
    s.show("user_item", body="go", turn_id="t-1")
    svc.read_next(world["launch"])
    assert len(echoes(cs, world["launch"])) == 1 and outside_epochs(cs, world["launch"]) == []


def test_echo_in_another_turn_opens_epoch(cs, world):
    s = ScriptedSession(sends=("accept_silent",))
    svc = ready(cs, world, s)
    send(svc, cs, world)
    s.show("user_item", body="go", turn_id="t-2")
    svc.read_next(world["launch"])
    assert echoes(cs, world["launch"]) == [] and len(outside_epochs(cs, world["launch"])) == 1


def test_echo_with_other_words_opens_epoch(cs, world):
    s = ScriptedSession(sends=("accept_silent",))
    svc = ready(cs, world, s)
    send(svc, cs, world)
    s.show("user_item", body="stop", turn_id="t-1")
    svc.read_next(world["launch"])
    assert echoes(cs, world["launch"]) == [] and len(outside_epochs(cs, world["launch"])) == 1


def test_ambiguous_send_and_matching_item_stays_ambiguous_no_epoch(cs, world):
    # no turn id from the vendor's (missing) reply: the turn the service recorded as current at the attempt
    s = ScriptedSession(sends=("lose",))
    s.show("turn_started", "t-1", turn_id="t-1")
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    assert r.state == "ambiguous"
    s.show("user_item", body="go", turn_id="t-1")
    svc.read_next(world["launch"])
    assert [m for _k, m, _b in echoes(cs, world["launch"])] == [r.message_id]
    assert outside_epochs(cs, world["launch"]) == []
    assert outbox_rows(cs, r.message_id)[-1][1] == "ambiguous"
    assert svc.candidates(r.message_id)                          # §7.3 still waits for your mark


def test_attempt_with_no_recorded_turn_is_no_candidate(cs, world):
    # neither a vendor turn nor a turn the service had recorded: no same-turn match, so the line yields
    s = ScriptedSession(sends=("lose",))
    svc = ready(cs, world, s)
    send(svc, cs, world)
    s.show("user_item", body="go", turn_id="t-1")
    svc.read_next(world["launch"])
    assert echoes(cs, world["launch"]) == [] and len(outside_epochs(cs, world["launch"])) == 1


def test_ended_turn_at_attempt_is_not_current(cs, world):
    s = ScriptedSession(sends=("lose",))
    s.show("turn_started", "t-1", turn_id="t-1")
    s.show("turn_ended", "t-1", turn_id="t-1")
    svc = ready(cs, world, s)
    send(svc, cs, world)
    s.show("user_item", body="go", turn_id="t-1")
    svc.read_next(world["launch"])
    assert echoes(cs, world["launch"]) == [] and len(outside_epochs(cs, world["launch"])) == 1


def test_second_identical_item_yields(cs, world):
    # UNIQUE (message_id): one echo per message; the second matching item has no candidate
    s = ScriptedSession(sends=("accept_silent",))
    svc = ready(cs, world, s)
    send(svc, cs, world)
    s.show("user_item", body="go", turn_id="t-1")
    s.show("user_item", body="go", turn_id="t-1")
    svc.read_next(world["launch"])
    assert len(echoes(cs, world["launch"])) == 1 and len(outside_epochs(cs, world["launch"])) == 1


def test_outside_line_ends_your_epoch_and_fences_your_send(cs, world):
    s = ScriptedSession(sends=("accept_silent",))
    svc = ready(cs, world, s)
    epoch = cs.turn_claim(world["launch"], ME)
    s.show("user_item", body="typed in the terminal", turn_id="t-1")
    svc.read_next(world["launch"])
    hist = outside_epochs(cs, world["launch"])
    assert len(hist) == 1 and hist[0][4] == "terminal" and hist[0][7] == "service"
    r = svc.send(world["launch"], author_kind="operator", author_surface="cockpit", client_id="tab-a",
                 outbox_id="ob-00000009", body="go", expected_epoch=epoch)
    assert r.state == "refused"


def test_coordinator_queue_unaffected_by_outside_epoch(cs, world):
    # B11: the coordinator queues unfenced; an outside epoch does not refuse it
    s = ScriptedSession(sends=("accept",))
    svc = ready(cs, world, s)
    s.show("user_item", body="typed in the terminal", turn_id="t-1")
    svc.read_next(world["launch"])
    r = svc.send(world["launch"], author_kind="coordinator", author_surface="coordinator", client_id="coord-1",
                 outbox_id="ob-00000010", body="next step")
    assert r.state == "accepted"


def test_backfill_never_opens_an_epoch(cs, world):
    s = ScriptedSession()
    s.show("user_item", body="from before Levain read the thread", turn_id="t-0")
    ready(cs, world, s)
    assert outside_epochs(cs, world["launch"]) == [] and echoes(cs, world["launch"]) == []


def test_proven_item_opens_no_epoch_and_writes_no_echo(cs, world):
    # §7.3 step 1: the vendor echoes our op_id in the declared field
    s = ScriptedSession(sends=("ambiguous",), correlation_field="uuid")
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    svc.read_next(world["launch"])
    assert outbox_rows(cs, r.message_id)[-1][1] == "accepted"
    assert outside_epochs(cs, world["launch"]) == [] and echoes(cs, world["launch"]) == []


def test_own_accepted_item_opens_no_epoch_and_writes_no_echo(cs, world):
    # the send's own vendor_item_id names the item: it is ours, not a probable echo
    s = ScriptedSession(sends=("accept",))
    svc = ready(cs, world, s)
    send(svc, cs, world)
    svc.read_next(world["launch"])
    assert outside_epochs(cs, world["launch"]) == [] and echoes(cs, world["launch"]) == []


def test_late_item_of_an_ended_launch_opens_nothing(cs, world, store):
    s = ScriptedSession()
    svc = ready(cs, world, s)
    store.launch_end(world["launch"], "ended", "the worker exited")
    s.show("user_item", body="typed after the end", turn_id="t-1")
    assert svc.read_next(world["launch"]) == 1
    assert outside_epochs(cs, world["launch"]) == []


def test_replayed_batch_opens_no_second_epoch(cs, world):
    s = ScriptedSession()
    svc = ready(cs, world, s)
    s.show("user_item", body="typed in the terminal", turn_id="t-1")
    svc.read_next(world["launch"])
    svc.ingest(world["launch"], s.read(None))                       # the whole thread again: nothing is new
    assert len([r for r in cs.turn_history(world["launch"]) if r[1] in ("outside", "outside_beat")]) == 1


# --- B12: an echo row never changes an outbox state -----------------------------------------------------------------
def test_echo_row_never_changes_outbox_state(cs, world):
    s = ScriptedSession(sends=("lose",))
    s.show("turn_started", "t-1", turn_id="t-1")
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    with cs._write() as (conn, _now):
        all_before = conn.execute("SELECT * FROM chat_outbox_events ORDER BY gseq").fetchall()
    s.show("user_item", body="go", turn_id="t-1")
    svc.read_next(world["launch"])
    assert len(echoes(cs, world["launch"])) == 1
    with cs._write() as (conn, _now):
        assert conn.execute("SELECT * FROM chat_outbox_events ORDER BY gseq").fetchall() == all_before
    assert outbox_rows(cs, r.message_id)[-1][1] == "ambiguous"


def _echo_writers_that_write_outbox(source: str, name: str) -> list[str]:
    """Every function in ``source`` that names ``chat_echoes`` and also writes ``chat_outbox_events``."""
    bad = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            text = ast.get_source_segment(source, node) or ""
            if "chat_echoes" in text and ("outbox_step(" in text or "INTO chat_outbox_events" in text):
                bad.append(f"{name}:{node.name}")
    return bad


def test_no_module_reads_chat_echoes_to_write_outbox():
    # B4 / B12 (static, §12): no function that names chat_echoes writes chat_outbox_events
    root = Path(levain.__file__).parent
    bad = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "chat_echoes" in source:
            bad += _echo_writers_that_write_outbox(source, str(path.relative_to(root)))
    assert bad == []
    # the check itself catches the shape it forbids (a broken fake)
    fake = "def f(conn):\n    conn.execute('SELECT 1 FROM chat_echoes')\n    outbox_step(conn, 'x')\n"
    assert _echo_writers_that_write_outbox(fake, "fake") == ["fake:f"]


def test_chat_echoes_is_append_only_and_one_per_message(cs, world, qdir):
    s = ScriptedSession(sends=("accept_silent",))
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    s.show("user_item", body="go", turn_id="t-1")
    svc.read_next(world["launch"])
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("UPDATE chat_echoes SET basis = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("DELETE FROM chat_echoes")
    key = echoes(cs, world["launch"])[0][0]
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO chat_echoes (launch_id, vendor_event_key, message_id, gseq, basis, at) VALUES "
                  "(?, 'user_item:other', ?, 999999, 'b', 'x')", (world["launch"], r.message_id))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT OR REPLACE INTO chat_echoes (launch_id, vendor_event_key, message_id, gseq, basis, at) "
                  "VALUES (?, ?, ?, 999998, 'b', 'x')", (world["launch"], key, r.message_id))


# --- §2.8: the timeline's echo join -----------------------------------------------------------------------------------
def test_timeline_renders_an_echo_as_the_vendors_copy(cs, world, qdir):
    s = ScriptedSession(sends=("accept_silent",))
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    u = s.show("user_item", body="go", turn_id="t-1")
    o = s.show("user_item", body="typed in the terminal", turn_id="t-1")
    svc.read_next(world["launch"])
    _before, merged = cs.timeline(world["thread"])
    by_ref = {row.ref_id: row for row in merged}
    assert by_ref[u.key].kind == "vendor_echo" and by_ref[u.key].echo_of == r.message_id
    assert by_ref[o.key].kind == "vendor_user_item" and by_ref[o.key].echo_of is None
    assert by_ref[r.message_id].kind == "message" and by_ref[r.message_id].echo_of is None


# --- head ruling Q-A: the one ensure path; a released store is brought current ---------------------------------------
def _released_store(qdir, store, world):
    """A dispatch.db holding the v0.11.25 chat tables and rows written by v0.11.25's own writers' SQL."""
    c = raw(qdir)
    c.execute("BEGIN IMMEDIATE")
    for stmt in RELEASED_CHAT_SCHEMA_V0_11_25:
        c.execute(stmt)
    now = "2026-10-25T12:00:00Z"
    m, _ = chat.record_message(c, now, thread_id=world["thread"], recipient="worker", author_kind="operator",
                               author_surface="cockpit", client_id="tab-a", outbox_id="ob-released1", body="go",
                               normalized="go", launch_id=world["launch"])
    chat.outbox_step(c, now, m.message_id, "refused", cause="released")
    chat.record_event(c, now, launch_id=world["launch"], key="agent_text:a1", pos="00000001", kind="agent_text",
                      body="hello", normalized="hello")
    chat.record_cursor(c, now, launch_id=world["launch"], vendor_cursor="00000001", complete_through="00000001",
                       gap=False)
    record_check(c, now, world["launch"], "live", "activation", "released")
    c.execute("COMMIT")
    return c, m


def _rows(c):
    return {t: c.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall() for t in CHAT_TABLES}


def _view_sql(c):
    return c.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'chat_timeline'").fetchone()[0]


def test_released_store_opens_through_the_cli_keeps_every_row_and_gains_the_echo_view(qdir, store, world):
    from levain.coordinator.cli import run_chat

    c, m = _released_store(qdir, store, world)
    rows, old_view = _rows(c), _view_sql(c)
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'chat_echoes'").fetchone() is None
    c.close()

    class A:
        queue_dir = qdir
        json = False
        thread_id = world["thread"]
        chat_cmd = "show"

    out = io.StringIO()
    assert run_chat(A(), out) == 0
    text = out.getvalue()
    assert 'operator said [refused]: "go"' in text and "vendor agent_text (quoted data)" in text
    c = raw(qdir)
    assert _rows(c) == rows                                            # every row, rowids included
    assert _view_sql(c) != old_view and "chat_echoes" in _view_sql(c)
    assert c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chat_echoes'").fetchone()
    assert c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert [r[1] for r in c.execute("PRAGMA table_info(chat_timeline)")][-1] == "echo_of"


def test_released_store_reads_through_a_read_only_connection(qdir, store, world):
    # a reader that opens read-only migrates nothing and still reads the released shape
    c, m = _released_store(qdir, store, world)
    c.close()
    ro = sqlite3.connect((qdir / "dispatch.db").resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    _before, merged = chat.timeline(ro, world["thread"])
    assert [(r.kind, r.echo_of) for r in merged] == [("message", None), ("vendor_agent_text", None)]
    assert ro.execute("SELECT 1 FROM sqlite_master WHERE name = 'chat_echoes'").fetchone() is None


def test_current_store_open_changes_nothing(qdir, store, world, monkeypatch):
    ChatStore(store)._connect().close()
    c = raw(qdir)
    schema = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    ran = []
    monkeypatch.setattr(chat, "_integrity", lambda conn: ran.append(1) or [("ok",)])
    ChatStore(store)._connect().close()
    assert c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall() == schema
    assert ran == []                                                   # nothing changed: no integrity pass


def test_failed_ensure_rolls_everything_back(qdir, store, world, monkeypatch):
    c, _m = _released_store(qdir, store, world)
    rows, old_view = _rows(c), _view_sql(c)
    monkeypatch.setattr(chat, "CHAT_SCHEMA", (*chat.CHAT_SCHEMA, "CREATE TABLE chat_broken ("))
    with pytest.raises(sqlite3.OperationalError):
        ChatStore(store)._connect()
    assert _view_sql(c) == old_view and _rows(c) == rows
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'chat_echoes'").fetchone() is None


def test_failed_integrity_check_rolls_everything_back(qdir, store, world, monkeypatch):
    c, _m = _released_store(qdir, store, world)
    old_view = _view_sql(c)
    monkeypatch.setattr(chat, "_integrity", lambda conn: [("*** in database main ***",)])
    with pytest.raises(CoordError, match="integrity"):
        ChatStore(store)._connect()
    assert _view_sql(c) == old_view
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'chat_echoes'").fetchone() is None


def test_added_column_reaches_a_released_table(qdir, store, world, monkeypatch):
    # the step D1b-3 and D1c-C extend: an additive column on a table that exists, in the same transaction
    c, _m = _released_store(qdir, store, world)
    rows = _rows(c)
    monkeypatch.setattr(chat, "CHAT_ADDED_COLUMNS", (("chat_launch_checks", "probe_note", "TEXT"),))
    ChatStore(store)._connect().close()
    cols = [r[1] for r in c.execute("PRAGMA table_info(chat_launch_checks)")]
    assert cols[-1] == "probe_note"
    assert c.execute("SELECT probe_note FROM chat_launch_checks").fetchall() == [(None,)]
    assert _rows(c)["chat_messages"] == rows["chat_messages"]


def test_non_additive_column_is_refused_and_nothing_changes(qdir, store, world, monkeypatch):
    c, _m = _released_store(qdir, store, world)
    old_view = _view_sql(c)
    monkeypatch.setattr(chat, "CHAT_ADDED_COLUMNS", (("chat_launch_checks", "probe_note", "TEXT NOT NULL"),))
    with pytest.raises(sqlite3.OperationalError):
        ChatStore(store)._connect()
    assert _view_sql(c) == old_view
    assert "probe_note" not in [r[1] for r in c.execute("PRAGMA table_info(chat_launch_checks)")]


def test_reader_of_a_stale_view_is_brought_current(qdir, store, world):
    c, _m = _released_store(qdir, store, world)
    c.close()
    cs = ChatStore(store)
    cs.timeline(world["thread"])                                       # a read: the stale view is not current
    assert "chat_echoes" in _view_sql(raw(qdir))


# --- the cursor read-back ---------------------------------------------------------------------------------------------
class Recording(ScriptedSession):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.cursors: list[str | None] = []

    def read(self, cursor):
        self.cursors.append(cursor)
        return super().read(cursor)


def test_read_next_resumes_from_the_last_persisted_cursor(cs, world):
    s = Recording()
    svc = ready(cs, world, s)
    assert s.cursors == [None]                                         # nothing persisted yet: from the start
    s.show("agent_text", body="one")
    assert svc.read_next(world["launch"]) == 1
    s.show("agent_text", body="two")
    assert svc.read_next(world["launch"]) == 1
    assert s.cursors == [None, "00000000", "00000001"]
    svc.detach(world["launch"])
    again = Recording()
    again._shown, again._n = list(s._shown), s._n                      # the same vendor thread, a new session
    restarted = ChatService(cs)
    restarted.attach(world["launch"], again)
    assert restarted.read_next(world["launch"]) == 0
    assert again.cursors == ["00000002"]


def test_read_next_without_a_session_is_refused(cs, world):
    with pytest.raises(CoordError, match="no driver session"):
        ChatService(cs).read_next(world["launch"])


# --- §9: the unfenced interrupt (the approval-row half is CH-D1c's) ---------------------------------------------------
class Interrupts(ScriptedSession):
    def __init__(self, result=None, raises=None, **kw) -> None:
        super().__init__(**kw)
        self.got: list[InterruptCommand] = []
        self._result, self._raises = result, raises

    def interrupt(self, command):
        self.got.append(command)
        if self._raises is not None:
            raise self._raises
        return self._result or super().interrupt(command)


def test_interrupt_calls_the_session_and_writes_nothing(cs, world, qdir):
    s = Interrupts()
    svc = ready(cs, world, s)
    cs.turn_claim(world["launch"], "phone:iphone-1")                   # someone else holds the turn: unfenced
    c = raw(qdir)
    before = c.execute("SELECT MAX(gseq) FROM gseq").fetchone()
    got = svc.interrupt(world["launch"], "t-1")
    assert got == CommandResult("confirmed", "interrupted")
    assert len(s.got) == 1 and s.got[0].turn_id == "t-1" and s.got[0].op_id.startswith("op-")
    assert c.execute("SELECT MAX(gseq) FROM gseq").fetchone() == before
    svc.interrupt(world["launch"], None, owner_epoch=7)
    assert s.got[1].turn_id is None and s.got[1].op_id != s.got[0].op_id


def test_interrupt_driver_failure_is_ambiguous(cs, world):
    svc = ready(cs, world, Interrupts(raises=ConnectionError("pipe closed")))
    got = svc.interrupt(world["launch"], "t-1")
    assert got.outcome == "ambiguous" and "ConnectionError" in got.detail
    svc2 = ChatService(cs)
    svc2.attach(world["launch"], Interrupts(result="not a result"))
    assert svc2.interrupt(world["launch"], "t-1").outcome == "ambiguous"


def test_interrupt_without_a_session_is_refused(cs, world):
    with pytest.raises(CoordError, match="no driver session"):
        ChatService(cs).interrupt(world["launch"], "t-1")


# --- L3 r1 K1: a first batch's backfill is classified against its own frontier ---------------------------------------
def _attached(cs, world, session):
    with cs._write() as (conn, now):
        record_check(conn, now, world["launch"], "live", "activation", "test")
    svc = ChatService(cs)
    svc.attach(world["launch"], session)
    return svc


def _backfill(cs, launch):
    with cs._write() as (conn, _now):
        return dict(conn.execute("SELECT vendor_event_key, backfill FROM chat_events WHERE launch_id = ?", (launch,)))


def test_first_batch_classifies_backfill_against_its_own_frontier(cs, world):
    # no frontier persisted yet: only what the batch's own complete_through covers is backfill
    svc = _attached(cs, world, ScriptedSession())
    batch = ReadBatch((event("user_item", "u1", "00000001", body="before Levain", turn_id="t-0"),
                       event("user_item", "u2", "00000200", body="typed in the terminal", turn_id="t-0")),
                      "00000200", "00000001", False)
    assert svc.ingest(world["launch"], batch) == 2
    assert _backfill(cs, world["launch"]) == {"user_item:u1": 1, "user_item:u2": 0}
    assert len(outside_epochs(cs, world["launch"])) == 1
    assert cs.turn_view(world["launch"], ME).state == "taken"


def test_first_batch_with_no_frontier_has_no_backfill(cs, world):
    svc = _attached(cs, world, ScriptedSession())
    batch = ReadBatch((event("user_item", "u1", "00000001", body="typed in the terminal", turn_id="t-0"),),
                      None, None, False)
    svc.ingest(world["launch"], batch)
    assert _backfill(cs, world["launch"]) == {"user_item:u1": 0}
    assert len(outside_epochs(cs, world["launch"])) == 1


# --- L3 r1 K3: an attempt whose stamp cannot be read is not an echo, never a wedged ingest ----------------------------
def test_unreadable_attempt_stamp_is_no_echo_and_ingest_goes_on(cs, world):
    s = ScriptedSession()
    svc = ready(cs, world, s)
    with cs._write() as (conn, now):
        m, _ = chat.record_message(conn, now, thread_id=world["thread"], recipient="worker", author_kind="operator",
                                   author_surface="cockpit", client_id="tab-a", outbox_id="ob-badstamp", body="go",
                                   normalized="go", launch_id=world["launch"])
        chat.outbox_step(conn, now, m.message_id, "pending", cause="preflight passed")
        # a row another writer stamped in a shape parse_stamp refuses (the triggers hold the edge, not the stamp)
        conn.execute("INSERT INTO chat_outbox_events (message_id, seq, gseq, state, op_id, vendor_anchor, cause, by, "
                     "at) VALUES (?, 2, ?, 'attempted', 'op-badstamp', '00000000', 'attempt', 'service', "
                     "'yesterday')", (m.message_id, next_gseq(conn, now)))
        chat.outbox_step(conn, now, m.message_id, "accepted", cause="accepted", vendor_turn_id="t-1",
                         vendor_item_id="i-9")
    s.show("user_item", body="go", turn_id="t-1")
    assert svc.read_next(world["launch"]) == 1
    assert echoes(cs, world["launch"]) == [] and len(outside_epochs(cs, world["launch"])) == 1
    s.show("agent_text", body="still reading")
    assert svc.read_next(world["launch"]) == 1


# --- L3 r1 K4: the read-back starts at the NEWEST persisted cursor, a NULL one included -------------------------------
def test_read_next_after_a_null_cursor_does_not_resurrect_an_older_one(cs, world):
    s = Recording()
    svc = ready(cs, world, s)
    assert s.cursors == [None]
    svc.read_next(world["launch"])
    assert s.cursors[-1] == "00000000"
    svc.ingest(world["launch"], ReadBatch((), None, None, False))     # the vendor's stream reset: no cursor
    svc.read_next(world["launch"])
    assert s.cursors[-1] is None


# --- L3 r1 K2: an interrupt pins its session's lifetime; K9: its turn id is checked -----------------------------------
class Blocking(ScriptedSession):
    def __init__(self, log, **kw) -> None:
        super().__init__(**kw)
        self.log = log
        self.entered, self.release = threading.Event(), threading.Event()

    def interrupt(self, command):
        self.log.append(("interrupt", "start", self.closed))
        self.entered.set()
        assert self.release.wait(5)
        self.log.append(("interrupt", "end", self.closed))
        return super().interrupt(command)

    def close(self):
        self.log.append(("close",))
        super().close()


def test_detach_waits_for_an_interrupt_in_flight_and_refuses_new_ones(cs, world):
    log: list[tuple] = []
    s1 = Blocking(log)
    svc = ready(cs, world, s1)
    got: dict = {}
    s2 = Interrupts()
    a = threading.Thread(target=lambda: got.setdefault("a", svc.interrupt(world["launch"], "t-1")))
    b = threading.Thread(target=svc.detach, args=(world["launch"],))
    try:
        a.start()
        assert s1.entered.wait(5)
        b.start()
        b.join(0.3)
        assert b.is_alive() and not s1.closed                          # the close waits for the interrupt
        with pytest.raises(CoordError, match="being detached"):
            svc.interrupt(world["launch"], "t-1")                      # no new pin on a session being detached
        with pytest.raises(CoordError, match="detach it first"):
            svc.attach(world["launch"], s2)                            # nothing replaces it before it is closed
    finally:
        s1.release.set()
        for t in (a, b):
            if t.ident is not None:
                t.join(5)
    assert got["a"].outcome == "confirmed"
    assert log == [("interrupt", "start", False), ("interrupt", "end", False), ("close",)]
    svc.attach(world["launch"], s2)
    assert svc.interrupt(world["launch"], "t-2").outcome == "confirmed"
    assert [x.turn_id for x in s2.got] == ["t-2"]


@pytest.mark.parametrize("turn_id", [7, "", "t" * 513, "t-1\nt-2", b"t-1"])
def test_interrupt_refuses_a_turn_id_that_is_not_one(cs, world, turn_id):
    s = Interrupts()
    svc = ready(cs, world, s)
    with pytest.raises(CoordError, match="turn id"):
        svc.interrupt(world["launch"], turn_id)
    assert s.got == []


# --- L3 r1 K6: every view, trigger and index CHAT_SCHEMA creates is compared by its stored text -----------------------
def _stmt(prefix):
    return next(s for s in chat.CHAT_SCHEMA if s.startswith(prefix))


def _older_trigger_and_index(c):
    # an older release's text of a trigger and an index, under the names CHAT_SCHEMA uses
    c.execute("BEGIN IMMEDIATE")
    c.execute("DROP TRIGGER chat_cursors_next")
    c.execute("CREATE TRIGGER chat_cursors_next BEFORE INSERT ON chat_cursors WHEN 0 BEGIN SELECT RAISE(ABORT, 'old'); "
              "END")
    c.execute("DROP INDEX chat_outbox_one_attempt")
    c.execute("CREATE INDEX chat_outbox_one_attempt ON chat_outbox_events (message_id)")
    c.execute("COMMIT")


def _current_trigger_and_index(c):
    stored = dict(c.execute("SELECT name, sql FROM sqlite_master WHERE name IN ('chat_cursors_next', "
                            "'chat_outbox_one_attempt')").fetchall())
    assert stored["chat_cursors_next"] == _stmt("CREATE TRIGGER IF NOT EXISTS chat_cursors_next ").replace(
        "IF NOT EXISTS ", "", 1)
    assert stored["chat_outbox_one_attempt"] == _stmt("CREATE UNIQUE INDEX IF NOT EXISTS chat_outbox_one_attempt "
                                                      ).replace("IF NOT EXISTS ", "", 1)
    unique = {r[1]: r[2] for r in c.execute("PRAGMA index_list(chat_outbox_events)")}
    assert unique["chat_outbox_one_attempt"] == 1


def test_released_store_with_an_older_trigger_and_index_is_brought_current(qdir, store, world):
    c, _m = _released_store(qdir, store, world)
    _older_trigger_and_index(c)
    rows = _rows(c)
    ChatStore(store)._connect().close()
    _current_trigger_and_index(c)
    assert _rows(c) == rows and c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    with pytest.raises(sqlite3.IntegrityError, match="next seq"):     # the current trigger holds again
        c.execute("INSERT INTO chat_cursors (launch_id, seq, gseq, vendor_cursor, complete_through, gap, at) VALUES "
                  "(?, 9, 999999, NULL, NULL, 0, 'x')", (world["launch"],))


def test_reader_of_an_older_trigger_brings_it_current(qdir, store, world):
    ChatStore(store)._connect().close()
    c = raw(qdir)
    _older_trigger_and_index(c)
    ChatStore(store).timeline(world["thread"])                         # a read: the older trigger is not current
    _current_trigger_and_index(c)


# --- L3 r1 K7: a reader of a file it cannot write never runs the ensure path ------------------------------------------
def test_reader_of_a_read_only_released_store_reads_it_and_changes_nothing(qdir, store, world, clock):
    c, m = _released_store(qdir, store, world)
    schema = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    c.close()
    db = qdir / "dispatch.db"
    db.chmod(0o444)
    try:
        cs = ChatStore(EnvelopeStore(qdir, clock=clock))
        _before, merged = cs.timeline(world["thread"])
        assert [(r.kind, r.echo_of) for r in merged] == [("message", None), ("vendor_agent_text", None)]
        assert cs.message(m.message_id).body == "go"
    finally:
        db.chmod(0o600)
    c = raw(qdir)
    assert c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall() == schema


# --- L3 r1 K10: the fingerprint covers attempts: one committed between the batch read and its write refuses the write -
class AttemptsMidRead(ScriptedSession):
    """A vendor whose ``pos_lt`` (called between the ingest's read and its write) lets another writer commit an
    attempt of the launch first, once."""

    def __init__(self, cs, world, **kw) -> None:
        super().__init__(**kw)
        self.cs, self.world, self.armed = cs, world, False

    def pos_lt(self, a, b):
        if self.armed:
            self.armed = False
            with self.cs._write() as (conn, now):
                m, _ = chat.record_message(conn, now, thread_id=self.world["thread"], recipient="worker",
                                           author_kind="coordinator", author_surface="coordinator", client_id="coord",
                                           outbox_id="ob-midread1", body="go", normalized="go",
                                           launch_id=self.world["launch"])
                chat.outbox_step(conn, now, m.message_id, "pending", cause="preflight passed")
                chat.outbox_step(conn, now, m.message_id, "attempted", cause="attempt", op_id="op-midread",
                                 vendor_anchor="00000000")
        return super().pos_lt(a, b)


def test_an_attempt_committed_between_the_read_and_the_write_refuses_the_batch(cs, world):
    s = AttemptsMidRead(cs, world)
    svc = ready(cs, world, s)
    s.show("user_item", body="go", turn_id="t-1")
    s.armed = True
    with pytest.raises(CoordError, match="changed while its batch was read"):
        svc.read_next(world["launch"])
    assert _backfill(cs, world["launch"]) == {}                        # nothing of the batch was written
    assert outside_epochs(cs, world["launch"]) == [] and echoes(cs, world["launch"]) == []
    assert svc.read_next(world["launch"]) == 1                         # read again over the new attempt


# --- the released shape (v0.11.25's CHAT_SCHEMA, frozen) -------------------------------------------------------------
RELEASED_CHAT_SCHEMA_V0_11_25 = (
    'CREATE TABLE IF NOT EXISTS chat_messages (message_id TEXT PRIMARY KEY, gseq INTEGER NOT NULL UNIQUE,'
    ' thread_id TEXT NOT NULL REFERENCES threads(thread_id), launch_id TEXT REFERENCES launches(launch_id'
    "), recipient TEXT NOT NULL CHECK (recipient IN ('coordinator', 'worker', 'thread')), author_kind TEX"
    "T NOT NULL CHECK (author_kind IN ('operator', 'coordinator')), author_surface TEXT NOT NULL CHECK (a"
    "uthor_surface IN ('cockpit', 'phone', 'cli', 'coordinator')), client_id TEXT NOT NULL, outbox_id TEX"
    "T NOT NULL UNIQUE, mode TEXT NOT NULL CHECK (mode IN ('queue', 'steer')), expected_turn_id TEXT, own"
    'er_epoch INTEGER, body TEXT NOT NULL, body_sha256 TEXT NOT NULL, body_norm_sha256 TEXT NOT NULL, att'
    "achments TEXT NOT NULL, draft_ref TEXT, at TEXT NOT NULL, CHECK ((recipient = 'worker') = (launch_id"
    " IS NOT NULL)), CHECK ((author_kind = 'coordinator') = (author_surface = 'coordinator')), CHECK (aut"
    "hor_kind = 'operator' OR (mode = 'queue' AND owner_epoch IS NULL)), CHECK ((mode = 'steer') = (expec"
    "ted_turn_id IS NOT NULL)), CHECK (mode = 'queue' OR recipient = 'worker'), CHECK (length(body) <= 20"
    '000))',
    'CREATE TRIGGER IF NOT EXISTS chat_messages_frozen BEFORE UPDATE ON chat_messages BEGIN SELECT RAISE('
    "ABORT, 'a chat message is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_messages_kept BEFORE DELETE ON chat_messages BEGIN SELECT RAISE(AB'
    "ORT, 'a chat message is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_messages_no_replace BEFORE INSERT ON chat_messages WHEN EXISTS (SE'
    'LECT 1 FROM chat_messages WHERE message_id = NEW.message_id OR outbox_id = NEW.outbox_id OR gseq = N'
    "EW.gseq OR rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a chat message is never replaced'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_messages_worker BEFORE INSERT ON chat_messages WHEN NEW.launch_id '
    'IS NOT NULL AND NOT EXISTS (SELECT 1 FROM launches l, json_each(l.threads) j WHERE l.launch_id = NEW'
    ".launch_id AND l.role = 'worker' AND j.value = NEW.thread_id) BEGIN SELECT RAISE(ABORT, 'a chat mess"
    "age to a worker names a worker launch of its thread'); END",
    'CREATE TABLE IF NOT EXISTS chat_outbox_events (message_id TEXT NOT NULL REFERENCES chat_messages(mes'
    "sage_id), seq INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, state TEXT NOT NULL CHECK (state IN ('"
    "refused', 'pending', 'attempted', 'accepted', 'ambiguous', 'not_delivered', 'cancelled')), op_id TEX"
    'T, vendor_anchor TEXT, vendor_turn_id TEXT, vendor_item_id TEXT, cause TEXT NOT NULL, by TEXT NOT NU'
    "LL CHECK (by IN ('service', 'operator')), at TEXT NOT NULL, PRIMARY KEY (message_id, seq), CHECK (st"
    "ate <> 'attempted' OR (op_id IS NOT NULL AND vendor_anchor IS NOT NULL)), CHECK (state = 'attempted'"
    " OR (op_id IS NULL AND vendor_anchor IS NULL)), CHECK (state IN ('accepted', 'ambiguous', 'not_deliv"
    "ered') OR (vendor_turn_id IS NULL AND vendor_item_id IS NULL)))",
    'CREATE UNIQUE INDEX IF NOT EXISTS chat_outbox_one_attempt ON chat_outbox_events (message_id) WHERE s'
    "tate = 'attempted'",
    'CREATE TRIGGER IF NOT EXISTS chat_outbox_events_frozen BEFORE UPDATE ON chat_outbox_events BEGIN SEL'
    "ECT RAISE(ABORT, 'a chat outbox event is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_outbox_events_kept BEFORE DELETE ON chat_outbox_events BEGIN SELEC'
    "T RAISE(ABORT, 'a chat outbox event is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_outbox_events_next BEFORE INSERT ON chat_outbox_events WHEN NEW.se'
    'q != COALESCE((SELECT MAX(seq) FROM chat_outbox_events WHERE message_id = NEW.message_id), 0) + 1 OR'
    ' EXISTS (SELECT 1 FROM chat_outbox_events WHERE rowid = NEW.rowid OR gseq = NEW.gseq) BEGIN SELECT R'
    "AISE(ABORT, 'an outbox event is the next seq of its message'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_outbox_events_edges BEFORE INSERT ON chat_outbox_events WHEN ((SEL'
    'ECT state FROM chat_outbox_events WHERE message_id = NEW.message_id ORDER BY seq DESC LIMIT 1) IS NU'
    "LL AND NEW.state NOT IN ('refused', 'pending')) OR ((SELECT state FROM chat_outbox_events WHERE mess"
    "age_id = NEW.message_id ORDER BY seq DESC LIMIT 1) = 'pending' AND NEW.state NOT IN ('attempted', 'c"
    "ancelled', 'not_delivered')) OR ((SELECT state FROM chat_outbox_events WHERE message_id = NEW.messag"
    "e_id ORDER BY seq DESC LIMIT 1) = 'attempted' AND NEW.state NOT IN ('accepted', 'ambiguous', 'not_de"
    "livered')) OR ((SELECT state FROM chat_outbox_events WHERE message_id = NEW.message_id ORDER BY seq "
    "DESC LIMIT 1) = 'ambiguous' AND NEW.state NOT IN ('accepted', 'not_delivered')) OR (SELECT state FRO"
    "M chat_outbox_events WHERE message_id = NEW.message_id ORDER BY seq DESC LIMIT 1) IN ('accepted', 'r"
    "efused', 'cancelled', 'not_delivered') OR (NEW.by = 'operator' AND (SELECT state FROM chat_outbox_ev"
    "ents WHERE message_id = NEW.message_id ORDER BY seq DESC LIMIT 1) IS NOT 'ambiguous') BEGIN SELECT R"
    "AISE(ABORT, 'an outbox event takes an edge §2.2 does not allow'); END",
    'CREATE TABLE IF NOT EXISTS chat_events (launch_id TEXT NOT NULL REFERENCES launches(launch_id), vend'
    'or_event_key TEXT NOT NULL, gseq INTEGER NOT NULL UNIQUE, vendor_pos TEXT NOT NULL, kind TEXT NOT NU'
    "LL CHECK (kind IN ('turn_started', 'turn_ended', 'user_item', 'agent_text', 'tool_call', 'tool_resul"
    "t', 'approval_request', 'approval_resolved', 'error', 'gap')), vendor_turn_id TEXT, call_id TEXT, bo"
    'dy TEXT NOT NULL, body_sha256 TEXT NOT NULL, body_norm_sha256 TEXT NOT NULL, truncated INTEGER NOT N'
    'ULL CHECK (truncated IN (0, 1)), backfill INTEGER NOT NULL CHECK (backfill IN (0, 1)), source TEXT N'
    "OT NULL CHECK (source = 'vendor'), at TEXT NOT NULL, PRIMARY KEY (launch_id, vendor_event_key), CHEC"
    'K (length(CAST(body AS BLOB)) <= 16384), CHECK (substr(vendor_event_key, 1, length(kind) + 1) = kind'
    " || ':' AND length(vendor_event_key) > length(kind) + 1))",
    'CREATE TRIGGER IF NOT EXISTS chat_events_frozen BEFORE UPDATE ON chat_events BEGIN SELECT RAISE(ABOR'
    "T, 'a chat event is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_events_kept BEFORE DELETE ON chat_events BEGIN SELECT RAISE(ABORT,'
    " 'a chat event is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_events_no_replace BEFORE INSERT ON chat_events WHEN EXISTS (SELECT'
    ' 1 FROM chat_events WHERE (launch_id = NEW.launch_id AND vendor_event_key = NEW.vendor_event_key) OR'
    " gseq = NEW.gseq OR rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a chat event is never replaced'); "
    'END',
    'CREATE TABLE IF NOT EXISTS chat_cursors (launch_id TEXT NOT NULL REFERENCES launches(launch_id), seq'
    ' INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, vendor_cursor TEXT, complete_through TEXT, gap INTE'
    'GER NOT NULL CHECK (gap IN (0, 1)), at TEXT NOT NULL, PRIMARY KEY (launch_id, seq))',
    'CREATE TRIGGER IF NOT EXISTS chat_cursors_frozen BEFORE UPDATE ON chat_cursors BEGIN SELECT RAISE(AB'
    "ORT, 'a chat cursor is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_cursors_kept BEFORE DELETE ON chat_cursors BEGIN SELECT RAISE(ABOR'
    "T, 'a chat cursor is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_cursors_next BEFORE INSERT ON chat_cursors WHEN NEW.seq != COALESC'
    'E((SELECT MAX(seq) FROM chat_cursors WHERE launch_id = NEW.launch_id), 0) + 1 OR EXISTS (SELECT 1 FR'
    "OM chat_cursors WHERE rowid = NEW.rowid OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'a cursor is t"
    "he next seq of its launch'); END",
    'CREATE TABLE IF NOT EXISTS chat_anomalies (launch_id TEXT NOT NULL REFERENCES launches(launch_id), s'
    'eq INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, message_id TEXT REFERENCES chat_messages(message_'
    "id), vendor_event_key TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('proof_after_terminal')), de"
    'tail TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (launch_id, seq))',
    'CREATE TRIGGER IF NOT EXISTS chat_anomalies_frozen BEFORE UPDATE ON chat_anomalies BEGIN SELECT RAIS'
    "E(ABORT, 'a chat anomaly is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_anomalies_kept BEFORE DELETE ON chat_anomalies BEGIN SELECT RAISE('
    "ABORT, 'a chat anomaly is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_anomalies_next BEFORE INSERT ON chat_anomalies WHEN NEW.seq != COA'
    'LESCE((SELECT MAX(seq) FROM chat_anomalies WHERE launch_id = NEW.launch_id), 0) + 1 OR EXISTS (SELEC'
    "T 1 FROM chat_anomalies WHERE rowid = NEW.rowid OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'an an"
    "omaly is the next seq of its launch'); END",
    'CREATE TABLE IF NOT EXISTS chat_launch_checks (launch_id TEXT NOT NULL REFERENCES launches(launch_id'
    "), seq INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL CHECK (kind IN ('probing',"
    " 'live', 'not_live', 'reenabled')), source TEXT NOT NULL CHECK (source IN ('activation', 'turn_start"
    "', 'send', 'result', 'operator') OR (source GLOB 'tripwire:?*' AND kind = 'not_live')), evidence_sha"
    '256 TEXT CHECK (evidence_sha256 IS NULL OR (length(evidence_sha256) = 71 AND substr(evidence_sha256,'
    " 1, 7) = 'sha256:' AND substr(evidence_sha256, 8) NOT GLOB '*[^0-9a-f]*')), spawn_nonce TEXT, detail"
    " TEXT NOT NULL, by TEXT NOT NULL CHECK (by IN ('service', 'operator')), at TEXT NOT NULL, PRIMARY KE"
    "Y (launch_id, seq), CHECK ((kind = 'reenabled') = (by = 'operator')), CHECK ((kind = 'reenabled') = "
    "(source = 'operator')), CHECK (spawn_nonce IS NULL OR (source = 'activation' AND length(spawn_nonce)"
    " = 32 AND spawn_nonce NOT GLOB '*[^0-9a-f]*')))",
    'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_frozen BEFORE UPDATE ON chat_launch_checks BEGIN SEL'
    "ECT RAISE(ABORT, 'a launch check is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_kept BEFORE DELETE ON chat_launch_checks BEGIN SELEC'
    "T RAISE(ABORT, 'a launch check is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_next BEFORE INSERT ON chat_launch_checks WHEN NEW.se'
    'q != COALESCE((SELECT MAX(seq) FROM chat_launch_checks WHERE launch_id = NEW.launch_id), 0) + 1 OR E'
    'XISTS (SELECT 1 FROM chat_launch_checks WHERE rowid = NEW.rowid OR gseq = NEW.gseq) BEGIN SELECT RAI'
    "SE(ABORT, 'a launch check is the next seq of its launch'); END",
    'CREATE TRIGGER IF NOT EXISTS chat_launch_checks_reenable BEFORE INSERT ON chat_launch_checks WHEN NE'
    "W.kind = 'reenabled' AND NOT ((SELECT kind FROM chat_launch_checks WHERE launch_id = NEW.launch_id A"
    "ND kind IN ('probing', 'live', 'not_live') ORDER BY seq DESC LIMIT 1) = 'live' AND EXISTS (SELECT 1 "
    "FROM chat_launch_checks WHERE launch_id = NEW.launch_id AND kind = 'not_live' AND gseq > COALESCE((S"
    "ELECT MAX(gseq) FROM chat_launch_checks WHERE launch_id = NEW.launch_id AND kind = 'reenabled'), 0))"
    ") BEGIN SELECT RAISE(ABORT, 'a re-enable clears a latch whose latest probe read live'); END",
    'CREATE TABLE IF NOT EXISTS turn_lease_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, gseq INTEGER NO'
    'T NULL UNIQUE, launch_id TEXT NOT NULL REFERENCES launches(launch_id), kind TEXT NOT NULL CHECK (kin'
    "d IN ('claim', 'handoff', 'take', 'outside', 'beat', 'outside_beat', 'release', 'lapse')), token INT"
    "EGER, owner TEXT NOT NULL, owner_kind TEXT NOT NULL CHECK (owner_kind IN ('client', 'terminal', 'ven"
    "dor_app')), until TEXT, cause TEXT NOT NULL, by TEXT NOT NULL CHECK (by IN ('operator', 'service')),"
    " at TEXT NOT NULL, CHECK ((kind IN ('claim', 'handoff', 'take', 'outside')) = (token IS NULL)), CHEC"
    "K ((kind IN ('release', 'lapse')) = (until IS NULL)), CHECK ((owner_kind = 'client' AND (owner GLOB "
    "'cockpit:?*' OR owner GLOB 'phone:?*' OR owner GLOB 'cli:?*')) OR (owner_kind != 'client' AND owner "
    "= owner_kind)), CHECK ((kind IN ('claim', 'handoff', 'take', 'beat') AND by = 'operator' AND owner_k"
    "ind = 'client') OR (kind IN ('outside', 'outside_beat') AND by = 'service' AND owner_kind != 'client"
    "') OR (kind = 'lapse' AND by = 'service') OR kind = 'release'))",
    'CREATE TRIGGER IF NOT EXISTS turn_lease_events_frozen BEFORE UPDATE ON turn_lease_events BEGIN SELEC'
    "T RAISE(ABORT, 'a turn lease event is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS turn_lease_events_kept BEFORE DELETE ON turn_lease_events BEGIN SELECT '
    "RAISE(ABORT, 'a turn lease event is append-only'); END",
    'CREATE TRIGGER IF NOT EXISTS turn_lease_events_no_replace BEFORE INSERT ON turn_lease_events WHEN EX'
    'ISTS (SELECT 1 FROM turn_lease_events WHERE seq = NEW.seq OR gseq = NEW.gseq) BEGIN SELECT RAISE(ABO'
    "RT, 'a turn lease event is never replaced'); END",
    'CREATE TRIGGER IF NOT EXISTS turn_lease_events_edges BEFORE INSERT ON turn_lease_events WHEN (NEW.ki'
    "nd IN ('claim', 'handoff', 'take', 'outside', 'beat', 'outside_beat') AND NOT EXISTS (SELECT 1 FROM "
    "launches l WHERE l.launch_id = NEW.launch_id AND l.role = 'worker' AND (SELECT kind FROM launch_even"
    "ts e WHERE e.launch_id = l.launch_id ORDER BY seq DESC LIMIT 1) = 'active')) OR (NEW.kind = 'claim' "
    "AND ((SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'hando"
    "ff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) IS NOT NULL AND NOT EXISTS (SELECT 1 FROM turn_le"
    'ase_events WHERE token = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind'
    " IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) AND kind IN ('release', 'laps"
    "e')) AND NEW.at < (SELECT MAX(until) FROM turn_lease_events WHERE seq = (SELECT seq FROM turn_lease_"
    "events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY "
    'seq DESC LIMIT 1) OR (token = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND'
    " kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) AND kind IN ('beat', 'ou"
    "tside_beat'))))) OR (NEW.kind = 'handoff' AND (NOT ((SELECT seq FROM turn_lease_events WHERE launch_"
    "id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) IS"
    ' NOT NULL AND NOT EXISTS (SELECT 1 FROM turn_lease_events WHERE token = (SELECT seq FROM turn_lease_'
    "events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY "
    "seq DESC LIMIT 1) AND kind IN ('release', 'lapse')) AND NEW.at < (SELECT MAX(until) FROM turn_lease_"
    "events WHERE seq = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('"
    "claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) OR (token = (SELECT seq FROM turn_l"
    "ease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDE"
    "R BY seq DESC LIMIT 1) AND kind IN ('beat', 'outside_beat')))) OR (SELECT owner_kind FROM turn_lease"
    '_events WHERE seq = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('
    "'claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1)) != 'client')) OR (NEW.kind NOT IN"
    " ('claim', 'handoff', 'take', 'outside') AND (NEW.token IS NOT (SELECT seq FROM turn_lease_events WH"
    "ERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC "
    'LIMIT 1) OR EXISTS (SELECT 1 FROM turn_lease_events WHERE token = (SELECT seq FROM turn_lease_events'
    " WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DE"
    "SC LIMIT 1) AND kind IN ('release', 'lapse')) OR (SELECT owner FROM turn_lease_events WHERE seq = NE"
    'W.token) IS NOT NEW.owner OR (SELECT owner_kind FROM turn_lease_events WHERE seq = NEW.token) IS NOT'
    " NEW.owner_kind)) OR (NEW.kind IN ('beat', 'outside_beat') AND NOT ((SELECT seq FROM turn_lease_even"
    "ts WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq "
    'DESC LIMIT 1) IS NOT NULL AND NOT EXISTS (SELECT 1 FROM turn_lease_events WHERE token = (SELECT seq '
    "FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take', 'out"
    "side') ORDER BY seq DESC LIMIT 1) AND kind IN ('release', 'lapse')) AND NEW.at < (SELECT MAX(until) "
    'FROM turn_lease_events WHERE seq = (SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_i'
    "d AND kind IN ('claim', 'handoff', 'take', 'outside') ORDER BY seq DESC LIMIT 1) OR (token = (SELECT"
    " seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ('claim', 'handoff', 'take',"
    " 'outside') ORDER BY seq DESC LIMIT 1) AND kind IN ('beat', 'outside_beat'))))) OR (NEW.kind = 'beat"
    "' AND (SELECT kind FROM turn_lease_events WHERE seq = NEW.token) = 'outside') OR (NEW.kind = 'outsid"
    "e_beat' AND (SELECT kind FROM turn_lease_events WHERE seq = NEW.token) != 'outside') BEGIN SELECT RA"
    "ISE(ABORT, 'a turn lease event takes an edge §5 does not allow'); END",
    'CREATE VIEW IF NOT EXISTS chat_timeline (thread_id, gseq, kind, source, ref_id, launch_id, status, b'
    "ody, at) AS SELECT m.thread_id, m.gseq, 'message', m.author_kind, m.message_id, m.launch_id, (SELECT"
    ' o.state FROM chat_outbox_events o WHERE o.message_id = m.message_id ORDER BY o.seq DESC LIMIT 1), m'
    ".body, m.at FROM chat_messages m UNION ALL SELECT j.value, e.gseq, 'vendor_' || e.kind, e.source, e."
    'vendor_event_key, e.launch_id, NULL, e.body, e.at FROM chat_events e JOIN launches l ON l.launch_id '
    "= e.launch_id, json_each(l.threads) j UNION ALL SELECT r.thread_id, r.gseq, CASE r.kind WHEN 'drop_r"
    "eceipt' THEN 'reply_receipt' WHEN 'sign' THEN 'reply_sign' WHEN 'answer' THEN 'reply_answer' WHEN 'k"
    "now' THEN 'reply_know' END, 'levain', r.reply_id, NULL, NULL, r.body, r.at FROM replies r WHERE r.gs"
    "eq IS NOT NULL AND r.thread_id IS NOT NULL UNION ALL SELECT b.thread_id, b.gseq, 'envelope_bound', '"
    "levain', b.envelope_id, NULL, NULL, NULL, b.at FROM envelope_binds b UNION ALL SELECT a.thread_id, a"
    ".gseq, 'attenuation_' || a.kind, a.by, a.envelope_id, NULL, NULL, a.value, a.at FROM envelope_attenu"
    "ations a WHERE a.thread_id IS NOT NULL UNION ALL SELECT b.thread_id, a.gseq, 'attenuation_' || a.kin"
    'd, a.by, a.envelope_id, NULL, NULL, a.value, a.at FROM envelope_attenuations a JOIN envelope_binds b'
    ' ON b.envelope_id = a.envelope_id WHERE a.thread_id IS NULL',
)
