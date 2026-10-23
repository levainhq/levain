"""T1 (C1_DESIGN r4 §2.4): the hold transition table, cell by cell, on a ledger whose writer is the test.

The ledger is attached with no writer thread (`attach(loop, _writer=False)`); the test plays the writer by draining
the queue and posting `_durable` / `_fault` itself. Each cell asserts the resulting state and outcome, the lines
queued, `charged`, and the durable future's outcome. Each completing transition from PENDING is also driven with the
future already cancelled: no `InvalidStateError`, and `_retrieve` returns without raising."""
from __future__ import annotations

import asyncio
import errno
import logging
import threading

import pytest

from levain.broker import ledger as L
from tests.ledger_support import AMOUNT, SCOPE, Owner, ledger_path, queued

P, R, S, RG, RD, ST = "PENDING", "RESERVED", "SENT", "RESOLVING", "RESOLVED", "STRANDED"
STATES = (P, R, S, RG, RD, ST)

DASH = ("dash",)            # cannot happen
STAYS = ("stays",)          # no change, nothing queued
NOOP = ("noop",)            # returns False, no change
MISUSE = ("raise", L.LedgerMisuse)
UNAVAIL = ("raise", L.LedgerUnavailable)
GATE = ("gate",)
RETURNS = ("returns",)
WAITS = ("waits",)


def to(state, outcome=None, lines=(), charged=AMOUNT):
    return ("to", L.HoldState[state], outcome, list(lines), charged)


REL = to(RG, ("released",), ["released"])
TABLE = {
    "reserved_durable": {P: to(R), R: DASH, S: DASH, RG: STAYS, RD: DASH, ST: STAYS},
    "fault": {P: to(ST), R: to(ST), S: to(ST), RG: to(ST, ("released",)), RD: STAYS, ST: STAYS},
    "send_gate": {P: UNAVAIL, R: GATE, S: UNAVAIL, RG: UNAVAIL, RD: UNAVAIL, ST: UNAVAIL},
    "mark_sent": {P: MISUSE, R: MISUSE, S: MISUSE, RG: MISUSE, RD: MISUSE, ST: MISUSE},   # R ungated; gated below
    "release": {P: REL, R: REL, S: MISUSE, RG: NOOP, RD: NOOP, ST: NOOP},
    "settle": {P: MISUSE, R: MISUSE, S: to(RG, ("settled", 100, 50), ["settled"]), RG: NOOP, RD: NOOP, ST: NOOP},
    "uncertain": {P: MISUSE, R: MISUSE, S: to(RG, ("uncertain", "upstream-reset"), ["uncertain"]), RG: NOOP,
                  RD: NOOP, ST: NOOP},
    "end": {P: REL, R: REL, S: to(RG, ("uncertain", "ended"), ["uncertain"]), RG: NOOP, RD: NOOP, ST: NOOP},
    "owner_ends": {P: REL, R: REL, S: to(RG, ("uncertain", "owner-ended"), ["uncertain"]), RG: STAYS, RD: STAYS,
                   ST: STAYS},
    "aclose": {P: to(RG, ("released",), ["released", "CLOSE"]), R: to(RG, ("released",), ["released", "CLOSE"]),
               S: to(RG, ("uncertain", "closed"), ["uncertain", "CLOSE"]), RG: ("stays", "CLOSE"),
               RD: ("stays", "CLOSE"), ST: ("stays", "CLOSE")},
    "resolution_durable": {P: DASH, R: DASH, S: DASH, RG: to(RD, ("released",), [], 0), RD: DASH, ST: STAYS},
    "durable": {P: WAITS, R: RETURNS, S: RETURNS, RG: UNAVAIL, RD: UNAVAIL, ST: UNAVAIL},
}
CELLS = [(st, ev) for ev in TABLE for st in STATES if TABLE[ev][st] != DASH]


async def _ledger(path):
    led = L.Ledger.open(path)
    led.attach(asyncio.get_running_loop(), _writer=False)
    return led


async def _build(led, state):
    """A hold in `state`, the queue drained. RESOLVING and STRANDED keep the `reserved` line in flight."""
    o = await Owner(led).start()
    h = o.hold
    assert [x["kind"] for x in queued(led)] == ["reserved"]
    res = None
    if state in (R, S):
        led._durable([h.seq])
    if state == S:
        h.send_gate()
        h.mark_sent()
    if state in (RG, RD, ST):
        h.release()
        res = queued(led)[0]["seq"]
    if state == RD:
        led._durable([h.seq, res])
    if state == ST:
        led._strand(h)
    assert h.state is L.HoldState[state]
    return o, h, res


async def _fire(led, o, h, res, event):
    if event == "reserved_durable":
        return led._durable([h.seq])
    if event == "fault":
        return led._fault(None, ("write", OSError(errno.ENOSPC, "No space left on device")))
    if event == "send_gate":
        return h.send_gate()
    if event == "mark_sent":
        return h.mark_sent()
    if event == "release":
        return h.release()
    if event == "settle":
        return h.settle(150)
    if event == "uncertain":
        return h.uncertain("upstream-reset")
    if event == "end":
        return h.end()
    if event == "owner_ends":
        await o.finish()
        return None
    if event == "aclose":
        await led.aclose(0.01)        # no writer: the deadline passes
        return None
    if event == "resolution_durable":
        return led._durable([h.seq, res])
    if event == "durable":
        t = asyncio.ensure_future(h.durable())
        for _ in range(5):
            await asyncio.sleep(0)
        if not t.done():
            t.cancel()
            await asyncio.wait([t])
            return "WAITS"
        return t.result()
    raise AssertionError(event)


async def _cell(path, state, event, cancel_fut=False):
    loop = asyncio.get_running_loop()
    errors = []
    loop.set_exception_handler(lambda lp, ctx: errors.append(ctx))
    led = await _ledger(path)
    o, h, res = await _build(led, state)
    before = (h.state, h.outcome, led.charged(SCOPE))
    if cancel_fut:
        h.fut.cancel()
    exp = TABLE[event][state]
    if exp[0] == "raise":
        with pytest.raises(exp[1]):
            await _fire(led, o, h, res, event)
        assert (h.state, h.outcome, led.charged(SCOPE)) == before
        assert queued(led) == []
    else:
        got = await _fire(led, o, h, res, event)
        q = [x if x == "CLOSE" else x["kind"] for x in queued(led)]
        if exp[0] in ("stays", "noop"):
            assert (h.state, h.outcome, led.charged(SCOPE)) == before
            assert q == list(exp[1:])
            if exp == NOOP:
                assert got is False
        elif exp == GATE:
            assert got is None and h.gated and h.state is L.RESERVED and q == []
        elif exp == RETURNS:
            assert got is None and h.state is before[0] and q == []
        elif exp == WAITS:
            assert got == "WAITS" and h.state is L.PENDING and not h.fut.done() and q == []
        else:
            _, st, outcome, lines, charged = exp
            assert h.state is st
            assert h.outcome == outcome
            assert q == lines
            assert led.charged(SCOPE) == charged
            if got is not None:
                assert got is True
            assert h.fut.done()
            if not h.fut.cancelled():
                if st is L.RESERVED:
                    assert h.fut.result() is None
                elif state == P:
                    assert isinstance(h.fut.exception(), L.LedgerUnavailable)
    for _ in range(3):
        await asyncio.sleep(0)
    if o.task is not None and not o.task.done():
        await o.finish()
    assert errors == [], errors


@pytest.mark.parametrize("state,event", CELLS, ids=["%s-%s" % c for c in CELLS])
def test_ledger_transition_table(tmp_path, state, event):
    asyncio.run(_cell(ledger_path(tmp_path), state, event))


PRECANCEL = ["reserved_durable", "fault", "release", "end", "owner_ends", "aclose"]


@pytest.mark.parametrize("event", PRECANCEL)
def test_completing_transitions_with_the_future_already_cancelled(tmp_path, event):
    """Nothing cancels `hold.fut` today; the guards make every completion and `_retrieve` independent of that."""
    asyncio.run(_cell(ledger_path(tmp_path), P, event, cancel_fut=True))


def test_mark_sent_after_a_passing_gate_is_sent(tmp_path):
    async def main():
        led = await _ledger(ledger_path(tmp_path))
        o, h, _ = await _build(led, R)
        with pytest.raises(L.LedgerMisuse):
            h.mark_sent()                    # no gate yet
        h.send_gate()
        h.mark_sent()
        assert h.state is L.SENT and queued(led) == []
        await o.finish()
        assert h.outcome == ("uncertain", "owner-ended")
    asyncio.run(main())


@pytest.mark.parametrize("why", ["unhealthy", "closing"])
def test_send_gate_on_reserved_refuses_when_unhealthy_or_closing(tmp_path, why):
    """The RESERVED/send_gate cell's `else unavail`: the check reads the loop-owned flags, not only the state."""
    async def main():
        led = await _ledger(ledger_path(tmp_path))
        o, h, _ = await _build(led, R)
        if why == "unhealthy":
            led._healthy = False             # the flag alone (in real use `_fault` also strands the hold)
        else:
            led._closing = True
        with pytest.raises(L.LedgerUnavailable):
            h.send_gate()
        assert not h.gated and h.state is L.RESERVED
        led._healthy, led._closing = True, False
        await o.finish()
    asyncio.run(main())


def test_settled_overrun_and_uncertain_accounting(tmp_path):
    """SETTLED(cost) books min(cost, amount) as settled and the excess as overrun; charged counts both. UNCERTAIN is
    counted at the full amount. Nothing leaves outstanding before its line is durable."""
    async def main():
        led = await _ledger(ledger_path(tmp_path))
        a = await _build(led, S)
        b = await _build(led, S)
        c = await _build(led, S)
        a[1].settle(150)
        b[1].settle(40)
        c[1].uncertain("upstream-reset")
        lines = queued(led)
        assert [x["kind"] for x in lines] == ["settled", "settled", "uncertain"]
        assert (lines[0]["settled"], lines[0]["overrun"]) == (100, 50)
        assert (lines[1]["settled"], lines[1]["overrun"]) == (40, 0)
        assert led.charged(SCOPE) == 300
        led._durable([x["seq"] for x in lines])
        t = led.totals(SCOPE)
        assert (t.settled, t.overrun, t.uncertain_other, t.uncertain_restart, t.outstanding) == (140, 50, 100, 0, 0)
        assert led.charged(SCOPE) == t.charged == 290
        for x in (a, b, c):
            await x[0].finish()
        assert queued(led) == []                  # every owner callback was a no-op
    asyncio.run(main())


def test_a_failing_durable_step_strands_only_that_hold(tmp_path, caplog):
    """Ruling 5 / Q11: one hold's failing step strands THAT hold; the ledger stays healthy."""
    async def main():
        led = await _ledger(ledger_path(tmp_path))
        a = await _build(led, P)
        b = await _build(led, P)
        real = led._apply_durable

        def boom(hold, kind):
            if hold is a[1]:
                raise RuntimeError("a bug in one step")
            real(hold, kind)
        led._apply_durable = boom
        with caplog.at_level(logging.WARNING, logger="levain.broker"):
            led._durable([a[1].seq, b[1].seq])
        assert a[1].state is L.STRANDED and b[1].state is L.RESERVED
        assert led.healthy and led.charged(SCOPE) == 200
        assert isinstance(a[1].fut.exception(), L.LedgerUnavailable)
        assert any("durable step failed" in r.getMessage() for r in caplog.records)
        led._apply_durable = real
        await a[0].finish()
        await b[0].finish()
        assert [x["kind"] for x in queued(led)] == ["released"]     # only b's: a is stranded
    asyncio.run(main())


def test_a_failing_owner_callback_strands_only_that_hold(tmp_path, caplog):
    async def main():
        led = await _ledger(ledger_path(tmp_path))
        a = await _build(led, R)
        b = await _build(led, R)
        real = led._end

        def boom(hold, reason):
            if hold is a[1]:
                raise RuntimeError("a bug in one callback")
            return real(hold, reason)
        led._end = boom
        with caplog.at_level(logging.WARNING, logger="levain.broker"):
            await a[0].finish()
            await b[0].finish()
        assert a[1].state is L.STRANDED and b[1].state is L.RESOLVING and led.healthy
        assert any("owner callback failed" in r.getMessage() for r in caplog.records)
    asyncio.run(main())


def test_hold_methods_off_the_loop_thread_are_misuse(tmp_path):
    async def main():
        led = await _ledger(ledger_path(tmp_path))
        o, h, _ = await _build(led, R)
        seen = []

        def other():
            for fn in (h.send_gate, h.end, h.release, lambda: led.charged(SCOPE)):
                try:
                    fn()
                    seen.append("ran")
                except L.LedgerMisuse:
                    seen.append("misuse")
        th = threading.Thread(target=other)
        th.start()
        th.join()
        assert seen == ["misuse"] * 4 and h.state is L.RESERVED and queued(led) == []
        await o.finish()
    asyncio.run(main())
