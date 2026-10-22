"""R1a fix round 1 (reaper DESIGN §3.1, head rulings H1-H9). Each test from a failure reproduced on R1a's tip 33ccdce5
(raw: levain-memory 1021-29/r1a-fix1/):

1. H1: two processes of the slot uid looping ``kill(-1, SIGCONT)`` resume each other between freeze passes, so the
   freeze never reaches two identical all-stopped scans. ``end_domain`` answered ``freeze-unproven`` without one
   SIGKILL and both processes outlived the call, on the macOS route (bootout seam) and the Linux shape (F, K1, P).
2. H3: one signalling child that neither exits nor stops (a fork that inherited a held libc lock) cost the whole
   remaining deadline and aborted ``end_domain`` with ``signal-child-hung``, the process of the uid left alive.
"""
from __future__ import annotations

import signal
from dataclasses import replace

import pytest

from levain.slot import proc as P
from levain.slot.proc import RUN, STOP, Proc

SLOT = 5001


class Loopers:
    """A fake host on a fake clock: ``n`` processes of ``uid``, each looping ``kill(-1, SIGCONT)``. A SIGSTOP that
    lands on one while another is still running is undone by that peer before the next scan; SIGKILL ends one for
    good (nothing the uid does undoes it). ``signal_uid`` delivers one process at a time, as ``kill(-1)`` does."""

    def __init__(self, uid: int, n: int = 2) -> None:
        self.uid, self.t = uid, 100.0
        self.procs = {3000 + i: Proc(3000 + i, (uid,) * 3, 7_000_000 + i, RUN) for i in range(n)}
        self.signals: list[int] = []

    def listing(self) -> list[Proc]:
        return list(self.procs.values())

    def probe(self, pid: int) -> Proc | None:
        return self.procs.get(pid)

    def pin(self, pid: int) -> tuple[int, int]:
        if pid not in self.procs:
            raise ProcessLookupError(pid)
        return pid, self.procs[pid].start

    def send(self, handle: tuple[int, int], sig: int) -> None:
        pid, start = handle
        p = self.procs.get(pid)
        if p is None or p.start != start:
            raise ProcessLookupError(pid)
        self.signals.append(sig)
        if sig == signal.SIGKILL:
            del self.procs[pid]
        elif sig == signal.SIGSTOP and p.state == RUN:
            peer_runs = any(q.state == RUN for q in self.procs.values() if q.pid != pid)
            if not peer_runs:
                self.procs[pid] = replace(p, state=STOP)

    def signal_uid(self, uid: int, sig: int, timeout: float) -> bool:
        self.t += 0.002
        for pid in list(self.procs):
            if pid in self.procs:
                self.send((pid, self.procs[pid].start), sig)
        return True

    def sleep(self, s: float) -> None:
        self.t += s

    def seams(self, darwin: bool) -> P.ProcSeams:
        return P.ProcSeams(listing=self.listing, probe=self.probe, pin=self.pin, send=self.send,
                           signal_uid=self.signal_uid if darwin else None, clock=lambda: self.t, sleep=self.sleep,
                           self_uids=lambda: (501, 501), self_pid=lambda: 1,
                           domain_out=(lambda uid, timeout: 0) if darwin else None)


@pytest.mark.parametrize("darwin", [True, False], ids=["macos-bootout", "linux-shape"])
def test_sigcont_loopers_cannot_hold_off_the_kill(darwin):
    host = Loopers(SLOT)
    t0 = host.t
    res, _ = P.end_domain(SLOT, P.V12K_SECONDS, seams=host.seams(darwin))
    assert host.procs == {}, f"{len(host.procs)} looper(s) outlived end_domain ({res.name})"
    assert res.proven, (res.name, res.detail)
    assert signal.SIGKILL in host.signals
    assert host.t - t0 <= P.V12K_SECONDS


def test_a_hung_signalling_child_costs_its_own_bound_and_the_sweep_goes_on():
    host = Loopers(SLOT, n=1)
    forks: list[int] = []
    killed: set[int] = set()
    pending: dict[int, int] = {}

    def fork() -> int:
        pid = 8000 + len(forks)
        forks.append(pid)
        return pid

    def waitpid(pid: int, flags: int) -> tuple[int, int]:
        host.t += 0.001
        if pid == forks[0]:                                      # the first child hangs until SIGKILLed
            return (pid, signal.SIGKILL) if pid in killed else (0, 0)
        if pid in pending:                                       # every other child signals the uid, then exits 0
            sig = pending.pop(pid)
            for q in list(host.procs):
                host.send((q, host.procs[q].start), sig)
        return pid, 0

    def signal_uid(uid: int, sig: int, timeout: float) -> bool:
        def child_fork() -> int:
            pid = fork()
            pending[pid] = sig
            return pid
        return P.signal_as_uid(uid, sig, timeout, fork=child_fork, waitpid=waitpid,
                               kill=lambda pid, s: killed.add(pid), clock=lambda: host.t, sleep=host.sleep)

    seams = host.seams(darwin=True)
    seams.signal_uid = signal_uid
    t0 = host.t
    res, _ = P.end_domain(SLOT, P.V12K_SECONDS, seams=seams)
    assert host.procs == {}, f"the process of the uid outlived end_domain ({res.name})"
    assert res.proven, (res.name, res.detail)
    assert forks[0] in killed and len(forks) > 1                # the hung child was ended and the sweep went on
    assert host.t - t0 <= P.V12K_SECONDS
