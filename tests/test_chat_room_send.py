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

from levain.coordinator import chat, lease, room
from levain.coordinator.chat import ChatService, ChatStore, record_check, record_cursor, record_event, record_message
from levain.coordinator.chat_lease import TurnRefused
from levain.coordinator.driver import CommandResult, ScriptedSession, SessionMeta
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import CoordError, Part

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
    with store._write() as (conn, _now):
        room.rotate(conn, _thread(store, title="next"))
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


def test_the_coordinator_never_writes_into_a_room(world):
    r = world["svc"].send(world["launch"], author_kind="coordinator", author_surface="coordinator",
                          client_id="coord", outbox_id="ob-00000009", body="from the coordinator")
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
    got = world["svc"].message_thread("tab-a", "ob-00000001")
    assert got is not None and got.message_id == r.message_id
    assert world["svc"].message_thread("tab-b", "ob-00000001") is None


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
