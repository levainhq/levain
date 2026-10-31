"""The reservation ledger with its real writer thread (C1_DESIGN r4 §2.3-§2.7): the owner backstop (T3), writer errors
and UNHEALTHY (T5, T6), the synchronous admit (T11), no credit before durable (T14), close (T15, T23 in-process),
opaque meta (T18), per-route scopes at the ledger (T21's ledger half), the fault window (T22) and the stall rule
(T24). Flushes and writes are injected at the module's `_flush` / `_os_write` boundary, after `open`."""
from __future__ import annotations

import asyncio
import errno
import json
import logging
import threading
import time

import pytest

from levain.broker import creds
from levain.broker import ledger as L
from tests._waits import scaled
from tests.ledger_support import (AMOUNT, CAP, SCOPE, FlushCtl, Owner, file_lines, kinds, ledger_path, until,
                                  wait_thread)


async def _opened(path, monkeypatch=None, ctl=None):
    led = L.Ledger.open(path)
    if ctl is not None:
        monkeypatch.setattr(L, "_flush", ctl)
    led.attach(asyncio.get_running_loop())
    return led


def _errors(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR and "ledger fault" in r.getMessage()]


# ---- T3 -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("when", ["before_flush_opens", "after_flush_before_resume"])
def test_owner_callback_resolves_cancelled_durable_wait(tmp_path, monkeypatch, when):
    """The owner is cancelled inside `durable()`; nothing in the task resolves the hold, the done-callback does."""
    path = ledger_path(tmp_path)
    ctl = FlushCtl()
    ctl.gate.clear()

    async def main():
        led = await _opened(path, monkeypatch, ctl)
        seen = []
        real_end = led._end

        def end(hold, reason):
            seen.append((reason, hold.state))
            return real_end(hold, reason)
        led._end = end
        o = await Owner(led, then_durable=True).start()
        h = o.hold
        await until(ctl.entered.is_set)              # the writer is inside the `reserved` line's flush
        if when == "after_flush_before_resume":
            ctl.gate.set()
            wait_thread(lambda: ctl.returned >= 1)  # flushed, `_durable` posted; the loop has run nothing since
            ctl.gate.clear()                         # the `released` line's flush will wait
            time.sleep(0.02)
        o.task.cancel()
        await asyncio.wait([o.task])
        await until(lambda: h.state is L.RESOLVING)
        expect = L.PENDING if when == "before_flush_opens" else L.RESERVED
        assert seen == [("owner-ended", expect)]
        assert led.charged(SCOPE) == AMOUNT          # still held: the `released` line is not durable
        assert not h.fut.cancelled()                 # the shield kept the cancel off the durable future
        ctl.gate.set()
        await until(lambda: h.state is L.RESOLVED)
        assert led.charged(SCOPE) == 0
        await led.aclose(2.0)
        assert kinds(path)[-2:] == ["reserved", "released"]
    asyncio.run(main())


# ---- T5 / T6 ------------------------------------------------------------------------------------------------------

class WriteCtl:
    """Replaces `ledger._os_write`: call `fail_at` (1-based) raises ENOSPC; with `short`, that call first writes half
    the bytes (a short write) and the retry raises."""

    def __init__(self, fail_at, short=False):
        self.fail_at, self.short = fail_at, short
        self.calls = 0
        self.real = L._os_write

    def __call__(self, fd, data):
        self.calls += 1
        if self.short and self.calls == self.fail_at:
            return self.real(fd, bytes(data)[: max(1, len(data) // 2)])
        if self.calls == self.fail_at + (1 if self.short else 0):
            raise OSError(errno.ENOSPC, "No space left on device")
        return self.real(fd, data)


@pytest.mark.parametrize("where", ["reserved", "resolution"])
@pytest.mark.parametrize("how", ["write", "short_write", "flush"])
def test_unhealthy_on_writer_error(tmp_path, monkeypatch, caplog, where, how):
    path = ledger_path(tmp_path)
    ctl = FlushCtl()
    batch = 1 if where == "reserved" else 2                  # the failing batch, counted from attach
    if how == "flush":
        ctl.errors = [None] * (batch - 1) + [OSError(errno.EIO, "Input/output error")]

    async def main():
        led = await _opened(path, monkeypatch, ctl)
        if how != "flush":
            monkeypatch.setattr(L, "_os_write", WriteCtl(batch, short=(how == "short_write")))
        with caplog.at_level(logging.ERROR, logger="levain.broker"):
            o = await Owner(led).start()
            h = o.hold
            if where == "resolution":
                await h.durable()
                h.release()
            await until(lambda: not led.healthy)
            reason = "flush:EIO" if how == "flush" else "write:ENOSPC"
            assert led.fault_reason == reason
            assert h.state is L.STRANDED and led.charged(SCOPE) == AMOUNT
            with pytest.raises(L.LedgerUnavailable):
                led.admit(SCOPE, CAP, lambda r: 1, "r-a")
            with pytest.raises(L.LedgerUnavailable):
                h.send_gate()
            assert h.release() is False and h.end() is False
            led._fault(None, ("flush", OSError(errno.EIO, "again")))      # a second fault changes nothing
            assert led.fault_reason == reason
            errs = _errors(caplog)
            assert len(errs) == 1 and ("ledger fault: " + reason.replace(":", " ")) in errs[0]
            await o.finish()
            assert led.charged(SCOPE) == AMOUNT                          # never decreases again
            await led.aclose(2.0)
    asyncio.run(main())
    monkeypatch.undo()
    led2 = L.Ledger.open(path)
    k = kinds(path)
    # what replay finds: an unwritten or torn line grants and books nothing; a written but unflushed line counts
    if where == "reserved":
        expect = AMOUNT if how == "flush" else 0
    else:
        expect = 0 if how == "flush" else AMOUNT
    assert led2.charged(SCOPE) == expect
    if where == "resolution" and how == "flush":
        assert "released" in k and "uncertain" not in k            # complement F5: released, no restart line
    led2._fd >= 0 and asyncio.run(led2.aclose(0))


def test_no_write_after_first_error(tmp_path, monkeypatch):
    """T6: after the first error the fd records no further write or flush, for lines queued before AND after it."""
    path = ledger_path(tmp_path)
    ctl = FlushCtl()
    ctl.gate.clear()
    ctl.errors = [OSError(errno.EIO, "Input/output error")]
    writes = []

    async def main():
        led = await _opened(path, monkeypatch, ctl)
        real_write = L._os_write
        monkeypatch.setattr(L, "_os_write", lambda fd, d: (writes.append(len(d)), real_write(fd, d))[1])
        a = await Owner(led).start()
        await until(ctl.entered.is_set)              # batch B (a's line) is inside its flush
        b = await Owner(led).start()                 # queued before the error
        ctl.gate.set()
        wait_thread(lambda: ctl.calls == 1 and led._q.empty())   # B failed; b's line taken and discarded
        time.sleep(0.05)
        c = led.admit(SCOPE, CAP, lambda r: AMOUNT, "r-a")      # queued after the error, before `_fault` ran
        await until(lambda: not led.healthy)
        for x in (a, b):
            await x.finish()
        await led.aclose(2.0)
        assert c.state is L.STRANDED
        assert len(writes) == 1 and ctl.calls == 1
    asyncio.run(main())
    assert sum(1 for o in file_lines(path) if o["kind"] == "reserved") == 1    # a's (written, its flush failed)


# ---- T11 ----------------------------------------------------------------------------------------------------------

def test_admit_is_synchronous(tmp_path, monkeypatch):
    path = ledger_path(tmp_path)
    ctl = FlushCtl()
    ctl.gate.clear()

    async def main():
        led = await _opened(path, monkeypatch, ctl)
        seen, refused = [], []

        async def one():
            before = led.charged(SCOPE)
            try:
                led.admit(SCOPE, 1000, lambda r: 150, "r-a")
                seen.append(before)
            except L.Refused as e:
                refused.append(e.name)
        await asyncio.gather(*[one() for _ in range(20)])
        assert seen == [150 * k for k in range(1000 // 150)]       # each admit saw every earlier hold
        assert refused == ["budget-exhausted"] * (20 - 1000 // 150)
        assert led.charged(SCOPE) == 150 * (1000 // 150)
        ctl.gate.set()

        state = (led.charged(("session-route", "s1", "other")), led._next_seq, len(led._lines))
        sc = ("session-route", "s1", "other")

        async def coro_bound(r):
            return 1

        def calls_ledger(r):
            led.charged(SCOPE)
            return 1

        def swallows(r):
            try:
                led.charged(SCOPE)
            except L.LedgerMisuse:
                pass
            return 1
        for bad in (coro_bound, calls_ledger, swallows, lambda r: 0, lambda r: -1, lambda r: True,
                    lambda r: 1.0):
            with pytest.raises(L.LedgerMisuse):
                led.admit(sc, 1000, bad, "r-a")
        with pytest.raises(L.Refused) as ei:
            led.admit(sc, 1000, lambda r: r + 1, "r-a")
        assert ei.value.name == "budget-exhausted"

        class Clamp(Exception):
            pass

        def raises(r):
            raise Clamp()
        with pytest.raises(Clamp):
            led.admit(sc, 1000, raises, "r-a")
        assert (led.charged(sc), led._next_seq, len(led._lines)) == state
        assert not led._in_bound
        # a 64 KiB non-ASCII session: ledger-only coverage (a real exchange's session rides the 4096-byte H frame)
        big = ("session-route", "é中\U0001f600" * (64 * 1024 // 9), "r-a")
        h = led.admit(big, 1000, lambda r: 7, "r-a")
        await h.durable()
        h.send_gate()
        h.mark_sent()
        h.settle(7)
        await until(lambda: h.state is L.RESOLVED)
        await led.aclose(2.0)
        return big
    big = asyncio.run(main())
    led2 = L.Ledger.open(path)
    assert led2.charged(big) == 7
    asyncio.run(led2.aclose(0))


def test_admit_argument_shapes_are_misuse(tmp_path):
    async def main():
        led = await _opened(ledger_path(tmp_path))
        ok = lambda r: 1  # noqa: E731
        for scope in (["session-route", "s"], (), ("Session", "s"), ("session-route", ""), ("session-route", 1),
                      ("x" * 33,)):
            with pytest.raises(L.LedgerMisuse):
                led.admit(scope, 10, ok, "r")
        for cap in (-1, True, 1.0, "1"):
            with pytest.raises(L.LedgerMisuse):
                led.admit(SCOPE, cap, ok, "r")
        for pr in ("", "x" * 257, "a\nb", "é", 5):
            with pytest.raises(L.LedgerMisuse):
                led.admit(SCOPE, 10, ok, pr)
        for meta in ([], {"f": 1.5}, {1: 2}, {"n": float("nan")}, {"s": {1, 2}}):
            with pytest.raises(L.LedgerMisuse):
                led.admit(SCOPE, 10, ok, "r", meta)
        with pytest.raises(L.LedgerMisuse):
            led.admit(SCOPE, 10, "not callable", "r")
        assert led._next_seq == led._next_seq and not led._lines
        await led.aclose(1.0)
    asyncio.run(main())


def test_admit_outside_a_task_or_unattached_is_misuse(tmp_path):
    led = L.Ledger.open(ledger_path(tmp_path))
    with pytest.raises(L.LedgerMisuse):
        led.admit(SCOPE, 10, lambda r: 1, "r")            # not attached
    loop = asyncio.new_event_loop()
    try:
        led.attach(loop, _writer=False)
        with pytest.raises(L.LedgerMisuse):
            led.admit(SCOPE, 10, lambda r: 1, "r")        # no current task
        with pytest.raises(L.LedgerMisuse):
            led.attach(loop)
    finally:
        loop.close()


# ---- T14 ----------------------------------------------------------------------------------------------------------

def test_no_credit_before_durable(tmp_path, monkeypatch):
    path = ledger_path(tmp_path)
    ctl = FlushCtl()

    async def main():
        led = await _opened(path, monkeypatch, ctl)
        o = await Owner(led).start()
        h = o.hold
        await h.durable()
        ctl.gate.clear()
        h.release()
        await until(ctl.entered.is_set)
        await asyncio.sleep(0.05)
        assert h.state is L.RESOLVING and led.charged(SCOPE) == AMOUNT
        at = []
        real = led._durable

        def durable(seqs):
            at.append(led.charged(SCOPE))
            real(seqs)
            at.append(led.charged(SCOPE))
        led._durable = durable
        ctl.entered.clear()
        ctl.gate.set()
        await until(lambda: h.state is L.RESOLVED)
        assert at == [AMOUNT, 0]                        # it drops exactly at the callback
        await o.finish()
        await led.aclose(2.0)
    asyncio.run(main())


# ---- T15 ----------------------------------------------------------------------------------------------------------

def test_aclose_finalizes_live_holds(tmp_path, monkeypatch):
    path = ledger_path(tmp_path)
    ctl = FlushCtl()

    async def main():
        led = await _opened(path, monkeypatch, ctl)
        r = await Owner(led).start()
        s = await Owner(led).start()
        g = await Owner(led).start()
        await r.hold.durable()
        await s.hold.durable()
        await g.hold.durable()
        s.hold.send_gate()
        s.hold.mark_sent()
        ctl.gate.clear()
        p = await Owner(led).start()                   # PENDING: its line is behind a gated flush
        await until(ctl.entered.is_set)
        g.hold.release()                               # RESOLVING at aclose
        # a straggler: its hold RESERVED and its exchange still running (awaiting `straggle`) when aclose runs
        straggle = asyncio.Event()
        st_hold = []

        async def straggler():
            h = led.admit(SCOPE, CAP, lambda x: AMOUNT, "r-a")
            st_hold.append(h)
            await h.durable()
            await straggle.wait()
            return h
        st_task = asyncio.ensure_future(straggler())
        asyncio.get_running_loop().call_later(0.05, ctl.gate.set)
        await asyncio.sleep(0)
        # the straggler's hold is RESERVED (a state, not a flush count: how the writer batches lines is its own)
        await until(lambda: st_hold != [] and st_hold[0].state is L.RESERVED and p.hold.state is L.RESERVED)
        ctl.gate.clear()
        asyncio.get_running_loop().call_later(0.05, ctl.gate.set)
        await led.aclose(5.0)
        for x in (r, s, g, p):
            assert x.hold.state is L.RESOLVED, x.hold
        assert r.hold.outcome == p.hold.outcome == g.hold.outcome == ("released",)
        assert s.hold.outcome == ("uncertain", "closed")
        with pytest.raises(L.LedgerUnavailable):
            led.admit(SCOPE, CAP, lambda x: 1, "r-a")
        straggle.set()
        sh = await st_task
        assert sh.state is L.RESOLVED and sh.outcome == ("released",)
        with pytest.raises(L.LedgerUnavailable):
            sh.send_gate()
        with pytest.raises(L.LedgerUnavailable):
            await sh.durable()
        n = len(file_lines(path))
        for x in (r, s, g, p):
            await x.finish()
        await asyncio.sleep(0)
        assert len(file_lines(path)) == n               # every callback after close is a no-op
    asyncio.run(main())
    led2 = L.Ledger.open(path)
    lines = file_lines(path)
    assert [o for o in lines if o.get("reason") == "restart"] == []
    assert led2.charged(SCOPE) == AMOUNT                # s: uncertain {closed} at the full amount
    asyncio.run(led2.aclose(0))


def test_aclose_past_its_deadline_leaves_the_rest_to_replay(tmp_path, monkeypatch):
    path = ledger_path(tmp_path)
    ctl = FlushCtl()

    async def main():
        led = await _opened(path, monkeypatch, ctl)
        a = await Owner(led).start()
        await a.hold.durable()
        ctl.gate.clear()
        b = await Owner(led).start()
        await until(ctl.entered.is_set)                 # b's `reserved` batch is inside its flush
        t0 = time.monotonic()
        await led.aclose(0.1)
        assert time.monotonic() - t0 < 0.5
        ctl.gate.set()                                  # b's line lands; a's and b's resolutions are discarded
        await until(lambda: led._thread is not None and not led._thread.is_alive())
        for x in (a, b):
            await x.finish()
    asyncio.run(main())
    lines = file_lines(path)
    led2 = L.Ledger.open(path)
    restarts = [o["ref"] for o in file_lines(path)[len(lines):] if o.get("reason") == "restart"]
    assert len(restarts) == 2 and led2.charged(SCOPE) == 2 * AMOUNT
    asyncio.run(led2.aclose(0))


# ---- T23 (in-process) ---------------------------------------------------------------------------------------------

def test_close_protocol_fd_closed_only_on_the_writer(tmp_path, monkeypatch):
    path = ledger_path(tmp_path)
    ctl = FlushCtl()
    closes = []

    async def main():
        led = await _opened(path, monkeypatch, ctl)
        real = L._os_close
        monkeypatch.setattr(L, "_os_close", lambda fd: (closes.append((fd, threading.current_thread().name)),
                                                        real(fd))[1])
        o = await Owner(led).start()
        await o.hold.durable()
        ctl.gate.clear()
        o.hold.release()
        await until(ctl.entered.is_set)
        t0 = time.monotonic()
        await led.aclose(0.2)                           # the writer is blocked in a flush
        took = time.monotonic() - t0
        assert 0.15 < took < 0.2 + 0.25
        assert closes == []                             # aclose closed nothing
        ctl.gate.set()
        await until(lambda: closes != [])
        assert closes == [(led._fd, "levain-ledger-writer")]
        await o.finish()
    asyncio.run(main())
    asyncio.run(L.Ledger.open(path).aclose(0))         # the lock is free


# ---- T18 ----------------------------------------------------------------------------------------------------------

def test_meta_and_price_ref_opaque(tmp_path, monkeypatch):
    path = ledger_path(tmp_path)
    meta = {"price": 999999, "unit": "usd", "cost": 5, "nested": {"a": [1, "xé", None, True]}}
    pr = "anthropic:claude/x@2026-10 {not parsed}"

    async def main():
        led = await _opened(path)
        h = led.admit(SCOPE, CAP, lambda r: AMOUNT, pr, meta)
        meta["unit"] = "changed after admit"
        await h.durable()
        h.send_gate()
        h.mark_sent()
        h.settle(AMOUNT, {"usage": {"in": 3, "out": 4}, "price": 1})
        await until(lambda: h.state is L.RESOLVED)
        assert led.charged(SCOPE) == AMOUNT
        state = (led.charged(SCOPE), led._next_seq)
        edge = {"k": "x" * (L.META_MAX - len('{"k":""}'))}
        assert len(json.dumps(edge, separators=(",", ":"))) == L.META_MAX
        h2 = led.admit(SCOPE, CAP, lambda r: 1, "p" * L.PRICE_REF_MAX, edge)    # at the caps: accepted
        h2.release()
        state = (led.charged(SCOPE), led._next_seq)
        over = {"k": edge["k"] + "x"}
        with pytest.raises(L.LedgerMisuse):
            led.admit(SCOPE, CAP, lambda r: 1, "r", over)
        with pytest.raises(L.LedgerMisuse):
            led.admit(SCOPE, CAP, lambda r: 1, "p" * (L.PRICE_REF_MAX + 1))
        assert (led.charged(SCOPE), led._next_seq) == state
        await led.aclose(2.0)
    asyncio.run(main())
    led2 = L.Ledger.open(path)
    res = [o for o in file_lines(path) if o["kind"] == "reserved"]
    assert res[0]["price_ref"] == pr and res[0]["meta"]["unit"] == "usd" and res[0]["meta"]["nested"] == \
        {"a": [1, "xé", None, True]}
    st = [o for o in file_lines(path) if o["kind"] == "settled"][0]
    assert st["meta"] == {"usage": {"in": 3, "out": 4}, "price": 1}
    assert led2.charged(SCOPE) == AMOUNT
    asyncio.run(led2.aclose(0))


# ---- T21 (the ledger half: two route scopes, two caps) -------------------------------------------------------------

def test_route_caps_are_per_route(tmp_path):
    """codex r2's corrected sequence, at the ledger: the keys half (a base map of two routes) is session B's."""
    path = ledger_path(tmp_path)
    a, b = ("session-route", "s", "a"), ("session-route", "s", "b")

    async def main():
        led = await _opened(path)
        got = []
        for scope, cap, n in ((b, 1000, 900), (a, 100, 100), (a, 100, 1), (b, 1000, 100), (b, 1000, 1)):
            try:
                led.admit(scope, cap, lambda r, n=n: n if n <= r else r + 1, scope[2])
                got.append("ok")
            except L.Refused as e:
                got.append(e.name)
        assert got == ["ok", "ok", "budget-exhausted", "ok", "budget-exhausted"]
        assert led.charged(a) == 100 and led.charged(b) == 1000
        await led.aclose(2.0)
    asyncio.run(main())
    res = [(tuple(o["scope"]), o["cap"]) for o in file_lines(path) if o["kind"] == "reserved"]
    assert res == [(b, 1000), (a, 100), (b, 1000)]


# ---- T22 ----------------------------------------------------------------------------------------------------------

def test_fault_linearization(tmp_path, monkeypatch):
    """§2.6's error table: batch A (Z's release) flushes, batch B (V's reserved) fails; with both posts waiting on
    the loop, the window W holds an admit (U), a send (X) and a resolution (Y)."""
    path = ledger_path(tmp_path)
    ctl = FlushCtl()

    async def main():
        led = await _opened(path, monkeypatch, ctl)
        order = []
        rd, rf = led._durable, led._fault
        led._durable = lambda seqs: (order.append("durable"), rd(seqs))[1]
        led._fault = lambda seqs, c: (order.append("fault"), rf(seqs, c))[1]
        x, y, z = [await Owner(led).start() for _ in range(3)]
        for o in (x, y, z):
            await o.hold.durable()
        ctl.gate.clear()
        ctl.entered.clear()
        ctl.errors = [None, OSError(errno.EIO, "Input/output error")]
        order.clear()
        base = ctl.calls
        z.hold.release()                                # batch A
        await until(ctl.entered.is_set)
        v = await Owner(led).start()                    # batch B, queued while A flushes
        ctl.gate.set()
        wait_thread(lambda: ctl.calls == base + 2)             # B's flush was called (and raises)
        time.sleep(0.05)                                # its `_fault` is posted behind A's `_durable`
        assert led.healthy                              # W: the loop has applied neither
        u = led.admit(SCOPE, CAP, lambda r: AMOUNT, "r-a")       # W-admit
        x.hold.send_gate()                              # W-send
        x.hold.mark_sent()
        y.hold.end()                                    # W-resolve
        await until(lambda: not led.healthy)
        assert order == ["durable", "fault"]            # E0 before E3
        assert z.hold.state is L.RESOLVED               # an earlier durable resolution is never stranded
        for h in (x.hold, y.hold, v.hold, u):
            assert h.state is L.STRANDED
        assert led.charged(SCOPE) == 4 * AMOUNT         # W-resolve lowered nothing
        with pytest.raises(L.LedgerUnavailable):
            await u.durable()
        with pytest.raises(L.LedgerUnavailable):
            led.admit(SCOPE, CAP, lambda r: 1, "r-a")
        with pytest.raises(L.LedgerUnavailable):
            v.hold.send_gate()
        for o in (x, y, z, v):
            await o.finish()
        await led.aclose(2.0)
        return u.seq, x.hold.seq
    useq, xseq = asyncio.run(main())
    seqs = {o["seq"] for o in file_lines(path) if o["kind"] == "reserved"}
    assert useq not in seqs                             # W-admit's line never reached the file
    assert xseq in seqs                                 # W-send's hold had a flushed reserved line


# ---- T24 ----------------------------------------------------------------------------------------------------------

def test_durable_timeout_constant_is_keys_stall_bound():
    assert L.DURABLE_TIMEOUT_S == creds.LOCK_TIMEOUT_S


def test_hung_flush_marks_unhealthy(tmp_path, monkeypatch, caplog):
    path = ledger_path(tmp_path)
    ctl = FlushCtl()
    monkeypatch.setattr(L, "DURABLE_TIMEOUT_S", 0.2)

    async def main():
        led = await _opened(path, monkeypatch, ctl)
        ctl.gate.clear()
        with caplog.at_level(logging.ERROR, logger="levain.broker"):
            o = await Owner(led).start()
            o2 = await Owner(led).start()
            t0 = time.monotonic()
            with pytest.raises(L.LedgerUnavailable):
                await o.hold.durable()
            assert 0.19 < time.monotonic() - t0 < 1.0
            assert not led.healthy and led.fault_reason == "stall"
            errs = _errors(caplog)
            assert len(errs) == 1 and "ledger fault: stall" in errs[0]
            assert o.hold.state is L.STRANDED and o2.hold.state is L.STRANDED
            ctl.gate.set()                              # the flush now returns OK: S3a
            await until(lambda: ctl.returned == 1)
            await asyncio.sleep(0.05)
            assert o.hold.state is L.STRANDED and led.charged(SCOPE) == 2 * AMOUNT
            for x in (o, o2):
                await x.finish()
            await led.aclose(2.0)
            assert ctl.calls == 1                       # the writer started no further batch
    asyncio.run(main())


def test_slow_writer_does_not_fault(tmp_path, monkeypatch, caplog):
    path = ledger_path(tmp_path)
    ctl = FlushCtl()
    # the delay must sit between half the timeout (the last hold waits on a second flush, so its durable() times out)
    # and the timeout (no flush stalls past it, so no fault): 0.7 of it keeps a wide margin on each side, and the
    # pair is scaled together so a slow runner stretches both and the ratio holds
    timeout = scaled(1.0)
    ctl.delay = 0.7 * timeout
    monkeypatch.setattr(L, "DURABLE_TIMEOUT_S", timeout)

    async def main():
        led = await _opened(path, monkeypatch, ctl)
        with caplog.at_level(logging.ERROR, logger="levain.broker"):
            first = await Owner(led).start()
            await asyncio.sleep(0.01)
            rest = [await Owner(led).start() for _ in range(5)]
            last = rest[-1].hold
            with pytest.raises(L.LedgerUnavailable):
                await last.durable()                    # a flush returned within the last timeout: no fault
            assert led.healthy and led.fault_reason is None and _errors(caplog) == []
            assert last.state is L.RESOLVING and last.outcome == ("released",)
            for o in [first] + rest[:-1]:
                await until(lambda o=o: o.hold.state is L.RESERVED)
            await until(lambda: last.state is L.RESOLVED)
            assert led.charged(SCOPE) == 5 * AMOUNT
            for o in [first] + rest:
                await o.finish()
            await led.aclose(5.0)
    asyncio.run(main())


def test_durable_and_timeout_in_the_same_loop_turn_returns(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "DURABLE_TIMEOUT_S", 0.05)

    async def main():
        led = L.Ledger.open(ledger_path(tmp_path))
        loop = asyncio.get_running_loop()
        led.attach(loop, _writer=False)
        o = await Owner(led).start()
        t = asyncio.ensure_future(o.hold.durable())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        time.sleep(0.1)                                 # the timeout is now due ...
        loop.call_soon(led._durable, [o.hold.seq])      # ... and `_durable` is ahead of it in the ready queue
        await asyncio.wait([t])
        assert t.exception() is None and o.hold.state is L.RESERVED and led.healthy
        await o.finish()
    asyncio.run(main())


def test_adverse_order_flushed_but_unapplied_is_not_a_stall(tmp_path, monkeypatch, caplog):
    """codex r3 F1: the batch's flush returned and stamped `_flushed_at`; its `_durable` is held back until the hold's
    `durable()` timeout has run the stall check. The ledger stays healthy. Fail-first: with the stamp after the post,
    or no stamp at all, this test faults (a_mutate.py, set `writer`)."""
    path = ledger_path(tmp_path)
    ctl = FlushCtl()
    ctl.delay = 0.3
    monkeypatch.setattr(L, "DURABLE_TIMEOUT_S", 0.5)

    async def main():
        led = await _opened(path, monkeypatch, ctl)
        start = led._flushed_at
        held, stamps = [], []
        real_post = led._post

        def post(fn, *a):
            if fn == led._durable and not held:
                stamps.append(led._flushed_at)
                held.append(a)
                return True
            return real_post(fn, *a)
        led._post = post
        with caplog.at_level(logging.ERROR, logger="levain.broker"):
            o = await Owner(led).start()
            with pytest.raises(L.LedgerUnavailable):
                await o.hold.durable()
            assert held and stamps[0] > start           # the stamp was set before the post
            assert led.healthy and led.fault_reason is None and _errors(caplog) == []
            assert o.hold.state is L.RESOLVING and o.hold.outcome == ("released",)
            real_post(led._durable, *held[0])
            await until(lambda: o.hold.state is L.RESOLVED)
            assert led.charged(SCOPE) == 0 and led.healthy
            await o.finish()
            await led.aclose(5.0)
    asyncio.run(main())
