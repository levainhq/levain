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
    assert SQ.egress_slots(seams(L, R)) == [0, 3]


def test_the_slot_list_is_read_from_root_dirs_only(layout):
    L, R = layout
    add_slot(L, R, 0)
    with pytest.raises(Refused):
        SQ.egress_slots(H.Seams(store=L, run=R))    # the real trust rule: these dirs are mine, not root's


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
        for _ in range(200):
            if path.exists():
                break
            stop.wait(0.025)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(10)
            c.connect(str(path))
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
