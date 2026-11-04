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

    def close(self):
        self.closed = True


def scripted(tmp_path, **kw):
    store = make_store(tmp_path)
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
