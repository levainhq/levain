"""RM-D2: ChatService for the room (DESIGN_ROOM §2.2 C1-C5, §3) and the ChatService fences the room line left open.

A room launch takes messages, vendor events, cursors and checks like a worker launch; its send is queue-only with no
turn lease and no epoch; ``send_room`` resolves the open room's running launch in the transaction that writes
``pending`` (replay first, a rotated room refused with nothing written); ``session_meta`` is the meta read once at
attach. Also: no send reaches a session while an interrupt waits on it; ``message_thread`` answers only the client
that sent; ``session`` is a snapshot; a turn release waits for a send in flight on its launch."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from levain.desk import chat, lease, room
from levain.desk.chat import ChatService, ChatStore, record_check, record_cursor, record_event, record_message
from levain.desk.chat_lease import TurnRefused
from levain.desk.driver import CommandResult, ScriptedSession, SessionMeta
from levain.desk.envelopes import EnvelopeStore
from levain.desk.store import CoordError, Part
from levain.desk.legacy import ROLE_DESK

LIVE = {"probe": lambda _l, _e: chat.Verdict(True, "scripted probe: live"), "sleep": lambda _s: None}
T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    s = EnvelopeStore(d, clock=Clock())
    s.coord_set_agent("none")
    return s


@pytest.fixture
def cs(store):
    s = ChatStore(store)
    s._connect().close()
    return s


def _thread(store, kind="note", title="the room"):
    d = store.accept_drop(title, surface="cli")[0]
    return store.split(d.drop_id, [Part(kind, title)])[0].thread_id


def _open_room_thread(store, thread):
    with store._write() as (conn, _now):
        room.open_room_thread(conn, thread)


def _room_launch(store, thread, sid):
    with store._write() as (conn, now):
        ln = lease.open_room_launch(conn, now, harness="claude", model=None, cwd=str(store.path.parent),
                                    room_thread=thread, pid=4242)
    with store._write() as (conn, now):
        got, why = lease.activate_room(conn, now, ln.launch_id, harness_session_id=sid)
    assert why is None and got.state == "active"
    return ln.launch_id


def _live(cs, launch):
    with cs._write() as (conn, now):
        record_check(conn, now, launch, "live", "activation", "test")


class Counting(ScriptedSession):
    def __init__(self) -> None:
        super().__init__()
        self.meta_calls = 0
        self.sends = 0

    def meta(self) -> SessionMeta:
        self.meta_calls += 1
        return SessionMeta("room-session", "dontAsk", "claude-room-model", "2.1.317", None, ("Read",), ())

    def send(self, command):
        self.sends += 1
        return super().send(command)


def _attach(svc, cs, launch, session=None):
    _live(cs, launch)
    session = session or Counting()
    svc.attach(launch, session)
    svc.ingest(launch, session.read(None))
    return session


@pytest.fixture
def world(store, cs):
    thread = _thread(store)
    _open_room_thread(store, thread)
    launch = _room_launch(store, thread, "room-sid-1")
    svc = ChatService(cs, **LIVE)
    session = _attach(svc, cs, launch)
    return {"thread": thread, "launch": launch, "svc": svc, "session": session}


def say(svc, room_thread, *, outbox="ob-00000001", body="hello room", client="tab-a", surface="cockpit", **kw):
    return svc.send_room(room_thread, author_surface=surface, client_id=client, outbox_id=outbox, body=body, **kw)


def count(cs, sql, *args):
    return cs._read(lambda conn: conn.execute(sql, args).fetchone()[0])


# --- C5: send_room ------------------------------------------------------------------------------------------------
def test_a_room_send_reaches_the_open_rooms_launch_queued_with_no_epoch(world, cs):
    r = say(world["svc"], world["thread"])
    assert r.state == "accepted" and world["session"].sends == 1
    m = cs.message(r.message_id)
    assert (m.thread_id, m.launch_id, m.recipient, m.mode, m.owner_epoch, m.author_kind) == \
        (world["thread"], world["launch"], "worker", "queue", None, "operator")


def test_a_rotated_room_is_refused_and_nothing_is_written(world, store, cs):
    new = _thread(store, title="next room")
    with store._write() as (conn, _now):
        room.rotate(conn, new)
    before = count(cs, "SELECT COUNT(*) FROM chat_messages")
    with pytest.raises(chat.RoomRotated):
        say(world["svc"], world["thread"])
    assert count(cs, "SELECT COUNT(*) FROM chat_messages") == before and world["session"].sends == 0


def test_a_room_that_was_never_opened_is_refused(world, store):
    other = _thread(store, title="not a room")
    with pytest.raises(chat.RoomRotated):
        say(world["svc"], other)


def test_a_replaced_launch_on_the_same_room_thread_takes_the_next_send(world, store, cs):
    svc, old = world["svc"], world["launch"]
    with store._write() as (conn, now):
        lease.end_launch(conn, now, old, "ended", "vendor exited")
    new = _room_launch(store, world["thread"], "room-sid-2")
    session = _attach(svc, cs, new)
    r = say(svc, world["thread"])
    assert r.state == "accepted" and cs.message(r.message_id).launch_id == new and session.sends == 1


def test_a_resend_is_answered_from_the_record_before_the_launch_is_resolved(world, store, cs):
    svc = world["svc"]
    first = say(svc, world["thread"])
    with store._write() as (conn, now):
        lease.end_launch(conn, now, world["launch"], "ended", "restart")
    new = _room_launch(store, world["thread"], "room-sid-2")
    session = _attach(svc, cs, new)
    again = say(svc, world["thread"])                        # same outbox_id, the launch it went to has ended
    assert (again.message_id, again.state, again.created) == (first.message_id, "accepted", False)
    assert session.sends == 0 and count(cs, "SELECT COUNT(*) FROM chat_messages") == 1


def test_a_resend_after_rotation_is_still_answered_from_the_record(world, store):
    first = say(world["svc"], world["thread"])
    nxt = _thread(store, title="next")
    with store._write() as (conn, _now):
        room.rotate(conn, nxt)
    again = say(world["svc"], world["thread"])
    assert again.message_id == first.message_id and again.created is False


def test_the_launch_is_resolved_inside_the_pending_transaction(world, store, cs, monkeypatch):
    """The launch read before the lock is replaced before the write: the pending row names the launch the write's own
    transaction resolved, never the stale one."""
    svc, old, thread = world["svc"], world["launch"], world["thread"]
    real = chat._open_room_launch
    swapped = {}

    def racing(conn, room_thread):
        got = real(conn, room_thread)
        if not swapped:
            swapped["done"] = True
            with store._write() as (c, now):
                lease.end_launch(c, now, old, "ended", "replaced mid-send")
            swapped["new"] = _room_launch(store, thread, "room-sid-2")
            _attach(svc, cs, swapped["new"])
        return got

    monkeypatch.setattr(chat, "_open_room_launch", racing)
    r = say(svc, thread)
    assert cs.message(r.message_id).launch_id == swapped["new"]
    assert count(cs, "SELECT COUNT(*) FROM chat_messages WHERE launch_id = ?", old) == 0
    assert world["session"].sends == 0


def test_a_room_with_no_running_launch_writes_nothing(world, store, cs):
    with store._write() as (conn, now):
        lease.end_launch(conn, now, world["launch"], "ended", "lapsed")
    with pytest.raises(CoordError, match="no running launch"):
        say(world["svc"], world["thread"])
    assert count(cs, "SELECT COUNT(*) FROM chat_messages") == 0


# --- C3: the room preflight ---------------------------------------------------------------------------------------
def test_a_room_steer_is_refused_queue_only(world):
    r = world["svc"].send(world["launch"], author_kind="operator", author_surface="cockpit", client_id="tab-a",
                          outbox_id="ob-00000009", body="now", mode="steer", expected_turn_id="t-1")
    assert r.state == "refused" and "queued" in r.cause and world["session"].sends == 0


def test_a_room_send_naming_an_epoch_is_refused(world):
    r = world["svc"].send(world["launch"], author_kind="operator", author_surface="cockpit", client_id="tab-a",
                          outbox_id="ob-00000009", body="now", expected_epoch=1)
    assert r.state == "refused" and "epoch" in r.cause and world["session"].sends == 0


def test_the_desk_never_writes_into_a_room(world):
    r = world["svc"].send(world["launch"], author_kind=ROLE_DESK, author_surface=ROLE_DESK,
                          client_id="coord", outbox_id="ob-00000009", body="from the desk")
    assert r.state == "refused" and "room" in r.cause and world["session"].sends == 0


def test_two_surfaces_interleave_with_no_lease(world):
    a = say(world["svc"], world["thread"], outbox="ob-0000000a", client="tab-a")
    b = say(world["svc"], world["thread"], outbox="ob-0000000b", client="iphone-1", surface="phone")
    assert (a.state, b.state) == ("accepted", "accepted")


# --- K1: the turn lease is untouched and refuses a room ----------------------------------------------------------
def test_a_lease_verb_refuses_a_room_launch(world):
    with pytest.raises(TurnRefused, match="worker launch"):
        world["svc"].turn_claim(world["launch"], "cockpit:tab-a")


# --- C2: the widened writers ----------------------------------------------------------------------------------------
def test_the_writers_take_a_room_launch(world, cs):
    launch = world["launch"]
    with cs._write() as (conn, now):
        m, created = record_message(conn, now, thread_id=world["thread"], recipient="worker", author_kind="operator",
                                    author_surface="cli", client_id="c", outbox_id="ob-0000000c", body="x",
                                    normalized="x", launch_id=launch)
        e, _ = record_event(conn, now, launch_id=launch, key="agent_text:r-1", pos="00000099", kind="agent_text",
                            body="hi", normalized="hi")
        record_cursor(conn, now, launch_id=launch, vendor_cursor="00000099", complete_through="00000099", gap=False)
    assert created and m.launch_id == launch and e.launch_id == launch


def test_the_message_trigger_takes_a_room_launch_and_still_refuses_a_stranger_thread(world, store, cs):
    other = _thread(store, title="other")
    with cs._write() as (conn, now):
        with pytest.raises(CoordError, match="of its thread"):
            record_message(conn, now, thread_id=other, recipient="worker", author_kind="operator",
                           author_surface="cli", client_id="c", outbox_id="ob-0000000d", body="x", normalized="x",
                           launch_id=world["launch"])
    c = sqlite3.connect(store.path)
    try:
        c.execute("INSERT INTO chat_messages (message_id, gseq, thread_id, launch_id, recipient, author_kind, "
                  "author_surface, client_id, outbox_id, mode, body, body_sha256, body_norm_sha256, attachments, at) "
                  "VALUES (?, 999999, ?, ?, 'worker', 'operator', 'cli', 'c', 'ob-0000000e', 'queue', 'x', 's', 's', "
                  "'[]', 'now')", ("cm-" + "1" * 32, world["thread"], world["launch"]))
        with pytest.raises(sqlite3.IntegrityError, match="names a worker or room launch of its thread"):
            c.execute("INSERT INTO chat_messages (message_id, gseq, thread_id, launch_id, recipient, author_kind, "
                      "author_surface, client_id, outbox_id, mode, body, body_sha256, body_norm_sha256, attachments, "
                      "at) VALUES (?, 999998, ?, ?, 'worker', 'operator', 'cli', 'c', 'ob-0000000f', 'queue', 'x', "
                      "'s', 's', '[]', 'now')", ("cm-" + "2" * 32, other, world["launch"]))
    finally:
        c.rollback()
        c.close()


def test_an_outside_user_item_on_a_room_opens_no_turn_epoch(world, cs):
    session, launch = world["session"], world["launch"]
    session.show("user_item", body="typed in a terminal", turn_id="t-9")
    assert world["svc"].read_next(launch) >= 1
    assert count(cs, "SELECT COUNT(*) FROM turn_lease_events WHERE launch_id = ?", launch) == 0


def test_the_room_composer_has_no_turn_to_take(world):
    assert world["svc"].composer(world["launch"], "phone:iphone-1").state == "writable"


# --- the room's mode: dontAsk is its mode, anything else latches ---------------------------------------------------
def test_a_room_reporting_dontask_stays_live(world, cs):
    world["session"].show("error", "mode:init-1", body="dontAsk")
    world["svc"].read_next(world["launch"])
    fold = cs._read(lambda conn: chat.liveness(conn, world["launch"]))
    assert fold.enabled and not fold.latched


@pytest.mark.parametrize("mode", ["default", "bypassPermissions", ""])
def test_a_room_reporting_any_other_mode_latches(world, cs, mode):
    world["session"].show("error", "mode:later-1", body=mode)
    world["svc"].read_next(world["launch"])
    fold = cs._read(lambda conn: chat.liveness(conn, world["launch"]))
    assert fold.latched and not fold.enabled


# --- C4 / item 4: session_meta, and session() is a snapshot -----------------------------------------------------
def test_session_meta_is_read_once_at_attach_and_never_by_a_reader(world):
    svc, session = world["svc"], world["session"]
    calls = session.meta_calls
    for _ in range(5):
        got = svc.session_meta(world["launch"])
    assert got == SessionMeta("room-session", "dontAsk", "claude-room-model", "2.1.317", None, ("Read",), ())
    assert session.meta_calls == calls


def test_session_is_a_snapshot_never_the_live_driver_session(world):
    svc = world["svc"]
    got = svc.session(world["launch"])
    assert got is not world["session"] and isinstance(got, SessionMeta) and not hasattr(got, "send")
    svc.detach(world["launch"])
    assert svc.session(world["launch"]) is None and svc.session_meta(world["launch"]) is None


def test_a_meta_that_raises_at_attach_caches_none(store, cs):
    class Broken(Counting):
        def meta(self):
            raise RuntimeError("no init yet")

    thread = _thread(store)
    _open_room_thread(store, thread)
    launch = _room_launch(store, thread, "room-sid-b")
    svc = ChatService(cs, **LIVE)
    svc.attach(launch, Broken())
    assert svc.session_meta(launch) is None


# --- item 1: no send while an interrupt waits on the session ----------------------------------------------------
class Holding(Counting):
    def __init__(self) -> None:
        super().__init__()
        self.waiting, self.go = threading.Event(), threading.Event()

    def interrupt(self, command):
        self.waiting.set()
        self.go.wait(5)
        return CommandResult("confirmed", "interrupted")


def test_a_send_while_an_interrupt_waits_is_refused_and_never_reaches_the_session(store, cs):
    thread = _thread(store)
    _open_room_thread(store, thread)
    launch = _room_launch(store, thread, "room-sid-h")
    svc = ChatService(cs, **LIVE)
    session = _attach(svc, cs, launch, Holding())
    stopper = threading.Thread(target=svc.interrupt, args=(launch, None))
    stopper.start()
    try:
        assert session.waiting.wait(5)
        r = say(svc, thread)
        assert r.state in ("refused", "cancelled") and "interrupt" in r.cause and session.sends == 0
    finally:
        session.go.set()
        stopper.join(5)
    assert say(svc, thread, outbox="ob-00000002").state == "accepted"       # once it answered, sends go again


def _worker(store, cs, svc, session, sid="sess-w1"):
    token = store.coord_take()
    thread = _thread(store, kind="work", title="fix it")
    ln = store.launch_request(token, harness="claude", role="worker", threads=[thread])
    store.launch_activate(ln.launch_id, harness_session_id=sid)
    _attach(svc, cs, ln.launch_id, session)
    return ln.launch_id


def test_a_worker_send_while_an_interrupt_waits_is_refused_and_never_reaches_the_session(store, cs):
    svc = ChatService(cs, **LIVE)
    session = Holding()
    launch = _worker(store, cs, svc, session)
    epoch = svc.turn_claim(launch, "cockpit:tab-a")
    stopper = threading.Thread(target=svc.interrupt, args=(launch, None))
    stopper.start()
    try:
        assert session.waiting.wait(5)
        r = svc.send(launch, author_kind="operator", author_surface="cockpit", client_id="tab-a",
                     outbox_id="ob-00000001", body="go", expected_epoch=epoch)
        assert r.state in ("refused", "cancelled") and "interrupt" in r.cause and session.sends == 0
    finally:
        session.go.set()
        stopper.join(5)


# --- item 3: message_thread answers only the client that sent -------------------------------------------------
def test_message_thread_is_scoped_by_client(world):
    r = say(world["svc"], world["thread"], client="tab-a")
    got = world["svc"].message_thread("cockpit:tab-a", "ob-00000001")
    assert got is not None and got.message_id == r.message_id
    assert world["svc"].message_thread("cockpit:tab-b", "ob-00000001") is None
    assert world["svc"].message_thread("phone:tab-a", "ob-00000001") is None    # FIX 4: the surface is the owner's


# --- item 5: a turn release never lands inside a send in flight ----------------------------------------------
def test_a_turn_release_waits_for_a_send_in_flight_on_its_launch(store, cs):
    token = store.coord_take()
    thread = _thread(store, kind="work", title="fix it")
    ln = store.launch_request(token, harness="claude", role="worker", threads=[thread])
    store.launch_activate(ln.launch_id, harness_session_id="sess-w1")
    launch, me = ln.launch_id, "cockpit:tab-a"
    released = threading.Event()
    seen = {}

    def fault(point):
        if point == "attempt_committed":
            t = threading.Thread(target=lambda: (svc.turn_release(launch, epoch, me), released.set()))
            t.start()
            seen["thread"] = t
            seen["released_during_send"] = released.wait(0.5)

    svc = ChatService(cs, fault=fault, **LIVE)
    _attach(svc, cs, launch, ScriptedSession())
    epoch = svc.turn_claim(launch, me)
    r = svc.send(launch, author_kind="operator", author_surface="cockpit", client_id="tab-a",
                 outbox_id="ob-00000001", body="go", expected_epoch=epoch)
    seen["thread"].join(5)
    assert r.state == "accepted"
    assert seen["released_during_send"] is False and released.is_set()


# --- RM-D2B: L3_rmd2_r1 fixes ------------------------------------------------------------------------------------
def test_fix1_a_rotated_rooms_old_launch_refuses_send_and_writes_nothing(world, store, cs):
    """codex HIGH 1: the old launch of a rotated room is still active; the public send() is fenced like send_room."""
    nxt = _thread(store, title="next room")
    with store._write() as (conn, _now):
        room.rotate(conn, nxt)
    before = count(cs, "SELECT COUNT(*) FROM chat_messages")
    with pytest.raises(chat.RoomRotated):
        world["svc"].send(world["launch"], author_kind="operator", author_surface="cockpit", client_id="tab-a",
                          outbox_id="ob-00000011", body="to the stale room", mode="queue", expected_epoch=None)
    assert count(cs, "SELECT COUNT(*) FROM chat_messages") == before and world["session"].sends == 0


class SendWatched(Holding):
    """A session whose interrupt blocks until released, and which records whether the interrupt had already reached
    it when a send arrived."""

    def __init__(self) -> None:
        super().__init__()
        self.send_during_interrupt = False

    def send(self, command):
        if self.waiting.is_set() and not self.go.is_set():
            self.send_during_interrupt = True
        return super().send(command)


def _held_room(store, cs, sid, fault=None):
    thread = _thread(store)
    _open_room_thread(store, thread)
    launch = _room_launch(store, thread, sid)
    svc = ChatService(cs, fault=fault, **LIVE)
    return thread, launch, svc


def test_fix2_an_interrupt_pinned_after_the_attempt_commits_waits_for_the_send(store, cs):
    """codex HIGH 2: the send committed ``attempted``, then an interrupt pinned the session before session.send: the
    interrupt never reaches the session while the send is still to enter it (it waits, bounded, for that call)."""
    session = SendWatched()
    seen = {}

    def fault(point):
        if point == "attempt_committed":
            t = threading.Thread(target=lambda: seen.__setitem__("result", svc.interrupt(launch, None)))
            t.start()
            seen["thread"] = t
            seen["reached_during_send"] = session.waiting.wait(0.5)

    thread, launch, svc = _held_room(store, cs, "room-sid-a2", fault)
    _attach(svc, cs, launch, session)
    try:
        r = say(svc, thread)
        assert seen["reached_during_send"] is False and session.send_during_interrupt is False
        assert r.state == "accepted" and session.sends == 1
        assert session.waiting.wait(5)                  # once the send's call returned, the stop went in
    finally:
        session.go.set()
        seen["thread"].join(5)
    assert seen["result"].outcome == "confirmed"


def test_fix2_an_interrupt_pinned_before_the_arbiter_cancels_the_send(store, cs):
    """The arbiter's other branch: an interrupt pinned between the pending commit and the attempt cancels the send
    before any attempt; no byte reaches the session."""
    session = SendWatched()
    seen = {}

    def fault(point):
        if point == "pending_committed":
            t = threading.Thread(target=svc.interrupt, args=(launch, None))
            t.start()
            seen["thread"] = t
            assert session.waiting.wait(5)

    thread, launch, svc = _held_room(store, cs, "room-sid-b2", fault)
    _attach(svc, cs, launch, session)
    try:
        r = say(svc, thread)
        assert r.state == "cancelled" and "interrupt" in r.cause and session.sends == 0
        assert count(cs, "SELECT COUNT(*) FROM chat_outbox_events WHERE message_id = ? AND state = 'attempted'",
                     r.message_id) == 0
    finally:
        session.go.set()
        seen["thread"].join(5)


def test_fix3_a_worker_line_on_the_thread_is_no_room_replay(store, cs):
    """codex MED 4: a message stored against a worker launch holding T, resent after T became the room, is a
    conflict: it neither answers with the worker's receipt nor reaches the room."""
    svc = ChatService(cs, **LIVE)
    class WorkerCounting(ScriptedSession):              # a worker's own mode (Counting reports the room's)
        sends = 0

        def send(self, command):
            self.sends += 1
            return super().send(command)

    worker_session = WorkerCounting()
    token = store.coord_take()
    thread = _thread(store, kind="work", title="fix it")
    ln = store.launch_request(token, harness="claude", role="worker", threads=[thread])
    store.launch_activate(ln.launch_id, harness_session_id="sess-w3")
    _attach(svc, cs, ln.launch_id, worker_session)
    epoch = svc.turn_claim(ln.launch_id, "cockpit:tab-a")
    first = svc.send(ln.launch_id, author_kind="operator", author_surface="cockpit", client_id="tab-a",
                     outbox_id="ob-00000031", body="hello room", expected_epoch=epoch)
    assert first.state == "accepted"
    _open_room_thread(store, thread)
    room_launch = _room_launch(store, thread, "room-sid-3")
    room_session = _attach(svc, cs, room_launch)
    with pytest.raises(chat.MessageConflict):
        say(svc, thread, outbox="ob-00000031", body="hello room")
    assert room_session.sends == 0 and worker_session.sends == 1


def test_fix3_another_clients_outbox_id_is_a_conflict_naming_nothing_of_its_message(world):
    """deepseek MED 3: the stored-replay path is scoped to the caller; the refusal names no message id."""
    r = say(world["svc"], world["thread"], client="tab-a")
    with pytest.raises(chat.MessageConflict) as got:
        say(world["svc"], world["thread"], client="tab-b")
    assert r.message_id not in str(got.value) and "another client" in str(got.value)
    with pytest.raises(chat.MessageConflict) as again:            # the same owner check on the worker path's writer
        world["svc"].send(world["launch"], author_kind="operator", author_surface="phone", client_id="tab-a",
                          outbox_id="ob-00000001", body="hello room")
    assert r.message_id not in str(again.value) and world["session"].sends == 1


def test_fix5_an_overdue_meta_at_attach_wedges_the_session(store, cs, monkeypatch):
    """codex MED 3 / complement LOW 2: a meta() still running past READ_DEADLINE leaves the session wedged, so no
    other driver method is called on it while that call is in flight."""
    release = threading.Event()

    class Hung(Counting):
        def meta(self):
            release.wait(5)
            return super().meta()

        def capabilities(self):
            self.caps_calls = getattr(self, "caps_calls", 0) + 1
            return super().capabilities()

    monkeypatch.setattr(chat, "READ_DEADLINE", 0.05)
    thread = _thread(store)
    _open_room_thread(store, thread)
    launch = _room_launch(store, thread, "room-sid-5")
    svc = ChatService(cs, **LIVE)
    _live(cs, launch)
    session = Hung()
    try:
        svc.attach(launch, session)
        assert svc.session_meta(launch) is None
        r = say(svc, thread)
        assert r.state == "refused" and "stopped answering" in r.cause
        assert session.sends == 0 and getattr(session, "caps_calls", 0) == 0
    finally:
        release.set()


_OLD_WORKER_TRIGGER = (
    "CREATE TRIGGER chat_messages_worker BEFORE INSERT ON chat_messages WHEN NEW.launch_id IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM launches l, json_each(l.threads) j WHERE l.launch_id = NEW.launch_id "
    "AND l.role = 'worker' AND j.value = NEW.thread_id) "
    "BEGIN SELECT RAISE(ABORT, 'a chat message to a worker names a worker launch of its thread'); END")


def test_an_old_store_with_the_worker_only_trigger_is_migrated_on_open(store, cs):
    """deepseek HIGH 1, REFUTED: a store written by the build before RM-D2 holds the worker-only trigger text;
    _ensure drops a trigger whose stored text is not CHAT_SCHEMA's and recreates it, so a room line goes through."""
    thread = _thread(store)
    _open_room_thread(store, thread)
    launch = _room_launch(store, thread, "room-sid-old")
    raw = sqlite3.connect(store.path)
    try:
        raw.execute("DROP TRIGGER chat_messages_worker")
        raw.execute(_OLD_WORKER_TRIGGER)
        raw.commit()
    finally:
        raw.close()
    # the control: this ChatStore ensured its schema before the old text went back in, so the old trigger bites
    stale = ChatService(cs, **LIVE)
    _attach(stale, cs, launch)
    with pytest.raises(sqlite3.IntegrityError, match="names a worker launch of its thread"):
        say(stale, thread, outbox="ob-000000a1")
    stale.detach(launch)
    # a process opening the old store: its first connection brings the trigger current
    fresh_cs = ChatStore(store)
    svc = ChatService(fresh_cs, **LIVE)
    session = _attach(svc, fresh_cs, launch)
    r = say(svc, thread, outbox="ob-000000a2")
    assert r.state == "accepted" and session.sends == 1
    sql = fresh_cs._read(lambda conn: conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'chat_messages_worker'").fetchone()[0])
    assert "'room'" in sql


# --- RM-D2C: L3_rmd2_r2 fixes ------------------------------------------------------------------------------------
class SendHung(Counting):
    """A session whose send enters and then blocks until released (a slow or hung driver)."""

    def __init__(self) -> None:
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()
        self.interrupts = 0

    def send(self, command):
        self.entered.set()
        self.release.wait(5)
        return super().send(command)

    def interrupt(self, command):
        self.interrupts += 1
        return CommandResult("confirmed", "interrupted")


def test_fix1_a_stop_behind_a_hung_send_closes_the_session(store, cs, monkeypatch):
    """L3_rmd2_r2 complement MED: the send's driver call has no deadline of its own; an interrupt that waited
    INTERRUPT_DEADLINE for it does not give up with nothing done: it wedges the session and closes it (the stop that
    always works), and its receipt says so. FIX 2 (complement LOW): that branch leaves the session wedged."""
    monkeypatch.setattr(chat, "INTERRUPT_DEADLINE", 0.2)
    thread, launch, svc = _held_room(store, cs, "room-sid-c1")
    session = _attach(svc, cs, launch, SendHung())
    sender = threading.Thread(target=lambda: say(svc, thread))
    sender.start()
    try:
        assert session.entered.wait(5)
        got = svc.interrupt(launch, None)
        assert got.outcome == "ambiguous" and "closed" in got.detail and "not returned" in got.detail
        assert session.closed is True and session.interrupts == 0
        with pytest.raises(CoordError, match="stopped answering"):      # FIX 2: wedged, the pin gone
            svc.interrupt(launch, None)
        assert svc.composer(launch, "cockpit:tab-a").state != "writable"
    finally:
        session.release.set()
        sender.join(5)
    assert not sender.is_alive()


class CloseSlow(SendHung):
    """A hung send whose close takes a while and records how many closes run at once."""

    def __init__(self) -> None:
        super().__init__()
        self.live_closes = self.peak_closes = 0
        self.close_started = threading.Event()
        self._cl = threading.Lock()

    def close(self):
        with self._cl:
            self.live_closes += 1
            self.peak_closes = max(self.peak_closes, self.live_closes)
        self.close_started.set()
        time.sleep(0.25)
        with self._cl:
            self.live_closes -= 1
        super().close()


def test_a_stop_by_close_keeps_its_pin_until_the_close_returns_so_detach_cannot_close_twice(store, cs, monkeypatch):
    """L3_rmd2_r3 codex MED: the stop-by-close dropped its pin before the bounded close finished, so a hung send that
    returned late let detach() close the same session while the stop's close was still running. The pin now stays
    until the close returns, and detach waits on it."""
    monkeypatch.setattr(chat, "INTERRUPT_DEADLINE", 0.6)
    thread, launch, svc = _held_room(store, cs, "room-sid-c2")
    session = _attach(svc, cs, launch, CloseSlow())
    sender = threading.Thread(target=lambda: say(svc, thread))
    sender.start()
    stopper = None
    try:
        assert session.entered.wait(5)
        stopper = threading.Thread(target=lambda: svc.interrupt(launch, None))
        stopper.start()
        assert session.close_started.wait(5)             # the stop's close is running (the send is still hung)
        session.release.set()                            # the hung send now returns late
        svc.detach(launch)                               # must wait for the pin, not close beside the stop's close
    finally:
        session.release.set()
        sender.join(5)
        if stopper is not None:
            stopper.join(5)
    assert session.peak_closes == 1, f"{session.peak_closes} closes of one session ran at once"


def test_fix3_a_turn_release_of_a_room_launch_is_refused_at_the_service(world, monkeypatch):
    """codex 6: turn_release gates with the worker-only check, so a room launch never reaches the lease layer."""
    reached = []
    monkeypatch.setattr(world["svc"]._chat, "turn_release", lambda *a: reached.append(a))
    with pytest.raises(CoordError, match="only a worker launch"):
        world["svc"].turn_release(world["launch"], 1, "cockpit:tab-a")
    assert reached == []


def test_fix4_a_stored_room_resend_is_answered_with_the_launch_role_unresolved(world, cs, monkeypatch):
    """codex 7: a stored receipt comes from the record alone; the stored launch's role is not needed for it."""
    first = say(world["svc"], world["thread"])
    real = chat._launch_state
    monkeypatch.setattr(chat, "_launch_state",
                        lambda conn, lid: (None, None, None) if lid == world["launch"] else real(conn, lid))
    again = say(world["svc"], world["thread"])
    assert (again.message_id, again.state, again.created) == (first.message_id, "accepted", False)
    assert world["session"].sends == 1
    with pytest.raises(chat.MessageConflict):           # the record still answers only the same line
        say(world["svc"], world["thread"], body="something else")
