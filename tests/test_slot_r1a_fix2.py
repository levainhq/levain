"""R1a fix round 2 (reaper DESIGN §3.1, head rulings G1-G7). Each test from a failure reproduced on fix 1's tip
ce447bf7 (raw: levain-memory 1021-32/r1a-fix2/):

1. G1: the caller's child exits between ``end_domain``'s non-blocking reap and its kill seam, whose own wait reaps
   it. The domain then failed (a bootout launchd did not accept) and ``reaped`` did not name the pid, so ``start``'s
   abandon would SIGKILL it as root after it may have been reused.
"""
from __future__ import annotations

from levain.slot import proc as P
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
