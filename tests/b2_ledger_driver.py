"""The driver process of the 3c-1 session B2 kill -9 tests (design `1023-1/3c/C1_DESIGN.md` §5, T7 and T23).

Run as `python tests/b2_ledger_driver.py <mode> <ledger path> <events file> [arg]`. It opens the REAL ledger, drives it
to one named gate, appends what it did to the events file (one JSON object per line, written with one `os.write`, so a
`kill -9` keeps every line the driver had written) and then blocks forever; the test SIGKILLs it and reopens the file.

Modes (`gate` modes block a writer-thread primitive; `scenario` is the hold script run before it):
  prelude-only   the prelude, then block (the baseline for the reopen)
  w1             block before `os.write` of the batch holding a `released` line
  w1-torn        write HALF of that batch, then block (a torn resolution line)
  w2             block after `os.write`, before the flush, of that batch
  w3             run the batch's flush to the end, block before the writer posts the callback
  row3           an owner task awaiting `durable()` is cancelled while the `reserved` flush is blocked: the driver cancels
                 and awaits the owner, waits for its hold to be RESOLVING (the release queued), and only then emits the
                 `gate` event the parent SIGKILLs on (the flush's own `blocked` event is private to the driver)
  row4           a hold past `send_gate` and `mark_sent`, then block (the await on the upstream drain)
  replay-seed    admit three holds, all durable and unresolved, then block (the file a replay will read)
  replay-gate    (opens the file `replay-seed` left) block replay's `flush-opened`, after its batch was written
  close          T23: a flush that never returns, then `aclose(SHUTDOWN_WAIT_S)` and a normal exit
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from levain.broker import ledger as L         # noqa: E402

SCOPE = ("session-route", "s1", "r")
CAP = 10000
AMOUNT = 1000

_events_fd = -1


def ev(**kw) -> None:
    kw["t"] = time.time()
    kw["m"] = time.monotonic()   # durations are measured on this one, never on the wall clock
    os.write(_events_fd, (json.dumps(kw, sort_keys=True) + "\n").encode())


def block_forever() -> None:
    while True:
        time.sleep(3600)


class Gate:
    """`armed` is flipped by the scenario just before the line it wants held; the wrapped primitive blocks at its next
    call and records that it did, so the test knows the kill lands there."""

    def __init__(self) -> None:
        self.armed = False

    def arm(self) -> None:
        self.armed = True


gate = Gate()


def install(mode: str) -> None:
    real_write, real_flush = L._os_write, L._flush

    if mode in ("w1", "w1-torn"):
        def os_write(fd, data):
            if gate.armed:
                gate.armed = False
                if mode == "w1-torn":
                    real_write(fd, bytes(data)[: max(1, len(data) // 2)])
                ev(at="gate", mode=mode)
                block_forever()
            return real_write(fd, data)
        L._os_write = os_write
    elif mode in ("w2", "row3"):
        # row3's event is `blocked`, not `gate`: the parent kills on `gate` and the driver emits that only after the
        # cancel it is testing has run
        def flush(fd):
            if gate.armed:
                gate.armed = False
                ev(at="gate" if mode == "w2" else "blocked", mode=mode)
                block_forever()
            return real_flush(fd)
        L._flush = flush
    elif mode == "w3":
        def flush(fd):
            real_flush(fd)
            if gate.armed:
                gate.armed = False
                ev(at="gate", mode=mode)
                block_forever()
        L._flush = flush
    elif mode == "close":
        def flush(fd):
            if gate.armed:
                ev(at="gate", mode=mode)
                block_forever()
            return real_flush(fd)
        L._flush = flush


async def settle_resolved(h) -> None:
    for _ in range(2000):
        if h.state in (L.RESOLVED, L.STRANDED):
            return
        await asyncio.sleep(0.005)
    raise SystemExit("hold never resolved: %r" % (h.state,))


async def prelude(led) -> None:
    """Two resolved holds, durable before the gate: h0 sent then settled, h1 released. Their lines are what I2 holds."""
    h0 = led.admit(SCOPE, CAP, lambda room: min(AMOUNT, room), "r")
    await h0.durable()
    h0.send_gate()
    h0.mark_sent()
    ev(at="sent", ref=h0.seq)
    h0.settle(AMOUNT)
    await settle_resolved(h0)
    ev(at="resolved", ref=h0.seq, how="settled")
    h1 = led.admit(SCOPE, CAP, lambda room: min(AMOUNT, room), "r")
    await h1.durable()
    h1.release()
    await settle_resolved(h1)
    ev(at="resolved", ref=h1.seq, how="released")


async def run(mode: str, path: str, arg: str) -> None:
    led = L.Ledger.open(path)
    led.attach(asyncio.get_running_loop())
    ev(at="open", ledger_id=led.ledger_id, epoch=led.epoch)
    bound = lambda room: min(AMOUNT, room)   # noqa: E731
    if mode == "close":
        h = led.admit(SCOPE, CAP, bound, "r")
        await h.durable()
        gate.armed = True
        # a hold whose resolution line the blocked flush will never confirm: aclose resolves it, the writer blocks
        t0 = time.monotonic()
        ev(at="aclose-start")
        await led.aclose(float(arg))
        ev(at="aclose-end", took=time.monotonic() - t0, states=[h.state.name])
        return
    await prelude(led)
    if mode == "prelude-only":
        ev(at="gate", mode=mode)
    elif mode in ("w1", "w1-torn", "w2", "w3"):
        h2 = led.admit(SCOPE, CAP, bound, "r")
        await h2.durable()
        ev(at="reserved", ref=h2.seq)
        gate.arm()
        h2.release()
        ev(at="released-queued", ref=h2.seq)
    elif mode == "row3":
        got = asyncio.get_running_loop().create_future()

        async def owner() -> None:
            h = led.admit(SCOPE, CAP, bound, "r")
            got.set_result(h)
            try:
                await h.durable()
            except asyncio.CancelledError:
                ev(at="owner-cancelled", ref=h.seq)
                h.end()                      # the owner-end column: RELEASED while unsent
                raise

        gate.arm()
        t = asyncio.ensure_future(owner())
        h = await got
        ev(at="admitted", ref=h.seq)
        while not event_seen("blocked"):
            await asyncio.sleep(0.005)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        ev(at="cancelled", ref=h.seq)
        for _ in range(2000):                # the release is queued once the hold is RESOLVING
            if h.state is L.RESOLVING:
                break
            await asyncio.sleep(0.005)
        else:
            raise SystemExit("the cancelled owner's hold never reached RESOLVING: %r" % (h.state,))
        ev(at="gate", mode=mode, state=h.state.name)
    elif mode == "row4":
        h2 = led.admit(SCOPE, CAP, bound, "r")
        await h2.durable()
        h2.send_gate()
        h2.mark_sent()
        ev(at="sent", ref=h2.seq)
        ev(at="gate", mode=mode)
    elif mode == "replay-seed":
        for _ in range(3):
            h = led.admit(SCOPE, CAP, bound, "r")
            await h.durable()
            ev(at="reserved", ref=h.seq)
        ev(at="gate", mode=mode)
    await asyncio.sleep(3600)


def event_seen(at: str) -> bool:
    with open(_events_path, "rb") as f:
        return ('"at": "%s"' % at).encode() in f.read()


_events_path = ""


def main() -> int:
    global _events_fd, _events_path
    mode, path, events = sys.argv[1], sys.argv[2], sys.argv[3]
    arg = sys.argv[4] if len(sys.argv) > 4 else ""
    _events_path = events
    _events_fd = os.open(events, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    if mode == "replay-gate":
        # the reopen under test is THIS process: block `flush-opened` (the second flush of an open on an existing
        # file: `flush-dir` is the first), after replay's batch was written
        real_flush, calls = L._flush, {"n": 0}

        def flush(fd):
            calls["n"] += 1
            if calls["n"] == 2:
                ev(at="gate", mode="replay-gate")
                block_forever()
            return real_flush(fd)
        L._flush = flush
        ev(at="opening")
        L.Ledger.open(path)
        ev(at="opened-unexpectedly")
        return 1
    install(mode)
    asyncio.run(run(mode, path, arg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
