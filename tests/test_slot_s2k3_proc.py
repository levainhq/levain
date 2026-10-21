"""S1 session 2 fix round 2 (lane K3), proc.py: codex r1 3 (macOS signals by a dropped child's ``kill(-1, sig)``,
never a per-pid root kill). The real drop needs root, so the child's CALL ORDER is unit-tested with fakes and the
VM run proves the drop; the sweep's use of it runs on the fake table and, on macOS, on a real forking group."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from levain.slot import Refused
from levain.slot import proc as P
from levain.slot.proc import STOP, UNKNOWN, ZOMBIE, Proc
from tests.test_slot_s2f1_proc import FORKER, OTHER, SENTINEL, SLOT, Table


# --- the child: drop, THEN kill(-1) -------------------------------------------------------------------------


def test_signal_child_drops_before_it_signals_and_signals_minus_one():
    calls: list = []
    P.signal_child(SLOT, signal.SIGSTOP, drop=lambda uid: calls.append(("drop", uid)),
                   kill=lambda pid, sig: calls.append(("kill", pid, sig)))
    assert calls == [("drop", SLOT), ("kill", -1, signal.SIGSTOP)]


def test_signal_child_never_signals_when_the_drop_is_not_proven():
    calls: list = []

    def refused(uid):
        calls.append(("drop", uid))
        raise Refused("drop-assert", "saved uid")
    with pytest.raises(Refused):
        P.signal_child(SLOT, signal.SIGKILL, drop=refused, kill=lambda pid, sig: calls.append(("kill", pid, sig)))
    assert calls == [("drop", SLOT)]                      # no kill(-1) as root


def test_the_real_drop_is_the_one_drop_with_gid_and_groups_the_uid():
    seen: list = []
    real = P.drop_and_assert
    try:
        P.drop_and_assert = lambda uid, gid, groups, **kw: seen.append((uid, gid, tuple(groups), kw))
        P._drop_to(SLOT)
    finally:
        P.drop_and_assert = real
    assert seen == [(SLOT, SLOT, (SLOT,), {"nproc": None})]


class _Exit(BaseException):
    def __init__(self, code):
        self.code = code


def _exit(code):
    raise _Exit(code)


def test_signal_as_uid_child_path_exits_sent_or_70():
    order: list = []
    with pytest.raises(_Exit) as e:
        P.signal_as_uid(SLOT, signal.SIGKILL, fork=lambda: 0, waitpid=None,
                        child=lambda uid, sig: order.append((uid, sig)), exit=_exit)
    assert e.value.code == P.SIGNAL_SENT and order == [(SLOT, signal.SIGKILL)]

    def boom(uid, sig):
        raise Refused("drop-assert", "x")
    with pytest.raises(_Exit) as e:
        P.signal_as_uid(SLOT, signal.SIGKILL, fork=lambda: 0, waitpid=None, child=boom, exit=_exit)
    assert e.value.code == 70


@pytest.mark.parametrize("status,sent", [(0, True), (70 << 8, False), (signal.SIGKILL, False)])
def test_signal_as_uid_parent_reports_only_a_clean_exit(status, sent):
    reaped: list = []

    def waitpid(pid, flags):
        reaped.append(pid)
        return pid, status
    assert P.signal_as_uid(SLOT, signal.SIGSTOP, fork=lambda: 4321, waitpid=waitpid,
                           child=lambda u, s: pytest.fail("the parent never runs the child"),
                           exit=_exit) is sent
    assert reaped == [4321]


# --- the sweep signals by uid when the host does --------------------------------------------------------------


def uid_signal_table():
    """The fake table, with ``signal_uid`` acting as kill(-1) from the uid would: every process of the uid at
    once; ``send`` must never be called."""
    t = Table()
    sigs: list = []

    def signal_uid(uid, sig):
        sigs.append((uid, sig))
        hit = False
        for pid, p in list(t.procs.items()):
            if uid not in p.uids or p.state == UNKNOWN:
                continue
            hit = True
            if sig == signal.SIGSTOP and p.state != ZOMBIE:
                t.procs[pid] = Proc(pid, p.uids, p.start, STOP)
            elif sig == signal.SIGKILL:
                del t.procs[pid]
        return hit

    def no_send(handle, sig):
        raise AssertionError("a per-pid signal on a host that signals by uid")
    s = replace(t.seams(), signal_uid=signal_uid, pin=lambda pid: pytest.fail("pinned a pid"), send=no_send)
    return t, s, sigs


def test_freeze_and_kill_signal_by_uid_never_by_pid():
    t, s, sigs = uid_signal_table()
    for pid in (100, 101, 102):
        t.add(pid, SLOT)
    t.add(200, OTHER)
    u = P.UidProcs(s)
    assert u.freeze(SLOT, P.V12_SECONDS) is True
    assert {p.state for p in t.procs.values() if SLOT in p.uids} == {STOP}
    assert t.procs[200].state != STOP                             # another uid's process untouched
    assert u.kill(SLOT, P.V12_SECONDS) is True and u.no_process(SLOT) is True
    assert 200 in t.procs
    assert sigs and {x for x, _ in sigs} == {SLOT}
    assert (SLOT, signal.SIGSTOP) in sigs and (SLOT, signal.SIGKILL) in sigs
    assert t.sent == []


def test_a_uid_signal_that_was_not_sent_leaves_the_proof_unmet():
    t, s, sigs = uid_signal_table()
    t.add(100, SLOT)
    s = replace(s, signal_uid=lambda uid, sig: sigs.append((uid, sig)) and False)
    assert P.UidProcs(s).freeze(SLOT, 0.5) is False                # past the bound, never claimed
    assert sigs and t.procs[100].state != STOP


def test_nothing_is_signalled_when_the_uid_has_no_process():
    t, s, sigs = uid_signal_table()
    t.add(200, OTHER)
    assert P.UidProcs(s).kill(SLOT, P.V12_SECONDS) is True and sigs == []


def test_scrub_on_a_uid_signalling_host_signals_the_gateway_by_uid():
    t, s, sigs = uid_signal_table()
    t.add(300, 5002)
    P.scrub(SLOT, 5002, seams=s)
    assert sigs == [(5002, signal.SIGKILL)] and 300 not in t.procs


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS's seams")
def test_darwin_seams_never_signal_a_pid():
    s = P.darwin_seams()
    assert s.signal_uid is P.signal_as_uid
    with pytest.raises(OSError):
        s.pin(os.getpid())
    with pytest.raises(OSError):
        s.send(os.getpid(), 0)


@pytest.mark.skipif(sys.platform != "darwin", reason="the real libproc listing is macOS's")
def test_real_darwin_uid_sweep_on_a_forking_process_group():
    """The sweep's uid path against real processes: the real libproc listing, relabelled so this test's own
    forking group is the target uid; ``signal_uid`` = ``killpg`` on that group, which is what ``kill(-1)`` as
    the uid reaches (the drop itself is the VM's). No per-pid signal is possible: pin refuses."""
    real = P.darwin_seams()
    forker = subprocess.Popen([sys.executable, "-I", "-c", FORKER], start_new_session=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pg = forker.pid

    def mine(pid: int) -> bool:
        try:
            return os.getpgid(pid) == pg
        except (ProcessLookupError, PermissionError):
            return False

    def relabel(p):
        if p is None:
            return None
        return Proc(p.pid, (SENTINEL,) * 3 if mine(p.pid) else (OTHER,) * 3, p.start, p.state)

    def listing():
        return [relabel(p) for p in real.listing() if p.state != UNKNOWN and mine(p.pid)]

    def signal_uid(uid, sig):
        assert uid == SENTINEL
        try:
            os.killpg(pg, sig)
            return True
        except ProcessLookupError:
            return False

    seams = P.ProcSeams(listing=listing, probe=lambda pid: relabel(real.probe(pid)), pin=P._never_pin,
                        send=P._never_send, signal_uid=signal_uid)
    u = P.UidProcs(seams)
    try:
        deadline = time.monotonic() + 5
        while len(listing()) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(listing()) >= 4
        assert u.freeze(SENTINEL, P.V12_SECONDS) is True
        frozen = listing()
        assert frozen and all(p.state == STOP for p in frozen)
        assert u.kill(SENTINEL, P.V12_SECONDS) is True
        forker.wait(5)
        assert u.no_process(SENTINEL) is True
    finally:
        try:
            os.killpg(pg, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        forker.wait(5)


def test_cgroup_freeze_documents_its_scope():
    import inspect
    assert "cgroup-scoped" in inspect.getsource(P.CgroupProcs.freeze)     # comp r1 5's one line
