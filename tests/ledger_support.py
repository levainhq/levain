"""Shared helpers for the reservation-ledger tests (S1 3c-1): a ledger path, a controllable flush, an owner task that
admits and waits, the file's lines, and canonical line builders for hand-made files."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any, Callable, List, Optional

from levain.broker import ledger as L

SCOPE = ("session-route", "s1", "r-a")
CAP = 1000
AMOUNT = 100


def ledger_path(tmp_path) -> str:
    d = tmp_path / "keys"
    d.mkdir(mode=0o700)
    return str(d / "ledger.jsonl")


def file_lines(path: str) -> List[dict]:
    with open(path, "rb") as f:
        return [json.loads(x) for x in f.read().splitlines()]


def kinds(path: str) -> List[str]:
    return [o["kind"] for o in file_lines(path)]


def queued(led: "L.Ledger") -> List[dict]:
    """Drains a manual (writer-less) ledger's queue: the lines it would have written, in order."""
    out = []
    while True:
        try:
            item = led._q.get_nowait()
        except Exception:
            return out
        out.append("CLOSE" if item is L._CLOSE else json.loads(item[1]))


async def until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met within %s s" % timeout)
        await asyncio.sleep(0.002)


def wait_thread(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    """A blocking wait on the loop thread (holds every loop callback back: the fault window W)."""
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met within %s s" % timeout)
        time.sleep(0.002)


class _Gate(threading.Event):
    """The flush gate. Closing it also clears `entered`, so a wait on `entered` after `gate.clear()` waits for a flush
    that starts after the clear, never one that ran before it."""

    def __init__(self, entered: threading.Event) -> None:
        super().__init__()
        self._entered = entered

    def clear(self) -> None:
        self._entered.clear()
        super().clear()


class FlushCtl:
    """Replaces `ledger._flush` after open: each call is recorded, may wait on a gate, sleep, or raise a scripted
    error, then runs the real flush. `entered` is set by the first flush call since the gate last closed."""

    def __init__(self) -> None:
        self.real = L._flush
        self.entered = threading.Event()
        self.gate = _Gate(self.entered)
        self.gate.set()
        self.calls = 0
        self.returned = 0
        self.delay = 0.0
        self.errors: List[Optional[BaseException]] = []      # popped per call; None = OK
        self.lock = threading.Lock()

    def __call__(self, fd: int) -> None:
        with self.lock:
            self.calls += 1
            err = self.errors.pop(0) if self.errors else None
        self.entered.set()
        self.gate.wait(30)
        if self.delay:
            time.sleep(self.delay)
        if err is not None:
            raise err
        self.real(fd)
        with self.lock:
            self.returned += 1


class Owner:
    """A task that admits one hold and then waits on `end` (the exchange still running), or awaits `durable()`."""

    def __init__(self, led: "L.Ledger", amount: int = AMOUNT, cap: int = CAP, scope=SCOPE,
                 then_durable: bool = False, price_ref: str = "r-a", meta: Optional[dict] = None) -> None:
        self.led = led
        self.amount, self.cap, self.scope = amount, cap, scope
        self.then_durable = then_durable
        self.price_ref, self.meta = price_ref, meta
        self.hold: Optional[L.Hold] = None
        self.error: Optional[BaseException] = None
        self.durable_error: Optional[BaseException] = None
        self.task: Optional[asyncio.Task] = None

    async def start(self) -> "Owner":
        self.end = asyncio.Event()
        got = asyncio.get_running_loop().create_future()

        async def body() -> None:
            try:
                self.hold = self.led.admit(self.scope, self.cap, lambda r: min(self.amount, r), self.price_ref,
                                           self.meta)
            except BaseException as e:
                self.error = e
                got.set_result(None)
                return
            got.set_result(None)
            if self.then_durable:
                try:
                    await self.hold.durable()
                except L.LedgerUnavailable as e:
                    self.durable_error = e
            await self.end.wait()

        self.task = asyncio.ensure_future(body())
        await got
        return self

    async def finish(self) -> None:
        self.end.set()
        await asyncio.wait([self.task])
        await asyncio.sleep(0)


def line(kind: str, seq: int, **f: Any) -> bytes:
    d = {"kind": kind, "seq": seq}
    d.update(f)
    if kind != "header":
        d.setdefault("at", "2026-10-23T01:02:03.000004Z")
    return L._encode(d)


E1 = "a" * 32
E2 = "b" * 32
LID = "c" * 32


def header(seq: int = 0) -> bytes:
    return line("header", seq, format=L.FORMAT, ledger_id=LID, created_at="2026-10-23T01:02:03Z")


def reserved(seq: int, epoch: str = E1, amount: int = AMOUNT, cap: int = CAP, scope=SCOPE, meta=None) -> bytes:
    return line("reserved", seq, epoch=epoch, scope=list(scope), cap=cap, amount=amount, price_ref="r-a",
                meta={} if meta is None else meta)


def write_file(path: str, *lines: bytes) -> None:
    with open(path, "wb") as f:
        f.write(b"".join(lines))
    os.chmod(path, 0o600)


def open_fds() -> int:
    return len(os.listdir("/dev/fd"))
