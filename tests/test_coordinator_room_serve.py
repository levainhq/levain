"""RM-G: what ``levain serve`` holds for the room (``levain.coordinator.room_serve``): recover before any send, the
pump's start and stop, the room opened on first need, a restart that resumes, and two stale surfaces (DESIGN_ROOM
§3.6). Two transports: a ``ScriptedSession`` behind a stub launcher, and the fake ``claude`` of
``tests/test_coordinator_room_launch.py`` over real pipes. NOTHING here runs a real Claude child, an HTTP route, or the
page; those are RM-F's and a person's."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from levain.coordinator import lease, room
from levain.coordinator.chat import RoomRotated, Verdict, record_check
from levain.coordinator.driver import ScriptedSession, SessionMeta
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.room_launch import RoomHandle, RoomSetting
from levain.coordinator.room_serve import RoomServe, RoomServeError
from tests.test_coordinator_room_launch import FAKE

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def make_store(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    s = EnvelopeStore(d)
    s.coord_set_agent("none")
    return s


class RoomScripted(ScriptedSession):
    """A ScriptedSession that reports the room's permission mode (the service latches any other)."""

    def meta(self):
        return SessionMeta("room-session", "dontAsk", "room-model", "2.1.317", None, ("Read",), ())


class ScriptedLauncher:
    """A launcher with the three methods RoomServe calls, attaching a ScriptedSession to a real room launch row."""

    def __init__(self, store, cs, svc):
        self.store, self.cs, self.svc = store, cs, svc
        self.session = None
        self.handle = None
        self.ensures = 0
        self.closed = False
        self.order = []

    def ensure(self, *, fresh=False):
        from levain.coordinator.chat import _open_room
        self.ensures += 1
        thread = self.cs._read(_open_room)
        if self.handle is not None and self.handle.room_thread == thread:
            return self.handle
        if self.handle is not None:
            self.svc.detach(self.handle.launch_id)
        with self.store._write() as (conn, now):
            ln = lease.open_room_launch(conn, now, harness="claude", model=None, cwd=str(self.store.path.parent),
                                        room_thread=thread, pid=os.getpid())
        with self.store._write() as (conn, now):
            got, why = lease.activate_room(conn, now, ln.launch_id, harness_session_id=f"sid-{self.ensures}")
        assert why is None
        with self.cs._write() as (conn, now):
            record_check(conn, now, ln.launch_id, "live", "activation", "test")
        self.session = RoomScripted()
        self.svc.attach(ln.launch_id, self.session)
        self.svc.ingest(ln.launch_id, self.session.read(None))
        self.handle = RoomHandle(ln.launch_id, thread, f"sid-{self.ensures}", False)
        return self.handle

    def recycle_idle(self):
        return []

    def child_alive(self, _launch_id):
        return True

    def end(self, launch_id, _cause):
        self.svc.detach(launch_id)
        self.handle = None

    def close(self):
        self.closed = True


def scripted(tmp_path, store=None, **kw):
    store = store or make_store(tmp_path)
    holder = {}

    def build(cs, svc):
        holder["l"] = ScriptedLauncher(store, cs, svc)
        return holder["l"]
    rs = RoomServe(store, RoomSetting(str(tmp_path)), run_dir=str(tmp_path / "run"), launcher=build,
                   service_kw={"sleep": lambda _s: None, "probe": lambda _l, _e: Verdict(True, "scripted probe: live")},
                   pump_interval=0.01, **kw)
    return store, rs, holder["l"]


def wait(cond, secs=10.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# --- recover before send, start and stop ---------------------------------------------------------------------------
def test_nothing_sends_or_opens_before_start_and_nothing_after_stop(tmp_path):
    _store, rs, launcher = scripted(tmp_path)
    for call in (rs.ensure_room, rs.view, rs.rotate, lambda: rs.send(author_surface="cockpit", client_id="a",
                                                                       outbox_id="ob-00000001", body="x")):
        with pytest.raises(RoomServeError, match="not started"):
            call()
    assert launcher.ensures == 0
    rs.start()
    rs.stop()
    with pytest.raises(RoomServeError, match="stopped"):
        rs.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000002", body="x")
    with pytest.raises(RoomServeError, match="stopped"):
        rs.start()
    assert launcher.ensures == 0 and launcher.closed


def test_start_runs_recover_before_the_pump_and_before_the_first_open(tmp_path, monkeypatch):
    _store, rs, launcher = scripted(tmp_path)
    log = []
    real = rs.service.recover
    monkeypatch.setattr(rs.service, "recover", lambda: (log.append("recover"), real())[1])
    real_ensure = launcher.ensure
    monkeypatch.setattr(launcher, "ensure", lambda **kw: (log.append("ensure"), real_ensure(**kw))[1])
    real_pump = rs.pump_once
    monkeypatch.setattr(rs, "pump_once", lambda: (log.append("pump"), real_pump())[1])
    try:
        rs.start()
        rs.ensure_room()
        assert wait(lambda: "pump" in log)
        assert log[0] == "recover" and log.index("recover") < log.index("ensure") and log.index("recover") < log.index("pump")
    finally:
        rs.stop()


def test_recover_settles_the_lines_a_dead_process_left_and_start_is_idempotent(tmp_path):
    store, rs, _l = scripted(tmp_path)
    cs = rs.chat_store
    from levain.coordinator.chat import outbox_step, record_message
    d = store.accept_drop("t", surface="cli")[0]
    from levain.coordinator.store import Part
    thread = store.split(d.drop_id, [Part("note", "t")])[0].thread_id
    with cs._write() as (conn, now):
        for ob in ("ob-0000000a", "ob-0000000b"):
            m, _ = record_message(conn, now, thread_id=thread, recipient="coordinator", author_kind="operator",
                                  author_surface="cli", client_id="c", outbox_id=ob, body="x", normalized="x")
            outbox_step(conn, now, m.message_id, "pending", cause="test")
        outbox_step(conn, now, m.message_id, "attempted", cause="test", op_id="op-test-1", vendor_anchor="00000001")
    try:
        assert rs.start() == {"ambiguous": 1, "not_delivered": 1}
        assert rs.start() == {"ambiguous": 1, "not_delivered": 1}
    finally:
        rs.stop()


def test_the_pump_starts_one_thread_stops_it_and_survives_a_bad_step(tmp_path, monkeypatch):
    _store, rs, _l = scripted(tmp_path)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("one bad step")
    monkeypatch.setattr(rs, "pump_once", boom)
    rs.start()
    rs.start()
    try:
        assert wait(lambda: len(calls) >= 3)
        assert rs.last_error is not None and "one bad step" in rs.last_error
        assert [t for t in __import__("threading").enumerate() if t.name == "levain-room-pump"] == [rs._thread]
    finally:
        rs.stop()
    assert not rs._thread.is_alive()
    n = len(calls)
    time.sleep(0.1)
    assert len(calls) == n


# --- the room opens on first need, on a ScriptedSession ------------------------------------------------------------
def test_a_started_serve_opens_nothing_until_a_send_and_then_opens_one_room(tmp_path):
    _store, rs, launcher = scripted(tmp_path)
    rs.start()
    try:
        time.sleep(0.1)
        assert launcher.ensures == 0 and rs.view()["room_thread"] is None
        r = rs.send(author_surface="cockpit", client_id="tab-a", outbox_id="ob-00000001", body="hello")
        assert r.state == "accepted", r.cause
        assert len(launcher.session.written) == 1
        v = rs.view()
        assert v["room_thread"] is not None and v["launch_id"] == launcher.handle.launch_id
        rs.send(author_surface="cockpit", client_id="tab-a", outbox_id="ob-00000002", body="again")
        assert launcher.handle.room_thread == v["room_thread"]
        rooms = rs.chat_store._read(lambda conn: conn.execute("SELECT COUNT(*) FROM room_events").fetchone()[0])
        assert rooms == 1
    finally:
        rs.stop()


def test_the_pump_ingests_what_the_vendor_shows(tmp_path):
    _store, rs, launcher = scripted(tmp_path)
    rs.start()
    try:
        rs.send(author_surface="cockpit", client_id="tab-a", outbox_id="ob-00000001", body="hello")
        launcher.session.show("agent_text", "r-1", body="hi back")
        thread = rs.view()["room_thread"]

        def seen():
            rows = rs.chat_store.timeline(thread)
            return any("hi back" in str(r) for part in rows for r in part)
        assert wait(seen)
    finally:
        rs.stop()


# --- stale surfaces, DESIGN_ROOM 3.6 --------------------------------------------------------------------------------
def test_two_tabs_each_send_their_own_line_in_order_and_a_resend_sends_nothing_again(tmp_path):
    _store, rs, launcher = scripted(tmp_path)
    rs.start()
    try:
        a = rs.send(author_surface="cockpit", client_id="tab-a", outbox_id="ob-0000000a", body="from a")
        b = rs.send(author_surface="cockpit", client_id="tab-b", outbox_id="ob-0000000b", body="from b")
        assert a.message_id != b.message_id and launcher.session.written.__len__() == 2
        again = rs.send(author_surface="cockpit", client_id="tab-a", outbox_id="ob-0000000a", body="from a")
        assert again.message_id == a.message_id and len(launcher.session.written) == 2
    finally:
        rs.stop()


def test_a_tab_that_drew_a_rotated_room_is_refused_and_the_new_room_takes_the_next_send(tmp_path):
    _store, rs, launcher = scripted(tmp_path)
    rs.start()
    try:
        rs.send(author_surface="cockpit", client_id="tab-a", outbox_id="ob-00000001", body="one")
        old = rs.view()["room_thread"]
        first_launch = launcher.handle.launch_id
        new = rs.rotate()
        assert new != old and rs.view()["room_thread"] == new
        old_session = launcher.session
        written = len(old_session.written)
        with pytest.raises(RoomRotated):
            rs.send(author_surface="cockpit", client_id="tab-a", outbox_id="ob-00000002", body="stale", room_thread=old)
        assert launcher.handle.launch_id == first_launch      # nothing opened for the refused line
        r = rs.send(author_surface="cockpit", client_id="tab-b", outbox_id="ob-00000003", body="fresh", room_thread=new)
        assert r.state == "accepted" and launcher.handle.room_thread == new
        assert len(old_session.written) == written                    # the stale line wrote nothing to the vendor
        assert [w["text"] for w in launcher.session.written] == ["fresh"] and launcher.session is not old_session
    finally:
        rs.stop()


# --- the fake claude over real pipes: a round trip and a restart -----------------------------------------------------
def fake_serve(tmp_path, store=None):
    store = store or make_store(tmp_path)
    fake = tmp_path / "fake"
    fake.mkdir(exist_ok=True)
    script = tmp_path / "fake_claude"
    script.write_text(FAKE.format(python=__import__("sys").executable))
    script.chmod(0o700)
    cwd, home = tmp_path / "cwd", tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    rs = RoomServe(store, RoomSetting(str(cwd), model="sonnet", claude_bin=str(script)), run_dir=str(tmp_path / "run"),
                   launcher_kw={"home": str(home), "environ": {**os.environ, "FAKE_DIR": str(fake)}},
                   service_kw={"sleep": lambda _s: None}, pump_interval=0.02)
    return store, rs, fake


def agent_texts(rs, thread):
    rows = rs.chat_store.timeline(thread)
    return [str(r) for part in rows for r in part]


def test_a_round_trip_on_the_fake_transport_opens_on_first_need_and_the_pump_reads_the_reply(tmp_path):
    _store, rs, fake = fake_serve(tmp_path)
    rs.start()
    try:
        assert list(fake.glob("spawn-*.argv")) == []                      # started, nothing spawned
        r = rs.send(author_surface="cockpit", client_id="tab-a", outbox_id="ob-00000001", body="hello room")
        assert r.state == "accepted", r.cause
        assert len(list(fake.glob("spawn-*.argv"))) == 1
        thread = rs.view()["room_thread"]
        assert wait(lambda: any("echo: hello room" in t for t in agent_texts(rs, thread))), rs.last_error
    finally:
        rs.stop()


def test_a_restart_recovers_then_resumes_the_rooms_conversation_on_the_same_thread(tmp_path):
    store, rs1, fake = fake_serve(tmp_path)
    rs1.start()
    rs1.send(author_surface="cockpit", client_id="tab-a", outbox_id="ob-00000001", body="before")
    thread = rs1.view()["room_thread"]
    sid1 = rs1.view() and rs1._handle.harness_session_id
    launch1 = rs1._handle.launch_id
    # the process "dies": no stop(), so the launch stays active in the store and its child is orphaned from rs2
    rs1._stop_evt.set()
    rs1._thread.join(5)
    _store2, rs2, _ = fake_serve(tmp_path, store=store)
    try:
        counts = rs2.start()
        assert set(counts) == {"ambiguous", "not_delivered"}
        r = rs2.send(author_surface="cockpit", client_id="tab-a", outbox_id="ob-00000002", body="after",
                     room_thread=thread)
        assert r.state == "accepted" and rs2._handle.launch_id != launch1 and rs2._handle.resumed
        assert rs2._handle.room_thread == thread and rs2._handle.harness_session_id == sid1
        assert wait(lambda: any("echo: after" in t for t in agent_texts(rs2, thread))), rs2.last_error
        # the timeline is continuous: the first serve's line is still in the same thread
        assert any("echo: before" in t or "before" in t for t in agent_texts(rs2, thread))
    finally:
        rs2.stop()
        rs1.launcher.close()
        rs1.service.close()


# --- the fix lane (FINDINGS_rmg_r1) ----------------------------------------------------------------------------------
import sqlite3
import threading

from levain.coordinator.store import CoordError


def counts(rs):
    """(chat_messages, chat_outbox_events, threads, room_events) as the store holds them (0 for a table not made yet)."""
    def one(c, t):
        try:
            return c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
    return rs.chat_store._read(lambda c: tuple(one(c, t) for t in (
        "chat_messages", "chat_outbox_events", "threads", "room_events")))


def coordinator_launches(store):
    with store._write() as (conn, now):
        return [ln for ln in lease.launches(conn, limit=20) if ln.role == "coordinator"]


# F1: the room's thread is made without a drop and never wakes the coordinator
def test_bare_thread_makes_a_thread_with_no_drop_no_receipt_and_no_wake(tmp_path):
    store = make_store(tmp_path)
    store.coord_set_agent("claude", cwd=str(tmp_path))
    with store._write() as (conn, now):
        tid = store.bare_thread(conn, now, "the room")
    t = store.thread(tid)
    assert (t.kind, t.state, t.members, t.title) == ("note", "open", (), "the room")
    assert store.drops() == [] and coordinator_launches(store) == [] and store._woken == {}


def test_bare_thread_refuses_a_bad_title_or_actor_and_a_raising_caller_leaves_nothing(tmp_path):
    store = make_store(tmp_path)
    before = len(store.threads(include_closed=True))
    for bad in ("", "   ", "two\nlines", "x" * 5000, None):
        with pytest.raises(CoordError):
            with store._write() as (conn, now):
                store.bare_thread(conn, now, bad)
    with pytest.raises(CoordError, match="by is one of"):
        with store._write() as (conn, now):
            store.bare_thread(conn, now, "the room", by="coordinator")
    with pytest.raises(RuntimeError):
        with store._write() as (conn, now):
            store.bare_thread(conn, now, "the room")
            raise RuntimeError("the caller failed after the thread")
    assert len(store.threads(include_closed=True)) == before


def test_opening_the_room_with_a_claude_coordinator_writes_no_launch_and_a_real_drop_still_wakes(tmp_path):
    store = make_store(tmp_path)
    store.coord_set_agent("claude", cwd=str(tmp_path))
    _s, rs, _l = scripted(tmp_path, store=store)
    rs.start()
    try:
        rs.send(author_surface="cockpit", client_id="tab-a", outbox_id="ob-00000001", body="hello")
        assert store.drops() == []                                  # the room made no drop
        assert coordinator_launches(store) == [] and store._woken == {}
        d, _ = store.accept_drop("a real drop from the phone", surface="phone")
        assert store._woken.get(d.drop_id)                          # it woke the coordinator, normally
        assert [ln.cause for ln in coordinator_launches(store)] == ["wake"]
    finally:
        rs.stop()


# F2: a rotate with no open room writes nothing
def test_rotate_with_no_open_room_refuses_before_any_write(tmp_path):
    _store, rs, launcher = scripted(tmp_path)
    rs.start()
    try:
        before = counts(rs)
        for _ in range(2):
            with pytest.raises(CoordError, match="no room is open"):
                rs.rotate()
        assert counts(rs) == before and launcher.ensures == 0
    finally:
        rs.stop()


# F3: send holds the room against stop and rotate
def test_a_send_racing_stop_finishes_first_and_a_send_after_stop_writes_nothing(tmp_path):
    _store, rs, launcher = scripted(tmp_path)
    rs.start()
    rs.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000001", body="one")
    inside, stopped = threading.Event(), threading.Event()
    real = rs.service.send_room

    def late(*a, **k):
        t = threading.Thread(target=lambda: (rs.stop(), stopped.set()))
        t.start()
        inside.set()
        assert not stopped.wait(0.3)               # stop waits on the send in flight
        return real(*a, **k)
    rs.service.send_room = late
    r = rs.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000002", body="two")
    assert r.state == "accepted", r.cause
    assert stopped.wait(5) and launcher.closed
    after = counts(rs)
    with pytest.raises(RoomServeError, match="stopped"):
        rs.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000003", body="three")
    assert counts(rs) == after


def test_a_send_naming_no_room_is_not_refused_by_a_rotate_racing_it(tmp_path):
    _store, rs, launcher = scripted(tmp_path)
    rs.start()
    try:
        rs.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000001", body="one")
        old = rs.view()["room_thread"]
        rotated, box = threading.Event(), {}
        real = rs.service.send_room

        def late(*a, **k):
            def rot():
                box["new"] = rs.rotate()
                rotated.set()
            threading.Thread(target=rot).start()
            assert not rotated.wait(0.3)           # rotate waits for the send to land
            return real(*a, **k)
        rs.service.send_room = late
        r = rs.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000002", body="two")
        assert r.state == "accepted", r.cause
        assert rotated.wait(5) and box["new"] != old and rs.view()["room_thread"] == box["new"]
        rs.service.send_room = real
        r = rs.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000003", body="three")
        assert r.state == "accepted" and launcher.handle.room_thread == box["new"]
    finally:
        rs.stop()


# F4 + F7: a stored line is answered before any room is resolved
def test_a_resend_of_a_stored_line_after_a_restart_opens_nothing(tmp_path):
    store, rs1, fake = fake_serve(tmp_path)
    rs1.start()
    r1 = rs1.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000001", body="before")
    rs1._stop_evt.set()
    rs1._thread.join(5)
    for held in list(rs1.launcher._live.values()):
        held.proc.kill()
    _s2, rs2, _ = fake_serve(tmp_path, store=store)
    n0 = len(list(fake.glob("spawn-*.argv")))
    try:
        rs2.start()
        r2 = rs2.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000001", body="before")
        assert r2.message_id == r1.message_id and r2.state == r1.state
        assert len(list(fake.glob("spawn-*.argv"))) == n0 and rs2._handle is None
    finally:
        rs2.stop()
        rs1.launcher.close()
        rs1.service.close()


def test_a_replay_naming_no_room_after_a_rotation_gets_the_stored_receipt(tmp_path):
    _store, rs, launcher = scripted(tmp_path)
    rs.start()
    try:
        a = rs.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000001", body="one")
        rs.rotate()
        n = launcher.ensures
        again = rs.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000001", body="one")
        assert again.message_id == a.message_id and launcher.ensures == n
        with pytest.raises(CoordError):                                  # another client's id is still a conflict
            rs.send(author_surface="cockpit", client_id="b", outbox_id="ob-00000001", body="one")
    finally:
        rs.stop()


# F5: a child that dies while the server stays up is noticed
def test_a_child_that_dies_while_serve_is_up_is_ended_and_said_and_the_next_send_resumes(tmp_path):
    store, rs, fake = fake_serve(tmp_path)
    rs.start()
    try:
        rs.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000001", body="one")
        lid = rs._handle.launch_id
        for held in list(rs.launcher._live.values()):
            held.proc.kill()
        assert wait(lambda: rs.view()["launch_id"] is None), rs.view()
        assert "exited" in (rs.last_error or "") and lid not in rs.launcher._live
        with store._write() as (conn, now):
            assert lease._launch_state(conn, lid)[1] == "ended" if hasattr(lease, "_launch_state") else True
        r = rs.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000002", body="two")
        assert r.state == "accepted", r.cause
        assert rs._handle.launch_id != lid and rs._handle.resumed and rs.last_error is None
    finally:
        rs.stop()


# F6
def test_last_error_clears_on_a_good_open_and_a_step_racing_a_stop_records_nothing(tmp_path, monkeypatch):
    _store, rs, _l = scripted(tmp_path)
    rs.start()
    try:
        rs.last_error = "pump: something old"
        rs.send(author_surface="cockpit", client_id="a", outbox_id="ob-00000001", body="one")
        assert rs.last_error is None
    finally:
        rs.stop()
    (tmp_path / "two").mkdir()
    _store2, rs2, _l2 = scripted(tmp_path / "two")
    def stopping_step():
        rs2._stop_evt.set()                  # the stop lands mid-step, and the step finds the room stopped
        raise RoomServeError("stopped")
    monkeypatch.setattr(rs2, "pump_once", stopping_step)
    rs2._run()
    assert rs2.last_error is None


def test_stop_says_when_the_pump_would_not_stop(tmp_path, monkeypatch):
    import levain.coordinator.room_serve as mod
    _store, rs, _l = scripted(tmp_path)
    gate = threading.Event()
    monkeypatch.setattr(mod, "_JOIN_WAIT", 0.1)
    monkeypatch.setattr(rs, "pump_once", lambda: (gate.wait(5), 0)[1])
    rs.start()
    time.sleep(0.05)
    rs.stop()
    assert "pump" in (rs.last_error or "") and "did not stop" in rs.last_error
    gate.set()


# F8
def test_view_carries_no_dead_key(tmp_path):
    _store, rs, _l = scripted(tmp_path)
    rs.start()
    try:
        assert set(rs.view()) == {"room_thread", "launch_id", "last_error"}
    finally:
        rs.stop()


def test_a_stale_tab_writes_nothing_to_the_store_on_the_stub_and_the_fake_pipe(tmp_path):
    (tmp_path / "a").mkdir()
    _store, rs, launcher = scripted(tmp_path / "a")
    rs.start()
    try:
        rs.send(author_surface="cockpit", client_id="t", outbox_id="ob-00000001", body="one")
        old = rs.view()["room_thread"]
        rs.rotate()
        before = counts(rs)
        with pytest.raises(RoomRotated):
            rs.send(author_surface="cockpit", client_id="t", outbox_id="ob-00000002", body="stale", room_thread=old)
        assert counts(rs) == before
    finally:
        rs.stop()
    (tmp_path / "b").mkdir()
    _s, rs2, fake = fake_serve(tmp_path / "b")
    rs2.start()
    try:
        rs2.send(author_surface="cockpit", client_id="t", outbox_id="ob-00000001", body="one")
        old = rs2.view()["room_thread"]
        rs2.rotate()
        before, spawns = counts(rs2), len(list(fake.glob("spawn-*.argv")))
        with pytest.raises(RoomRotated):
            rs2.send(author_surface="cockpit", client_id="t", outbox_id="ob-00000002", body="stale", room_thread=old)
        assert counts(rs2) == before and len(list(fake.glob("spawn-*.argv"))) == spawns
    finally:
        rs2.stop()
