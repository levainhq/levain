"""R1a (reaper DESIGN §3.1, §9): macOS END's unit of kill and the spawn fence. Two tests, each from a failure
reproduced on W-T's tip 077cd624 (raw: levain-memory 1021-29/r1a/):

1. A fake macOS host whose launchd brings a KeepAlive agent back ``max(0, 10 s - age)`` after its kill until the uid's
   domain is booted out (R0 d1b), and answers 124 to a bootout of a domain an earlier bootout left re-instantiated
   and empty (R0 d3). END's kill and proof passed on it and an agent of the uid was back within 15 s (Z5d for a
   young agent, Z5b's respawn for an old one).
2. ``start`` abandoning a vendor whose ACTIVE CAS was refused, while a teardown of the same session runs: the
   teardown's END proved and SCRUB released the slot in the middle of the abandon's kill (RH1).
"""
from __future__ import annotations

import signal
from dataclasses import replace

import pytest

from levain.slot import proc as P
from levain.slot import records as R
from levain.slot.proc import RUN, STOP, Proc
from tests.test_slot_s2f3_start import _rendered, _world
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

SLOT = 5001


class Launchd:
    """A fake macOS host: a process table on a fake clock, and launchd holding ``uid``'s Background domain with one
    KeepAlive agent started ``age`` seconds ago. A SIGKILLed agent comes back ``max(0.05, 10 s - age)`` later while
    the domain is loaded (R0 d1b: the delay is ``max(0, 10 s - age)``; its "at once" rows were 11-96 ms). A bootout
    of a loaded domain unloads it, its agents with it (rc 0); a bootout of an unloaded one re-instantiates it EMPTY
    (rc 0, starting nothing), and a bootout within 40 ms of that answers 124 (R0 d3: longest window 42.1 ms)."""

    def __init__(self, uid: int, age: float) -> None:
        self.uid, self.t = uid, 100.0
        self.procs: dict[int, Proc] = {}
        self.started: dict[int, float] = {}
        self.next_pid = 2900
        self.loaded = True
        self.due: list[float] = []
        self.empty_until: float | None = None
        self.answers: list[int] = []
        self._agent(self.t - age)

    def _agent(self, start: float) -> None:
        pid, self.next_pid = self.next_pid, self.next_pid + 1
        self.procs[pid] = Proc(pid, (self.uid,) * 3, int(start * 1_000_000), RUN)
        self.started[pid] = start

    def _tick(self) -> None:
        for when in sorted(d for d in self.due if d <= self.t):
            self.due.remove(when)
            if self.loaded:
                self._agent(when)

    def listing(self) -> list[Proc]:
        self._tick()
        return list(self.procs.values())

    def probe(self, pid: int) -> Proc | None:
        self._tick()
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
        if sig == signal.SIGSTOP and p.state == RUN:
            self.procs[pid] = replace(p, state=STOP)
        elif sig == signal.SIGKILL:
            del self.procs[pid]
            if self.loaded:
                self.due.append(self.t + max(0.05, 10.0 - (self.t - self.started[pid])))

    def sleep(self, s: float) -> None:
        self.t += s

    def bootout(self, uid: int, timeout: float) -> int:
        self.t += 0.006
        if self.loaded:
            self.loaded = False
            self.due.clear()
            for pid in [q for q in self.procs if q in self.started]:
                del self.procs[pid]
            rc = 0
        elif self.empty_until is not None and self.t < self.empty_until:
            rc = 124
        else:
            self.empty_until = self.t + 0.040
            rc = 0
        self.answers.append(rc)
        return rc

    def seams(self, **kw) -> P.ProcSeams:
        return P.ProcSeams(listing=self.listing, probe=self.probe, pin=self.pin, send=self.send,
                           clock=lambda: self.t, sleep=self.sleep, self_uids=lambda: (501, 501), self_pid=lambda: 1,
                           domain_out=self.bootout, **kw)

    def of_uid(self) -> list[int]:
        return [p.pid for p in self.listing() if self.uid in p.uids]


@pytest.mark.parametrize("age", [2.0, 20.0], ids=["z5d-young-agent", "z5b-old-agent"])
def test_end_kill_proves_only_after_the_bootout_and_nothing_comes_back(age):
    ld = Launchd(SLOT, age)
    freeze, kill, no_process = P.end_seams("darwin", None, ld.seams())
    assert freeze(SLOT, P.V12_SECONDS)
    assert kill(SLOT, P.V12_SECONDS) and no_process(SLOT)          # END's kill step, then its proof
    proved = ld.t
    while ld.t < proved + 15.0:                                    # R1a M2: the uid sampled for 15 s after the proof
        ld.sleep(0.05)
        assert ld.of_uid() == [], f"an agent of the uid is back {ld.t - proved:.2f} s after END's proof"
    assert ld.answers == [0]                                       # one bootout, accepted, never re-issued

    # SCRUB's call right after: the domain is gone, so its bootout re-instantiates an empty one (rc 0). A third
    # bootout inside that teardown (a HELD retry, an orphan revert) answers 124: retried, never read as done.
    res, _ = P.end_domain(SLOT, P.V12K_SECONDS, seams=ld.seams())
    assert res.proven and ld.answers == [0, 0]
    ld.bootout(SLOT, 1.0)                                          # a fresh empty re-instantiation, 40 ms window
    res, _ = P.end_domain(SLOT, P.V12K_SECONDS, seams=ld.seams())
    assert res.proven and ld.answers[3:-1] and set(ld.answers[3:-1]) == {124} and ld.answers[-1] == 0

    # RV5: a child reaped before the call raised is still in ``reaped``, so the caller never signals that pid.
    gone: list[int] = []

    def reap(pid: int) -> bool:
        if gone:
            raise RuntimeError("the reap's bookkeeping failed")
        gone.append(pid)
        return True
    res, reaped = P.end_domain(SLOT, P.V12K_SECONDS, children=(7001, 7002), seams=ld.seams(reap=reap))
    assert not res.proven and res.name == "raised:RuntimeError" and reaped == frozenset({7001})


def test_end_waits_for_a_start_that_is_abandoning_and_releases_nothing_under_it(tmp_path):
    w, la, table = _world(tmp_path)
    s = _rendered(w)
    w.seams.end_overrides = {**w.seams.end_overrides, "clock": table.clock, "sleep": table.clock.sleep}
    during: list = []
    armed = [True]
    real_write = table.write

    def write(path, text):
        if path.endswith("/cgroup.kill") and armed[0]:            # start's abandon is killing the domain
            armed[0] = False
            during.append(w.main(["teardown", s])[1])
            during.append(w.slot_state(w.alloc(s).slot))
        real_write(path, text)
    table.write = write
    w.seams.proc_seams = table.seams()

    def eof(step):
        if step == "active":
            w.control.lease_eof(w.conn.lease)
            w.control.join_eof(10)
    w.on_step = eof
    assert w.main(["start", s])[1]["refused"] in ("lease-dead", "stale-generation", "from-mismatch")
    answer, slot_then = during
    assert answer.get("refused") == "end-busy", answer          # END waited on start's lock, then refused
    assert slot_then.state == "leased" and slot_then.session == s
    assert w.state(s).state == R.REVOKED                          # the record unchanged by the busy END
    (v,) = la.of("vendor")
    assert la.killed == [v["pid"]]

    w.on_step = lambda step: None
    assert w.main(["teardown", s])[1] == {"ok": True, "state": R.RETAINED}   # once start let go
    assert w.slot_state(w.alloc(s).slot).state == "reusable"
