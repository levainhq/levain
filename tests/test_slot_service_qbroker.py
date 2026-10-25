"""The queue broker SERVICE program (levain.slot.services.qbroker): the slots it reads, the listeners it keeps, its
instance lock, and one GATE answered over a real slot socket (the broker itself is tests/test_qbroker*.py's)."""
from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from levain.qbroker import __main__ as QM
from levain.qbroker import wire
from levain.qbroker.broker import Resolved
from levain.slot import Refused
from levain.slot import runtime_helper as H
from levain.slot import units
from levain.slot.services import qbroker as SQ

ME, MYGID = os.geteuid(), os.getegid()


@pytest.fixture
def layout():
    """A short root (a unix socket path is bounded), L and R as the service reads them; every dir in my group (a slot
    socket is served only in the slot's group, which the seams below map to mine)."""
    top = Path(tempfile.mkdtemp(prefix="qbs-", dir="/tmp"))
    L, R = top / "L", top / "R"
    for d in ("queue/requests", "queue/answers", "policy", "routes", "slots"):
        (L / d).mkdir(parents=True)
    (R / "qb").mkdir(parents=True)
    for d in (top, L, R, R / "qb"):
        os.chown(d, -1, MYGID)
    yield L, R
    shutil.rmtree(top)


def add_slot(L, R, n, *, run_dir=True):
    (L / "slots" / f"e{n}").mkdir()
    if run_dir:
        d = R / "qb" / f"slot{n}"
        d.mkdir()
        os.chown(d, -1, MYGID)


def seams(L, R, accounts=None):
    def uid(name):
        if accounts is not None and name not in accounts:
            raise Refused("account-missing", name)
        return ME
    return H.Seams(store=L, run=R, trusted=lambda st: True, account_uid=uid, group_gid=lambda name: MYGID)


class FakeListener:
    def __init__(self, broker, slot, path, uid, *, expected_gid):
        self.slot, self.path, self.uid, self.gid = slot, path, uid, expected_gid
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_the_slots_served_are_the_egress_dirs_under_L_slots(layout):
    L, R = layout
    for name in ("e0", "e3", "v0", "v1", "e01", "ex", "notes"):
        (L / "slots" / name).mkdir()
    (L / "slots" / "e7").write_text("a file, not a slot")
    assert SQ.scan(seams(L, R)) == ([0, 3], {})


def test_the_slot_list_is_read_from_root_dirs_only(layout):
    L, R = layout
    add_slot(L, R, 0)
    with pytest.raises(Refused):
        SQ.scan(H.Seams(store=L, run=R))            # the real trust rule: these dirs are mine, not root's


def test_sync_serves_each_slot_as_its_primary_account(layout):
    L, R = layout
    add_slot(L, R, 0)
    add_slot(L, R, 2)
    names = []
    s = seams(L, R)
    real_uid = s.account_uid
    s.account_uid = lambda n: (names.append(n), real_uid(n))[1]
    slots = SQ.Slots("broker", s, log=lambda m: None, listener=FakeListener)
    slots.sync()
    assert sorted(slots.running) == [0, 2] and names == ["_levain_s0", "_levain_s2"]
    lst = slots.running[2]
    assert lst.started and lst.path == R / "qb" / "slot2" / "q.sock" and (lst.uid, lst.gid) == (ME, MYGID)


def test_a_slot_not_ready_is_logged_once_and_served_once_it_is(layout):
    L, R = layout
    add_slot(L, R, 0)
    add_slot(L, R, 1, run_dir=False)
    accounts = {"_levain_s0"}
    logs = []
    slots = SQ.Slots("broker", seams(L, R, accounts), log=logs.append, listener=FakeListener)
    slots.sync()
    slots.sync()
    assert sorted(slots.running) == [0]
    assert len([m for m in logs if m.startswith("slot 1: not served yet")]) == 1      # a repeat is not logged again
    (R / "qb" / "slot1").mkdir()
    os.chown(R / "qb" / "slot1", -1, MYGID)
    slots.sync()
    assert sorted(slots.running) == [0]                                              # its account is still missing
    accounts.add("_levain_s1")
    slots.sync()
    assert sorted(slots.running) == [0, 1]


def test_a_slot_gone_from_L_slots_stops_being_served(layout):
    L, R = layout
    add_slot(L, R, 0)
    add_slot(L, R, 1)
    slots = SQ.Slots("broker", seams(L, R), log=lambda m: None, listener=FakeListener)
    slots.sync()
    gone = slots.running[1]
    (L / "slots" / "e1").rmdir()
    for _ in range(SQ.MISSES_TO_STOP):
        slots.sync()
    assert sorted(slots.running) == [0] and gone.stopped


def test_an_unreadable_slot_list_keeps_what_runs(layout):
    L, R = layout
    add_slot(L, R, 0)
    logs = []
    slots = SQ.Slots("broker", seams(L, R), log=logs.append, listener=FakeListener)
    slots.sync()
    (L / "slots" / "e0").rmdir()
    (L / "slots").rmdir()
    slots.sync()
    assert sorted(slots.running) == [0] and not slots.running[0].stopped
    assert any("the slot list could not be read" in m for m in logs)


def test_a_second_instance_waits_then_gives_up(layout, monkeypatch):
    L, R = layout
    monkeypatch.setattr(SQ, "INSTANCE_WAIT_S", 0.1)
    s = seams(L, R)
    held = SQ.take_instance_lock(s, wait_s=0)
    try:
        assert held is not None and SQ.take_instance_lock(s, wait_s=0.1) is None
        assert SQ.serve(s, stop=threading.Event(), log=lambda m: None) == SQ.EX_INSTANCE_HELD
    finally:
        os.close(held)
    again = SQ.take_instance_lock(s, wait_s=0)
    assert again is not None
    os.close(again)


def test_the_instance_wait_is_the_reapers():
    from levain.slot.services import reaper as SR
    assert (SQ.INSTANCE_WAIT_S, SQ.EX_INSTANCE_HELD) == (SR.INSTANCE_WAIT_S, SR.EX_INSTANCE_HELD)


def test_serve_answers_a_gate_on_the_slot_socket_and_unlinks_it_on_stop(layout, monkeypatch):
    L, R = layout
    add_slot(L, R, 0)
    built = []

    def build(ns, *, limits_owner):
        assert (ns.store, ns.run, limits_owner) == (str(L), str(R), ME)
        b = QM.build_broker(ns, limits_owner=limits_owner)
        monkeypatch.setattr(b, "_resolve", lambda slot, cap: Resolved(session="sess-0", kind="interactive", run=None))
        built.append(b)
        return b

    stop = threading.Event()
    got = []
    t = threading.Thread(target=lambda: got.append(SQ.serve(seams(L, R), limits_owner=ME, rescan_s=0.05, stop=stop,
                                                             log=lambda m: None, build=build)))
    t.start()
    path = R / "qb" / "slot0" / "q.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(10)
            for _ in range(200):                     # the path exists from bind; connect works from listen
                try:
                    c.connect(str(path))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    stop.wait(0.025)
            wire.write_frame(c, {"v": 1, "op": "GATE", "id": 1, "cap": "cap-0", "agent": "claude_code",
                                 "tool_name": "Read", "input_digest": "sha256:" + "0" * 64})
            answer = wire.read_frame(c, timeout=10)
    finally:
        stop.set()
        t.join(10)
    assert (answer["id"], answer["ok"], answer["gate"]) == (1, True, "go")
    assert got == [0] and not path.exists() and len(built) == 1


def test_main_refuses_any_user_but_the_broker_account(monkeypatch):
    def missing(name):
        raise Refused("account-missing", name)

    real = H.Seams
    for uid in (lambda n: ME + 1, missing):
        monkeypatch.setattr(SQ.H, "Seams", lambda uid=uid: real(account_uid=uid))
        assert SQ.main([]) == SQ.EX_WRONG_USER


def test_setup_installs_the_queue_broker_unit_now_that_its_program_exists():
    import levain
    have, pending = units.built_services("darwin", Path(levain.__file__).parent)
    assert "qbroker" in [s.name for s in have] and not [p for p in pending if "qbroker" in p]


# --- L3 r1 (RULINGS_R1): a stopped slot's open connections, retired slots, any scan failure, cleanup, two misses ---

TS = "2026-10-25T00:00:00Z"


def set_state(L, n, state):
    from levain.slot import records as RR
    from levain.slot.ids import SlotId
    (L / "slots" / f"e{n}" / "state").write_bytes(RR.slot_state_bytes(RR.SlotState(
        SlotId("egress", n), rev=0, lease_seq=0, state=state, dispatch_eligible=True, changed_at=TS)))


class CountingBroker:
    def __init__(self):
        self.handled = []

    def handle(self, slot, message):
        self.handled.append((slot, message.get("id")))
        return {"id": message.get("id"), "ok": True}


def test_a_slot_no_longer_served_closes_a_connection_already_open(layout):
    L, R = layout
    add_slot(L, R, 0)
    b = CountingBroker()
    slots = SQ.Slots(b, seams(L, R), log=lambda m: None)
    slots.sync()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(5)
            c.connect(str(R / "qb" / "slot0" / "q.sock"))
            wire.write_frame(c, {"v": 1, "op": "PING", "id": 1})
            assert wire.read_frame(c, timeout=5)["id"] == 1
            (L / "slots" / "e0").rmdir()
            slots.sync()
            slots.sync()
            assert 0 not in slots.running
            try:
                wire.write_frame(c, {"v": 1, "op": "PING", "id": 2})
                got = wire.read_frame(c, timeout=2)
            except (OSError, wire.FrameError):
                got = None
        assert got is None and b.handled == [(0, 1)]
    finally:
        slots.stop()


def test_a_retired_slot_is_not_served_and_one_without_a_state_record_is(layout):
    L, R = layout
    for n in (0, 1, 2):
        add_slot(L, R, n)
    set_state(L, 0, "reusable")
    set_state(L, 1, "reusable")                       # e2: no state record (an install before state files)
    slots = SQ.Slots("broker", seams(L, R), log=lambda m: None, listener=FakeListener)
    slots.sync()
    assert sorted(slots.running) == [0, 1, 2]
    gone = slots.running[1]
    set_state(L, 1, "retired")                        # retired by its record; the directory stays
    slots.sync()
    slots.sync()
    assert sorted(slots.running) == [0, 2] and gone.stopped


def test_the_served_set_is_install_helpers_slots_in_service(layout):
    from levain.slot import install_helper as IH
    L, R = layout
    for n in (0, 1, 2, 3):
        add_slot(L, R, n)
    set_state(L, 0, "reusable")
    set_state(L, 1, "retired")
    set_state(L, 3, "scrubbed")
    s = seams(L, R)
    store = H.Store(s)
    try:
        want = list(IH.slots_in_service(store)["egress"])
    finally:
        store.close()
    assert SQ.scan(s)[0] == want == [0, 2, 3]


def test_a_slot_whose_state_cannot_be_read_is_never_started_and_stopped_after_two_scans(layout):
    L, R = layout
    add_slot(L, R, 0)
    add_slot(L, R, 1)
    logs = []
    slots = SQ.Slots("broker", seams(L, R), log=logs.append, listener=FakeListener)
    slots.sync()
    first = slots.running[0]
    (L / "slots" / "e0" / "state").write_bytes(b"not a state record")
    (L / "slots" / "e1" / "state").write_bytes(b"not a state record")
    del slots.running[1]                               # as if it had never started
    slots.sync()                                      # one unreadable scan is absorbed
    assert sorted(slots.running) == [0] and not first.stopped
    slots.sync()                                      # the second fails closed: a miss like any other
    assert slots.running == {} and first.stopped
    assert len([m for m in logs if m.startswith("slot 1: its state record could not be read")]) == 1


def test_configured_slots_skips_a_name_whose_digits_are_not_ascii(layout):
    L, R = layout
    add_slot(L, R, 0)
    (L / "slots" / "e\u00b2").mkdir()                 # "²".isdigit() is True and int("²") raises ValueError
    (L / "slots" / "v\u0661").mkdir()                 # int("١") == 1: not the name "v1"
    store = H.Store(seams(L, R))
    try:
        assert H.configured_slots(store) == {"egress": frozenset({0}), "none": frozenset()}
    finally:
        store.close()


def test_any_failure_reading_the_slot_list_keeps_what_runs(layout, monkeypatch):
    L, R = layout
    add_slot(L, R, 0)
    logs = []
    slots = SQ.Slots("broker", seams(L, R), log=logs.append, listener=FakeListener)
    slots.sync()

    def corrupt(store):
        raise ValueError("a corrupt store")   # configured_slots raised this on L/slots/e² before its isascii() check

    monkeypatch.setattr(H, "configured_slots", corrupt)
    slots.sync()
    slots.sync()
    assert sorted(slots.running) == [0] and not slots.running[0].stopped
    assert len([m for m in logs if "the slot list could not be read: ValueError" in m]) == 1


def test_a_slot_is_stopped_only_after_two_scans_in_a_row_miss_it(layout):
    L, R = layout
    add_slot(L, R, 0)
    slots = SQ.Slots("broker", seams(L, R), log=lambda m: None, listener=FakeListener)
    slots.sync()
    first = slots.running[0]
    (L / "slots" / "e0").rmdir()
    slots.sync()                                      # one miss (a rename mid-scan): still served
    assert slots.running.get(0) is first and not first.stopped
    (L / "slots" / "e0").mkdir()
    slots.sync()                                      # seen again: the count starts over
    (L / "slots" / "e0").rmdir()
    slots.sync()
    assert slots.running.get(0) is first and not first.stopped
    slots.sync()
    assert 0 not in slots.running and first.stopped


class BadStop(FakeListener):
    def stop(self):
        self.stopped = True
        raise RuntimeError("stop failed")


class BadStart(FakeListener):
    made = []

    def start(self):
        BadStart.made.append(self)
        raise RuntimeError("cannot start new thread")


def test_one_stop_that_raises_stops_the_others_and_never_ends_the_scan(layout):
    L, R = layout
    for n in (0, 1, 2):
        add_slot(L, R, n)
    logs = []
    slots = SQ.Slots("broker", seams(L, R), log=logs.append, listener=BadStop)
    slots.sync()
    held = dict(slots.running)
    (L / "slots" / "e0").rmdir()
    (L / "slots" / "e1").rmdir()
    slots.sync()
    slots.sync()
    assert sorted(slots.running) == [2] and held[0].stopped and held[1].stopped
    slots.stop()
    assert slots.running == {} and held[2].stopped
    assert len([m for m in logs if "RuntimeError: stop failed" in m]) == 3


def test_a_listener_whose_start_raised_is_stopped(layout):
    L, R = layout
    add_slot(L, R, 0)
    BadStart.made.clear()
    slots = SQ.Slots("broker", seams(L, R), log=lambda m: None, listener=BadStart)
    slots.sync()
    assert slots.running == {} and [x.stopped for x in BadStart.made] == [True]


def test_a_listener_start_that_fails_after_bind_leaves_no_socket(layout, monkeypatch):
    from levain.qbroker import server as S
    L, R = layout
    add_slot(L, R, 0)
    path = R / "qb" / "slot0" / "q.sock"
    lst = S.SlotListener(CountingBroker(), 0, path, ME, expected_gid=MYGID)

    def no_thread(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(S.threading.Thread, "start", no_thread)
    with pytest.raises(RuntimeError):
        lst.start()
    monkeypatch.undo()
    assert not path.exists() and lst._sock is None and lst._thread is None
    lst.stop()                                        # and stopping it after is harmless


def test_serve_restores_the_signals_and_frees_the_lock_whatever_a_stop_raises(layout):
    import signal as SIG
    L, R = layout
    add_slot(L, R, 0)
    before = SIG.getsignal(SIG.SIGTERM)

    class StopsServe(BadStop):
        def start(self):
            os.kill(os.getpid(), SIG.SIGTERM)        # serve's own handler sets its stop

    logs = []
    assert SQ.serve(seams(L, R), limits_owner=ME, rescan_s=0.01, log=logs.append,
                    build=lambda ns, *, limits_owner: "broker", listener=StopsServe) == 0
    assert SIG.getsignal(SIG.SIGTERM) is before
    again = SQ.take_instance_lock(seams(L, R), wait_s=0)
    assert again is not None
    os.close(again)
    assert any("RuntimeError: stop failed" in m for m in logs)
