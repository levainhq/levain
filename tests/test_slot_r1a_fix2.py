"""R1a fix round 2 (reaper DESIGN §3.1, head rulings G1-G7). Each test from a failure reproduced on fix 1's tip
ce447bf7 (raw: levain-memory 1021-32/r1a-fix2/):

1. G1: the caller's child exits between ``end_domain``'s non-blocking reap and its kill seam, whose own wait reaps
   it. The domain then failed (a bootout launchd did not accept) and ``reaped`` did not name the pid, so ``start``'s
   abandon would SIGKILL it as root after it may have been reused.
2. G2: a child that will not die after its SIGKILL (uninterruptible sleep on a wedged volume). ``reap_within``, past
   its deadline, waited on it with a blocking ``waitpid(pid, 0)``: the clone's bound held START under the session
   lock forever.
3. G2, fix round 3 (reproduced on fix 2's tip 58f20980, raw: levain-memory 1021-35/r1a-fix3/): the same child as
   END's or stage's reader, killed on a deadline (``manifest-time-cap``, any refusal). Its ``wait()`` was a blocking
   ``waitpid(pid, 0)``, so END's budget held nothing.
4. G2, fix round 4 (reproduced on fix 3's tip 63af8946, raw: levain-memory 1021-35/r1a-fix4/): the same reader on
   the clean path, after root read its end mark (its ``close()`` hangs on a wedged volume). Its ``wait()`` was still a
   blocking ``waitpid(pid, 0)``: the clean path held END and staging past their deadline.
"""
from __future__ import annotations

import os
import signal
import time

import pytest

from levain.slot import Refused
from levain.slot import end as E
from levain.slot import proc as P
from levain.slot import runtime_helper as H
from levain.slot import stage as S
from tests.test_slot_r1a_fix1 import Loopers

SLOT = 5001
CHILD = 7001


def test_a_child_the_kill_seam_reaped_is_reported_reaped_when_the_domain_fails():
    host = Loopers(SLOT)
    seams = host.seams(darwin=True)
    seams.domain_out = lambda uid, timeout: 5                     # launchd does not accept the bootout
    gone: list[int] = []

    def kill_child(pid: int) -> bool:                             # the child exited after the reap: this wait took it
        gone.append(pid)
        return True
    seams.reap = lambda pid: False
    seams.kill_child = kill_child
    res, reaped = P.end_domain(SLOT, P.V12K_SECONDS, children=(CHILD,), seams=seams)
    assert gone == [CHILD]
    assert not res.proven and res.name == "domain-not-accepted:5"
    assert CHILD in reaped, "a pid the kill seam reaped is missing from reaped: the caller would signal it as root"


def test_a_child_that_will_not_die_cannot_hold_the_bound(monkeypatch):
    t = [100.0]
    killed: list[int] = []

    def waitpid(pid: int, flags: int) -> tuple[int, int]:
        t[0] += 0.001
        if not flags & os.WNOHANG:
            raise AssertionError("a blocking waitpid on a child that will not die: the caller hangs forever")
        return 0, 0

    def kill(pid: int, sig: int) -> None:
        assert sig == signal.SIGKILL
        killed.append(pid)

    def sleep(s: float) -> None:
        t[0] += s
    monkeypatch.setattr(os, "waitpid", waitpid)
    monkeypatch.setattr(os, "kill", kill)
    try:
        with pytest.raises(Refused) as e:
            H.reap_within(CHILD, 2.0, monotonic=lambda: t[0], sleep=sleep, refusal="child-timeout")
        assert e.value.name == "child-timeout" and killed == [CHILD]
        assert t[0] - 100.0 <= 2.0 + P.REAP_GRACE_S + 0.1
        assert CHILD in P._LEFTOVER                               # reaped later, by reap_leftovers
    finally:
        if CHILD in P._LEFTOVER:
            P._LEFTOVER.remove(CHILD)


@pytest.mark.parametrize("path", ["killed", "clean"])
@pytest.mark.parametrize("cls", [S._Forked, E._Forked], ids=["stage", "end"])
def test_a_killed_reader_that_will_not_die_cannot_hold_the_budget(monkeypatch, cls, path):
    killed: list[int] = []

    def waitpid(pid: int, flags: int) -> tuple[int, int]:
        if not flags & os.WNOHANG:
            raise AssertionError(f"a blocking waitpid on a {path} reader that will not die: END hangs forever")
        return 0, 0

    def kill(pid: int, sig: int) -> None:
        assert sig == signal.SIGKILL
        killed.append(pid)
    monkeypatch.setattr(os, "waitpid", waitpid)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(P, "_LEFTOVER", set())
    reader = cls(CHILD)
    left = 0.0
    t0 = time.monotonic()
    if path == "killed":
        reader.kill()                                             # the deadline path: kill, then the finally's wait
    else:                                                         # the end mark was read; the reader hangs in close()
        left = 0.2
        reader.until, reader.clock = t0 + left, time.monotonic
    if cls is S._Forked and path == "clean":
        with pytest.raises(Refused) as e:                         # stage: a failed reader, never a success
            reader.wait()
        assert e.value.name == "staging-reader-failed"
        code = None
    else:
        code = reader.wait()
    assert time.monotonic() - t0 <= left + P.REAP_GRACE_S + 0.5
    assert CHILD in P._LEFTOVER and set(killed) == {CHILD}
    assert code is None                                           # END's code: the reader failed
    assert reader.wait() is None and P._LEFTOVER == {CHILD}       # once: the leftover set owns it now
