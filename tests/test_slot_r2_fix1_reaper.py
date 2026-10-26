"""R2 fix round 1 (reaper L3 r1): one item that fails never ends the pass, and the revoker never records an outcome
that does not prove the front is gone. On lane F2's ``World`` and the fake process table of the start tests."""
from __future__ import annotations

import os

from levain.slot import Refused
from levain.slot import start as SS
from levain.slot.ids import SlotId
from tests.test_slot_r2_reaper import _lease_to, _reaper
from tests.test_slot_s2f3_start import E0, _started, _world
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

E1 = SlotId("egress", 1)


def _clock(w):
    """The revoker's monotonic clock, set by hand: a retry is due only once its delay has passed."""
    clk = [1000.0]
    w.seams.monotonic = lambda: clk[0]
    return clk


def test_one_unreadable_slot_state_does_not_starve_the_next_slots_orphan_revert(tmp_path):
    """e0's state file is corrupt and e1 is ``leased`` to a session with no alloc and no record (a ``prepare`` that
    died before ATTACH). Step 3a's snapshot reads e0 first: its failure is named, and e1 is still reverted."""
    w, la, table = _world(tmp_path)
    _lease_to(w, E1, "s-00000000000000bb")
    (E0.slot_dir(w.L) / "state").write_bytes(b'{"not": "a slot state"')

    report = _reaper(w).run_pass()

    after = w.slot_state(E1)
    assert (after.state, after.session) == ("reusable", None)
    assert any(x.startswith("orphan:e0:") for x in report["skipped"]), report["skipped"]


def test_revoker_retries_a_front_kill_that_errored_while_control_is_down(tmp_path, monkeypatch):
    """CONTROL down, an ACTIVE egress session: the first front kill fails before it proves anything (an OSError,
    as from the ``front-revoked`` marker's fsync). That outcome proves nothing, so the next wake tries again and
    ends the front."""
    from levain.slot import reaper as RP

    w, la, table = _world(tmp_path)
    s = _started(w, la, table)

    def down(path):
        raise Refused("control-down", str(path))
    w.seams.client_factory = down
    real = SS.revoke_front
    calls = []

    def flaky(ctx, a):
        calls.append(a.session)
        if len(calls) == 1:
            raise OSError(5, "Input/output error")
        return real(ctx, a)
    monkeypatch.setattr(SS, "revoke_front", flaky)

    clk = _clock(w)
    rv = RP.Revoker(w.seams)
    first = rv.wake()
    assert first["control"] == "down"
    assert first["outcomes"][s] == "error:OSError"
    assert rv.pending()

    clk[0] += RP.REVOKER_RETRY_S
    second = rv.wake()
    assert calls == [s, s]
    assert second["outcomes"][s] == "front-ended"
    assert not rv.pending()


def test_an_unreadable_alloc_keeps_its_pending_kill_and_the_fast_retry(tmp_path, monkeypatch):
    # Reproduced on a4d011b0: the unreadable alloc's lease was evicted from first_seen and pending() went false, so
    # the retry waited a V11 tick.
    """CONTROL down, an ACTIVE egress session whose first front kill errored. On the next wake its alloc cannot be
    read: the lease it already saw stays pending, and an alloc that failed on its first read is pending too."""
    from levain.slot import reaper as RP
    from levain.slot import runtime_helper as H

    w, la, table = _world(tmp_path)
    s = _started(w, la, table)

    def down(path):
        raise Refused("control-down", str(path))
    w.seams.client_factory = down
    real_kill, real_read = SS.revoke_front, H.Store.read
    kills, unreadable = [], {"n": 0}

    def flaky_kill(ctx, a):
        kills.append(a.session)
        if len(kills) == 1:
            raise OSError(5, "Input/output error")
        return real_kill(ctx, a)

    def flaky_read(self, parts, name, **kw):
        if tuple(parts) == ("alloc",) and unreadable["n"]:
            unreadable["n"] -= 1
            raise OSError(5, "Input/output error")
        return real_read(self, parts, name, **kw)
    monkeypatch.setattr(SS, "revoke_front", flaky_kill)
    monkeypatch.setattr(H.Store, "read", flaky_read)

    clk = _clock(w)
    rv = RP.Revoker(w.seams)
    unreadable["n"] = 1
    assert rv.wake()["outcomes"][s] == "alloc:OSError"
    assert rv.pending() and not kills
    clk[0] += RP.REVOKER_RETRY_S
    assert rv.wake()["outcomes"][s] == "error:OSError"
    unreadable["n"] = 1
    clk[0] += RP.REVOKER_RETRY_S
    assert rv.wake()["outcomes"][s] == "alloc:OSError"
    assert rv.pending() and len(rv.first_seen) == 1
    clk[0] += RP.REVOKER_RETRY_S
    assert rv.wake()["outcomes"][s] == "front-ended"
    assert kills == [s, s] and not rv.pending()


def test_an_unread_alloc_survives_a_slow_wake(tmp_path, monkeypatch):
    # Reproduced on 2ed56044: the slow wake's targets left out the unread session, its empty set replaced unread,
    # and pending() went false with the front never ended.
    """CONTROL down, the alloc unreadable on its first read; then CONTROL slow. The unread session is still a target
    of the slow wake: once its alloc reads, its front is ended."""
    from levain.slot import reaper as RP
    from levain.slot import runtime_helper as H

    w, la, table = _world(tmp_path)
    s = _started(w, la, table)

    class Slow:
        def call(self, op, **kw):
            raise Refused("control-down", "timed out")

        def close(self):
            pass

    def down(path):
        raise Refused("control-down", str(path))
    real_read = H.Store.read
    unreadable = {"n": 2}

    def flaky_read(self, parts, name, **kw):
        if tuple(parts) == ("alloc",) and unreadable["n"]:
            unreadable["n"] -= 1
            raise OSError(5, "Input/output error")
        return real_read(self, parts, name, **kw)
    monkeypatch.setattr(H.Store, "read", flaky_read)

    clk = _clock(w)
    rv = RP.Revoker(w.seams)
    w.seams.client_factory = down
    assert rv.wake()["outcomes"] == {s: "alloc:OSError"}
    w.seams.client_factory = lambda path: Slow()
    clk[0] += RP.REVOKER_RETRY_S
    out = rv.wake()
    assert (out["control"], out["outcomes"]) == ("slow", {s: "alloc:OSError"})
    assert rv.pending() and rv.unread == {s}
    clk[0] += 2 * RP.REVOKER_RETRY_S
    out = rv.wake()
    assert (out["control"], out["outcomes"]) == ("slow", {s: "front-ended"})
    assert not rv.pending()


def test_a_stuck_retry_and_a_raising_wake_back_off_to_the_tick(tmp_path, monkeypatch):
    # Reproduced on 2ed56044: over 600 s both woke 2400 times (every 0.25 s); the unreadable alloc logged 4801 lines
    # and the raising wake 2401.
    """CONTROL down and an alloc that never reads; separately, a wake that always raises. Over a fixed span the
    revoker's waits double from REVOKER_RETRY_S to the tick, and each logs only on a change."""
    from levain.slot import reaper as RP
    from levain.slot import runtime_helper as H
    from levain.slot.services import reaper as SR

    def run(rv, clk):
        waits, logs = [], []
        r, wfd = os.pipe()
        os.set_blocking(r, False)

        def fake_wait(fd, timeout):
            waits.append(timeout)
            clk[0] += timeout
        monkeypatch.setattr(SR, "_wait", fake_wait)
        rv.log = logs.append
        try:
            SR.revoker_loop(rv, r, wakes=17, tick_s=RP.V11_S, log=logs.append)
        finally:
            os.close(r)
            os.close(wfd)
        return waits, logs

    w, la, table = _world(tmp_path)
    s = _started(w, la, table)

    def down(path):
        raise Refused("control-down", str(path))
    w.seams.client_factory = down

    def unreadable(self, parts, name, **kw):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(H.Store, "read", unreadable)
    clk = _clock(w)
    rv = RP.Revoker(w.seams)
    doubling = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0] + [RP.V11_S] * 8
    waits, logs = run(rv, clk)
    assert waits == doubling
    assert sum("revoker: alloc" in x for x in logs) == 2                 # the first outcome, then the tick cap
    assert sum("revoker wake" in x for x in logs) == 1                   # every summary after the first is the same
    assert rv.pending() and rv.unread == {s}

    def boom():
        raise OSError(13, "Permission denied")
    rv = RP.Revoker(w.seams)
    rv.wake = boom
    waits, logs = run(rv, clk)
    assert waits == doubling
    assert sum("failed" in x for x in logs) == 1


def test_one_retry_per_session_backs_off_and_a_re_lease_is_tried_at_once(tmp_path, monkeypatch):
    # Reproduced on 86ef5b73: the lease's own retry stayed due once its alloc went unread, pinning retry_in at
    # REVOKER_RETRY_S (2400 wakes over 600 s); and the re-leased session's new revoke was not tried on the datagram
    # wake, waiting out the old unread backoff (59.9 s).
    """CONTROL down: the first kill errors, then the alloc never reads. The one retry doubles to the tick and the
    summary does not change between due wakes. Then the session is re-leased (its alloc rewritten, readable): the
    next wake, before the old retry is due, tries the new lease."""
    import dataclasses
    from pathlib import Path

    from levain.slot import reaper as RP
    from levain.slot import records as R
    from levain.slot import runtime_helper as H

    w, la, table = _world(tmp_path)
    s = _started(w, la, table)

    def down(path):
        raise Refused("control-down", str(path))
    w.seams.client_factory = down
    real_kill, real_read = SS.revoke_front, H.Store.read
    kills, unreadable = [], {"on": False}

    def flaky_kill(ctx, a):
        kills.append(a.lease_seq)
        if len(kills) == 1:
            raise OSError(5, "Input/output error")
        return real_kill(ctx, a)

    def flaky_read(self, parts, name, **kw):
        if tuple(parts) == ("alloc",) and unreadable["on"]:
            raise OSError(5, "Input/output error")
        return real_read(self, parts, name, **kw)
    monkeypatch.setattr(SS, "revoke_front", flaky_kill)
    monkeypatch.setattr(H.Store, "read", flaky_read)

    clk = _clock(w)
    rv = RP.Revoker(w.seams)
    assert rv.wake()["outcomes"] == {s: "error:OSError"}
    unreadable["on"] = True
    summaries = set()
    for _ in range(10):
        clk[0] += rv.retry_in()
        summaries.add(str(rv.wake()["outcomes"]))
        clk[0] += 0.1                                     # an early wake between retries
        summaries.add(str(rv.wake()["outcomes"]))
    assert list(rv.retry) == [s] and rv.retry[s].delay == RP.V11_S
    assert summaries == {str({s: "alloc:OSError"})}

    p = Path(w.seams.store) / "alloc" / f"{s}.json"
    a = R.parse_alloc(p.read_bytes())
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(R.alloc_bytes(dataclasses.replace(a, lease_seq=a.lease_seq + 1)))
    os.chmod(tmp, os.stat(p).st_mode & 0o7777)
    os.replace(tmp, p)
    unreadable["on"] = False
    clk[0] += 0.1
    rv.wake()
    assert kills == [a.lease_seq, a.lease_seq + 1]


def test_a_failing_wake_is_not_retried_at_the_datagram_rate(tmp_path, monkeypatch):
    # Reproduced on 86ef5b73: with a datagram every 0.01 s, a wake() that always raised was called 6001 times in 60 s.
    """While wakes fail, a readable wake pipe is drained but wake() is not called again before its backoff."""
    from levain.slot import reaper as RP
    from levain.slot.services import reaper as SR

    w, la, table = _world(tmp_path)
    clk = _clock(w)
    rv = RP.Revoker(w.seams)
    calls = []

    def boom():
        calls.append(clk[0])
        raise OSError(13, "Permission denied")
    rv.wake = boom
    r, wfd = os.pipe()
    os.set_blocking(r, False)

    def fake_wait(fd, timeout):                           # a datagram arrives 0.01 s into every wait
        os.write(wfd, b"x")
        clk[0] += min(max(timeout, 0.0), 0.01)
    monkeypatch.setattr(SR, "_wait", fake_wait)
    try:
        SR.revoker_loop(rv, r, wakes=6, tick_s=RP.V11_S, log=lambda m: None)
    finally:
        os.close(r)
        os.close(wfd)
    gaps = [round(b - a, 6) for a, b in zip(calls, calls[1:])]
    assert gaps == [0.25, 0.5, 1.0, 2.0, 4.0]
