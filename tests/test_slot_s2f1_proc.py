"""CONTRACT §5.6, §7.1, §7.3, §11 V12: process control for one uid (``levain/slot/proc.py``).

The unit tests drive a FAKE process table through :class:`ProcSeams` (fake clock, fake signals with
pidfd semantics: a signal to a pinned process that has since died is ESRCH, never delivered to the
pid's next owner). One darwin test runs the real libproc listing and real signals against a process
group this test creates, re-labelled as a sentinel uid, so nothing outside that group is ever
signalled."""
from __future__ import annotations

import os
import signal
import struct
import subprocess
import sys
import time

import pytest

from levain.slot import Refused
from levain.slot import end as E
from levain.slot import proc as P
from levain.slot.proc import RUN, STOP, UNKNOWN, ZOMBIE, Proc

SLOT, GW, CHILD, OTHER = 5001, 5002, 5003, 7777


class Table:
    """A fake host: ``procs`` by pid; hooks run on each listing / probe so a test can fork, exit or
    reuse a pid between the list and the signal."""

    def __init__(self, *procs: Proc) -> None:
        self.procs = {p.pid: p for p in procs}
        self.t = 0.0
        self.sent: list[tuple[int, int]] = []
        self.order: list[tuple[str, int]] = []
        self.lists = 0
        self.ignore_stop: set[int] = set()
        self.ignore_kill: set[int] = set()
        self.on_list: list = []        # callables(table, n) run before listing number n answers
        self.on_probe: list = []       # callables(table, pid) run before a probe answers
        self.hide_once: set[int] = set()
        self.files: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []
        self.on_read: list = []

    def add(self, pid: int, uid: int, state: str = RUN, start: int | None = None) -> Proc:
        p = Proc(pid, (uid, uid, uid), start if start is not None else pid * 10, state)
        self.procs[pid] = p
        return p

    def listing(self):
        self.lists += 1
        for h in self.on_list:
            h(self, self.lists)
        out = [p for pid, p in sorted(self.procs.items()) if pid not in self.hide_once]
        self.hide_once = set()
        return out

    def probe(self, pid):
        self.order.append(("probe", pid))
        for h in self.on_probe:
            h(self, pid)
        return self.procs.get(pid)

    def pin(self, pid):
        self.order.append(("pin", pid))
        p = self.procs.get(pid)
        if p is None:
            raise ProcessLookupError(pid)
        return (pid, p.start)

    def send(self, handle, sig):
        pid, start = handle
        p = self.procs.get(pid)
        if p is None or p.start != start:
            raise ProcessLookupError(pid)
        self.sent.append((pid, sig))
        if sig == signal.SIGSTOP and pid not in self.ignore_stop and p.state == RUN:
            self.procs[pid] = Proc(pid, p.uids, p.start, STOP)
        elif sig == signal.SIGKILL and pid not in self.ignore_kill:
            del self.procs[pid]

    def read(self, path):
        for h in self.on_read:
            h(self, path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write(self, path, text):
        if path not in self.files:
            raise FileNotFoundError(path)
        self.writes.append((path, text))

    def sleep(self, s):
        self.t += s

    def seams(self) -> P.ProcSeams:
        return P.ProcSeams(listing=self.listing, probe=self.probe, pin=self.pin, send=self.send,
                           read=self.read, write=self.write, clock=lambda: self.t, sleep=self.sleep,
                           self_uids=lambda: (501, 501), self_pid=lambda: 99999)


def pids_sent(t: Table, sig: int) -> set[int]:
    return {pid for pid, s in t.sent if s == sig}


# --- the uid sweep -------------------------------------------------------------------------------------

def test_freeze_stops_every_process_of_the_uid_and_nothing_else():
    t = Table()
    t.add(100, SLOT)
    t.add(101, SLOT)
    t.add(102, OTHER)
    t.add(103, SLOT, ZOMBIE)
    assert P.UidProcs(t.seams()).freeze(SLOT, 10.0) is True
    assert t.procs[100].state == STOP and t.procs[101].state == STOP
    assert t.procs[102].state == RUN
    assert pids_sent(t, signal.SIGSTOP) == {100, 101}


def test_freeze_catches_a_fork_between_scans():
    t = Table()
    t.add(100, SLOT)

    def fork(tab, n):
        if n == 2:
            tab.add(200, SLOT)   # forked by 100 after scan 1 listed it, before it stopped
    t.on_list.append(fork)
    assert P.UidProcs(t.seams()).freeze(SLOT, 10.0) is True
    assert t.procs[200].state == STOP


def test_freeze_needs_two_agreeing_scans_a_fork_then_exit_is_caught():
    """Scan 1 misses a child whose parent forked it and exited between the pid list and the read:
    one clean scan is not a proof."""
    t = Table()
    t.add(200, SLOT)
    t.hide_once = {200}
    assert P.UidProcs(t.seams()).freeze(SLOT, 10.0) is True
    assert t.procs[200].state == STOP
    assert t.lists >= 3


def test_freeze_unproven_past_the_bound_when_a_process_stays_running():
    t = Table()
    t.add(100, SLOT)
    t.ignore_stop.add(100)
    assert P.UidProcs(t.seams()).freeze(SLOT, 10.0) is False
    assert 10.0 <= t.t < 10.0 + P.POLL_MAX_S + 1e-9


def test_pid_reused_by_another_uid_between_list_and_signal_is_not_signalled():
    t = Table()
    t.add(100, SLOT)

    def reuse(tab, pid):
        if pid == 100 and tab.procs[100].uids[0] == SLOT:
            tab.procs[100] = Proc(100, (OTHER,) * 3, 4242, RUN)   # 100 died; another uid got its pid
    t.on_probe.append(reuse)
    assert P.UidProcs(t.seams()).freeze(SLOT, 10.0) is True     # no process of SLOT is left running
    assert t.sent == []
    assert t.procs[100].state == RUN


def test_pid_reused_by_the_same_uid_is_rechecked_by_start_time():
    t = Table()
    t.add(100, SLOT, start=1)
    p = t.procs[100]
    t.procs[100] = Proc(100, p.uids, 2, RUN)           # same uid, new process
    assert P.signal_checked(p, SLOT, signal.SIGKILL, t.seams()) is False
    assert t.sent == []


def test_the_same_process_no_longer_of_the_uid_is_not_signalled():
    """Same pid, same start time, every id now another uid's (a root act): the uid recheck alone."""
    t = Table()
    p = t.add(100, SLOT, start=1)
    t.procs[100] = Proc(100, (OTHER,) * 3, 1, RUN)
    assert P.signal_checked(p, SLOT, signal.SIGKILL, t.seams()) is False
    assert t.sent == []


def test_the_pin_comes_before_the_live_check():
    t = Table()
    p = t.add(100, SLOT)
    assert P.signal_checked(p, SLOT, signal.SIGSTOP, t.seams()) is True
    assert t.order[:2] == [("pin", 100), ("probe", 100)]


def test_a_pinned_process_that_dies_after_the_check_gets_no_signal():
    """pidfd semantics: a pid reused after the check is a different process to the pin."""
    t = Table()
    p = t.add(100, SLOT, start=1)
    seams = t.seams()
    real_probe = seams.probe

    def probe_then_reuse(pid):
        got = real_probe(pid)
        t.procs[100] = Proc(100, (OTHER,) * 3, 9, RUN)
        return got
    seams.probe = probe_then_reuse
    assert P.signal_checked(p, SLOT, signal.SIGKILL, seams) is False
    assert t.sent == [] and t.procs[100].uids[0] == OTHER


def test_never_signals_pid_1_or_itself():
    t = Table()
    one, me = t.add(1, SLOT), t.add(99999, SLOT)
    assert P.signal_checked(one, SLOT, signal.SIGKILL, t.seams()) is False
    assert P.signal_checked(me, SLOT, signal.SIGKILL, t.seams()) is False
    assert t.sent == []


def test_kill_kills_running_and_stopped_and_proves_none_left():
    t = Table()
    t.add(100, SLOT)
    t.add(101, SLOT, STOP)
    t.add(102, SLOT, ZOMBIE)
    t.add(103, OTHER)
    u = P.UidProcs(t.seams())
    assert u.kill(SLOT, 10.0) is True
    assert pids_sent(t, signal.SIGKILL) == {100, 101}
    assert set(t.procs) == {102, 103}
    assert u.no_process(SLOT) is True
    assert u.no_process(OTHER) is False


def test_kill_unproven_when_a_process_survives():
    t = Table()
    t.add(100, SLOT)
    t.ignore_kill.add(100)
    u = P.UidProcs(t.seams())
    assert u.kill(SLOT, 10.0) is False
    assert t.t >= 10.0
    assert u.no_process(SLOT) is False


def test_kill_catches_forks_until_the_sweep_is_empty():
    t = Table()
    t.add(100, SLOT)

    def fork(tab, n):
        if n in (2, 3):
            tab.add(300 + n, SLOT)
    t.on_list.append(fork)
    assert P.UidProcs(t.seams()).kill(SLOT, 10.0) is True
    assert not any(p.of(SLOT) for p in t.procs.values())


def test_any_of_real_effective_saved_is_the_uids():
    t = Table()
    t.procs[100] = Proc(100, (OTHER, SLOT, OTHER), 1, RUN)    # effective
    t.procs[101] = Proc(101, (OTHER, OTHER, SLOT), 2, RUN)    # saved
    t.procs[102] = Proc(102, (SLOT, OTHER, OTHER), 3, RUN)    # real
    u = P.UidProcs(t.seams())
    assert u.no_process(SLOT) is False
    assert u.kill(SLOT, 10.0) is True
    assert set(t.procs) == set()


def test_an_unreadable_pid_fails_every_proof_closed_and_is_never_signalled():
    t = Table()
    t.procs[100] = Proc(100, (), 0, UNKNOWN)
    u = P.UidProcs(t.seams())
    assert u.no_process(SLOT) is False
    assert u.freeze(SLOT, 1.0) is False
    assert u.kill(SLOT, 1.0) is False
    assert t.sent == []


def test_a_failing_listing_or_signal_is_unproven_never_raised():
    t = Table()
    seams = t.seams()

    def broken():
        raise OSError(5, "listing")
    seams.listing = broken
    u = P.UidProcs(seams)
    assert u.no_process(SLOT) is False and u.kill(SLOT, 1.0) is False and u.freeze(SLOT, 1.0) is False
    t = Table()
    t.add(100, SLOT)
    seams = t.seams()

    def eperm(_h, _sig):
        raise PermissionError(1, "send")
    seams.send = eperm
    assert P.UidProcs(seams).kill(SLOT, 1.0) is False
    assert 100 in t.procs


def test_no_process_scans_twice():
    t = Table()
    t.add(100, SLOT)
    t.hide_once = {100}
    assert P.UidProcs(t.seams()).no_process(SLOT) is False


@pytest.mark.parametrize("uid", [0, -1, 501, "5001", None, True])
def test_refuses_root_self_and_non_uids(uid):
    t = Table()
    u = P.UidProcs(t.seams())
    for call in (lambda: u.freeze(uid, 1.0), lambda: u.kill(uid, 1.0), lambda: u.no_process(uid)):
        with pytest.raises(Refused) as e:
            call()
        assert e.value.name == "bad-target-uid"
    assert t.lists == 0


# --- SCRUBBED ------------------------------------------------------------------------------------------

def test_scrub_kills_the_gateway_then_proves_all_three_clean():
    t = Table()
    t.add(100, GW)
    t.add(101, GW, STOP)
    P.scrub(SLOT, GW, CHILD, seams=t.seams())
    assert pids_sent(t, signal.SIGKILL) == {100, 101}
    assert t.procs == {}


def test_scrub_never_kills_the_slot_uid_and_names_the_survivor():
    t = Table()
    t.add(100, GW)
    t.add(200, SLOT)
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, CHILD, seams=t.seams())
    assert e.value.name == f"scrub-processes-remain:{SLOT}"
    assert pids_sent(t, signal.SIGKILL) == {100}
    assert 200 in t.procs


def test_scrub_names_a_surviving_child_and_a_surviving_gateway():
    t = Table()
    t.add(300, CHILD)
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, CHILD, seams=t.seams())
    assert e.value.name == f"scrub-processes-remain:{CHILD}"
    t = Table()
    t.add(100, GW)
    t.ignore_kill.add(100)
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, seams=t.seams(), bound_s=1.0)
    assert e.value.name == f"scrub-processes-remain:{GW}"


def test_scrub_without_a_child_uid_checks_two():
    t = Table()
    t.add(300, CHILD)
    P.scrub(SLOT, GW, seams=t.seams())
    assert 300 in t.procs and t.sent == []


def test_scrub_refuses_a_bad_uid_before_any_act():
    t = Table()
    t.add(100, GW)
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, 0, seams=t.seams())
    assert e.value.name == "bad-target-uid"
    assert t.sent == [] and t.lists == 0


# --- the Linux cgroup ---------------------------------------------------------------------------------

CG = "/sys/fs/cgroup/levain.slice/e0.scope/s-0000000000000001"


def cg_table(populated="1", frozen="0") -> Table:
    t = Table()
    t.files = {CG + "/cgroup.events": f"populated {populated}\nfrozen {frozen}\n", CG + "/cgroup.freeze": "0",
               CG + "/cgroup.kill": ""}
    return t


def test_cgroup_freeze_writes_freeze_and_waits_for_frozen():
    t = cg_table()

    def kernel(tab, path):
        if (CG + "/cgroup.freeze", "1") in tab.writes and tab.t >= 0.05:
            tab.files[CG + "/cgroup.events"] = "populated 1\nfrozen 1\n"
    t.on_read.append(kernel)
    assert P.CgroupProcs(CG, t.seams()).freeze(SLOT, 10.0) is True
    assert t.writes == [(CG + "/cgroup.freeze", "1")]
    assert t.t >= 0.05


def test_cgroup_freeze_unproven_past_the_bound():
    t = cg_table()
    assert P.CgroupProcs(CG, t.seams()).freeze(SLOT, 10.0) is False
    assert t.t >= 10.0


def test_cgroup_kill_writes_kill_and_waits_for_populated_0():
    t = cg_table()

    def kernel(tab, path):
        if (CG + "/cgroup.kill", "1") in tab.writes:
            tab.files[CG + "/cgroup.events"] = "populated 0\nfrozen 1\n"
    t.on_read.append(kernel)
    c = P.CgroupProcs(CG, t.seams())
    assert c.kill(SLOT, 10.0) is True
    assert t.writes == [(CG + "/cgroup.kill", "1")]
    assert c.no_process(SLOT) is True


def test_cgroup_proof_needs_populated_0_and_no_uid_process_anywhere():
    t = cg_table(populated="1")
    c = P.CgroupProcs(CG, t.seams())
    assert c.no_process(SLOT) is False
    t = cg_table(populated="0")
    t.add(100, SLOT)                    # escaped the cgroup (a root act): the uid test still sees it
    c = P.CgroupProcs(CG, t.seams())
    assert c.no_process(SLOT) is False
    assert c.kill(SLOT, 1.0) is True    # the cgroup is empty ...
    assert t.sent == []                 # ... and the cgroup kill sends no uid signal (END's proof refuses)


def test_absent_cgroup_falls_back_to_the_uid_sweep():
    t = Table()
    c = P.CgroupProcs(CG, t.seams())
    assert c.freeze(SLOT, 1.0) is True and c.kill(SLOT, 1.0) is True and c.no_process(SLOT) is True
    t.add(100, SLOT)
    assert c.freeze(SLOT, 1.0) is False and c.no_process(SLOT) is False


@pytest.mark.parametrize("path", ["/sys/fs/cgroup/other/x", "/sys/fs/cgroup/levain.slice", CG + "/",
                                  "/sys/fs/cgroup/levain.slice/../x", "/sys/fs/cgroup/levain.slice//x",
                                  "/sys/fs/cgroup/levain.slice/e0.scope/./s"])
def test_cgroup_path_outside_the_slice_refused(path):
    with pytest.raises(Refused) as e:
        P.CgroupProcs(path, Table().seams())
    assert e.value.name == "bad-value"


def test_verifier_step_child_cgroup_accepted():
    P.CgroupProcs(CG + "/step3", Table().seams())


# --- parsers -------------------------------------------------------------------------------------------

def stat_line(pid, comm, state, start):
    fields = [state] + ["0"] * 18 + [str(start)] + ["0"] * 10
    return f"{pid} ({comm}) " + " ".join(fields) + "\n"


STATUS = "Name:\tx\nState:\tS (sleeping)\nUid:\t5001\t5002\t5003\t5004\nGid:\t1\t1\t1\t1\n"


def test_parse_linux_proc_comm_with_parens_and_spaces():
    p = P.parse_linux_proc(42, stat_line(42, "a) (b c)", "S", 123456), STATUS)
    assert p == Proc(42, (5001, 5002, 5003), 123456, RUN)
    assert P.parse_linux_proc(42, stat_line(42, "x", "Z", 1), STATUS).state == ZOMBIE
    assert P.parse_linux_proc(42, stat_line(42, "x", "T", 1), STATUS).state == STOP
    assert P.parse_linux_proc(42, stat_line(42, "x", "D", 1), STATUS).state == RUN
    assert P.parse_linux_proc(42, stat_line(42, "x", "S", 1), "Name:\tx\n").state == UNKNOWN


def test_linux_probe_rereads_stat_around_status():
    reads = {"n": 0}

    def read(path):
        if path.endswith("/stat"):
            reads["n"] += 1
            return stat_line(7, "x", "S", 1 if reads["n"] == 1 else 2)   # reused between reads 1 and 2
        return STATUS
    p = P.linux_probe(7, read=read)
    assert p is not None and p.start == 2 and reads["n"] == 4


def test_linux_probe_gone_unknown_and_listing():
    def gone(path):
        raise FileNotFoundError(path)

    def denied(path):
        raise PermissionError(path)
    assert P.linux_probe(7, read=gone) is None
    assert P.linux_probe(7, read=denied).state == UNKNOWN
    flip = {"n": 0}

    def unstable(path):
        if path.endswith("/stat"):
            flip["n"] += 1
            return stat_line(7, "x", "S", flip["n"])
        return STATUS
    assert P.linux_probe(7, read=unstable).state == UNKNOWN
    probes = {1: Proc(1, (0, 0, 0), 1, RUN), 7: None}
    out = P.linux_listing(listdir=lambda d: ["1", "7", "self", "sys"], probe=lambda pid: probes[pid])
    assert out == [probes[1]]


def test_parse_bsdinfo():
    raw = struct.pack("=12I16s32s6I2Q", 0, 4, 0, 42, 1, 5002, 20, 5001, 20, 5003, 20, 0, b"x", b"x",
                      0, 42, 0, 0, 0, 0, 1700000000, 123456)
    assert P.parse_bsdinfo(42, raw) == Proc(42, (5001, 5002, 5003), 1700000000123456, STOP)
    raw2 = raw[:4] + struct.pack("=I", 2) + raw[8:]
    assert P.parse_bsdinfo(42, raw2).state == RUN


# --- wiring --------------------------------------------------------------------------------------------

def test_v12_is_ends():
    assert P.V12_SECONDS == E.V12_SECONDS


def test_end_seams_shape():
    t = Table()
    freeze, kill, none = P.end_seams("darwin", seams=t.seams())
    seams = E.EndSeams(cas=lambda *a: None, freeze=freeze, kill=kill, no_process=none)
    assert seams.freeze(SLOT, 1.0) is True and seams.kill(SLOT, 1.0) is True and seams.no_process(SLOT)
    with pytest.raises(Refused):
        P.end_seams("linux", seams=t.seams())
    with pytest.raises(Refused):
        P.end_seams("darwin", CG, seams=t.seams())
    f, k, n = P.end_seams("linux", CG, seams=cg_table(populated="0", frozen="1").seams())
    assert f(SLOT, 1.0) and k(SLOT, 1.0) and n(SLOT)


# --- real macOS: libproc + signals, on a process group this test creates ------------------------------

FORKER = ("import os, time\n"
          "while True:\n"
          "    if os.fork() == 0:\n"
          "        time.sleep(60)\n"
          "        os._exit(0)\n"
          "    time.sleep(0.02)\n")
SENTINEL = 2_000_000_000


@pytest.mark.skipif(sys.platform != "darwin", reason="the real libproc sweep is macOS's")
def test_real_darwin_sweep_on_a_forking_process_group():
    real = P.darwin_seams()
    forker = subprocess.Popen([sys.executable, "-I", "-c", FORKER], start_new_session=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pg = forker.pid

    def mine(pid: int) -> bool:
        try:
            return os.getpgid(pid) == pg
        except (ProcessLookupError, PermissionError):
            return False

    def relabel(p: Proc | None) -> Proc | None:
        if p is None:
            return None
        uids = (SENTINEL,) * 3 if mine(p.pid) else (OTHER,) * 3
        return Proc(p.pid, uids, p.start, p.state)

    def listing():
        return [relabel(p) for p in real.listing() if p.state != UNKNOWN and mine(p.pid)]

    def send(pid, sig):
        assert mine(pid), pid                    # belt and braces: only this test's group
        os.kill(pid, sig)

    seams = P.ProcSeams(listing=listing, probe=lambda pid: relabel(real.probe(pid)), pin=lambda pid: pid,
                        send=send)
    u = P.UidProcs(seams)
    try:
        deadline = time.monotonic() + 5
        while len(listing()) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(listing()) >= 4
        assert u.no_process(SENTINEL) is False
        t0 = time.monotonic()
        assert u.freeze(SENTINEL, P.V12_SECONDS) is True
        assert time.monotonic() - t0 < P.V12_SECONDS
        frozen = listing()
        assert frozen and all(p.state == STOP for p in frozen)
        time.sleep(0.2)
        assert {p.pid for p in listing()} == {p.pid for p in frozen}     # stopped: nothing new forks
        assert u.kill(SENTINEL, P.V12_SECONDS) is True
        forker.wait(5)
        assert u.no_process(SENTINEL) is True
    finally:
        try:
            os.killpg(pg, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        forker.wait(5)
