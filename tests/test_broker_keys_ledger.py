"""keys' wiring onto the reservation ledger (S1 3c-1 session B1, design `1023-1/3c/C1_DESIGN.md` r4 §2.5 and §7):
T2 (a cancel at each await resolves the hold), T4 and T4b (tripwires on keys.py's shape), T16 (the unmetered seam is
gone), T20 (every non-settle exit resolves before the task ends, the two ledger-unavailable sites, a cancel and a
stale exchange included), T20b (the owner callback is the backstop), T21's keys half (a route's per-session cap, per
route), the request check's refusal and a cap-less route admitting nothing, and `main()` / `_serve`'s open, attach
and close. Each exchange case drives the real `Keys._exchange` in-process on a real ledger file, against the scripted
upstream of `test_broker_keys_upstream.py`."""
from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import types
from pathlib import Path

import pytest

from levain.broker import codec, gwwire, norm
from levain.broker import keys as K
from levain.broker import ledger as L
from levain.contracts import canon
from tests.ledger_support import FlushCtl, file_lines, keys_ledger, resolutions, until
from levain.broker.policy import parse_price
from tests.test_broker_keys_upstream import (BODY, PRICE, _Rig, _Up, _lib, _q, _run, _scripted, _types, base_route,
                                             body_frames)

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets")

ROOT = Path(__file__).resolve().parents[1]
KEYS_PY = ROOT / "levain" / "broker" / "keys.py"
SCOPE = ("session-route", "s1", "r")
#: the hold of `_lib`'s exchange: the R4 §1.2 estimate of the fixture body at the `m1` price entry
AMOUNT = codec.CODECS["anthropic-messages"].estimate(json.loads(BODY), parse_price("m1", PRICE))


def _capture(rig) -> list:
    """Records every hold the rig's ledger admits."""
    got = []
    real = rig.ledger.admit

    def admit(*a, **kw):
        h = real(*a, **kw)
        got.append(h)
        return h

    rig.ledger.admit = admit
    return got


class _GatedWriter:
    """The exchange's front writer with its FIRST drain (the one after `A`) held at a gate."""

    def __init__(self, w) -> None:
        self._w = w
        self.n = 0
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self._w, name)

    async def drain(self) -> None:
        self.n += 1
        if self.n == 1:
            self.entered.set()
            await self.gate.wait()
        await self._w.drain()


def _durable_seen(monkeypatch) -> asyncio.Event:
    """An event set when an exchange reaches `hold.durable()` (the class method wrapped; the wait itself unchanged)."""
    ev = {"e": None}
    real = L.Hold.durable

    async def durable(self):
        ev["e"].set()
        return await real(self)

    monkeypatch.setattr(L.Hold, "durable", durable)
    return ev


# --- T2: a cancel at each await point of §5 rows 1-6 ----------------------------------------------------------------
# Under R4 §1.7 `A` and the body come BEFORE the admit, so row 1 (`A`'s drain) cancels an exchange that holds nothing
# yet; rows 2-6 hold the estimate from the admit on.

ROWS = ["1-after-A", "2-connect", "3-durable-pending", "3-durable-reserved", "4-head-drain", "5-upload-and-head",
        "6-relay"]


@pytest.mark.parametrize("row", ROWS)
def test_cancel_at_each_await_resolves(monkeypatch, caplog, row):
    # T2 [prescription: reasoned]: row 1 admits nothing (no `reserved` line, nothing upstream); rows 2-3 end
    # `released`, rows 4-6 `uncertain {ended}`, exactly one resolution per ref, `charged` back to its start only for
    # the released rows, and no LedgerMisuse on any path.
    up = _Up(stall_at=1 if row == "4-head-drain" else 0)
    gate = asyncio.Event if row == "2-connect" else None
    holder: dict = {}
    seen = _durable_seen(monkeypatch)

    async def go(d: str) -> dict:
        seen["e"] = asyncio.Event()
        hs_gate = None
        if gate is not None:
            hs_gate = holder["gate"] = gate()
        st = _scripted(monkeypatch, up, hs_gate)
        async with _Rig(d) as rig:
            holds = _capture(rig)
            led = rig.ledger
            ctl = None
            if row == "3-durable-pending":
                ctl = FlushCtl()
                monkeypatch.setattr(L, "_flush", ctl)
                ctl.gate.clear()
            held_posts: list = []
            if row == "3-durable-reserved":
                real_post = led._post
                capture_on = {"on": True}

                def post(fn, *args):
                    if capture_on["on"] and fn == led._durable:
                        held_posts.append(args)
                        return True
                    return real_post(fn, *args)

                led._post = post
            gw = None
            if row == "1-after-A":
                gw = _GatedWriter(rig.conn.writer)
                rig.conn.writer = gw
            rig.front.feed_data(body_frames())
            if row in ("6-relay",):
                up.feed(b"HTTP/1.1 200 OK\r\ncontent-length: 10\r\n\r\nabcde")
            task = rig.run(_q())
            state_at_cancel = None
            if row == "1-after-A":
                await asyncio.wait_for(gw.entered.wait(), 5)
            elif row == "2-connect":
                await until(lambda: st["handshakes"] == 1)
            elif row == "3-durable-pending":
                await asyncio.wait_for(seen["e"].wait(), 5)
                await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, ctl.entered.wait, 5), 6)
            elif row == "3-durable-reserved":
                await asyncio.wait_for(seen["e"].wait(), 5)
                await until(lambda: len(held_posts) == 1)
                # the `_durable` callback runs and the task has not resumed: apply it and cancel in one step, the way
                # `_detach` delivers one (the exchange marked detached, then its task cancelled)
                led._durable(*held_posts[0])
                capture_on["on"] = False
                state_at_cancel = holds[0].state
                rig.conn.detached = True
                task.cancel()
            elif row == "4-head-drain":
                await until(lambda: up.drains >= 1)
            elif row == "5-upload-and-head":
                await until(lambda: up.reads >= 1)
            elif row == "6-relay":
                await until(lambda: up.reads >= 2)
            if not task.cancelled() and state_at_cancel is None:
                state_at_cancel = holds[0].state if holds else None
                task.cancel()
            if ctl is not None:
                ctl.gate.set()
            if gw is not None:
                gw.gate.set()
            if hs_gate is not None:
                hs_gate.set()
            await asyncio.wait([task])
            charged_before_close = None
            if holds:
                await until(lambda: holds[0].state in (L.RESOLVED, L.STRANDED))
            charged_before_close = led.charged(SCOPE)
            out = {"cancelled": task.cancelled(), "state_at_cancel": state_at_cancel,
                   "charged": charged_before_close, "path": led.path, "head": up.writes[:1]}
        out["res"] = resolutions(out["path"])
        out["seqs"] = [o["seq"] for o in file_lines(out["path"]) if o["kind"] == "reserved"]
        return out

    with caplog.at_level(logging.WARNING):
        out = _run(go)
    released_rows = ("2-connect", "3-durable-pending", "3-durable-reserved")
    if row == "3-durable-reserved" and sys.version_info < (3, 12):
        # before 3.12, `asyncio.wait_for` returns the result and drops a cancel that lands in the turn its awaited
        # future completed (bpo-42130), so `durable()` returns: the cancel is lost, and the detached flag is what stops
        # the head (`Upstream.write` refuses it, so `end()` still releases). keys calls cancellation "delivery only".
        assert out["cancelled"] in (True, False)
    else:
        assert out["cancelled"] is True
    if row == "1-after-A":
        assert out["seqs"] == [] and out["res"] == [], out   # cancelled before its body: nothing admitted
        assert out["charged"] == 0 and out["head"] == []
        assert "misuse" not in caplog.text
        return
    assert len(out["seqs"]) == 1
    seq = out["seqs"][0]
    if row in released_rows:
        assert out["res"] == [("released", seq, None)], out
        assert out["charged"] == 0
        assert out["head"] == []                              # nothing went upstream
    else:
        assert out["state_at_cancel"] is L.SENT
        assert out["res"] == [("uncertain", seq, "ended")], out
        assert out["charged"] == AMOUNT
    if row == "3-durable-pending":
        assert out["state_at_cancel"] is L.PENDING
    if row == "3-durable-reserved":
        assert out["state_at_cancel"] is L.RESERVED
        assert out["head"] == []   # the detach withheld the head, whether or not the cancel was delivered
    assert "misuse" not in caplog.text


def test_completed_relay_settles_at_the_full_hold(monkeypatch):
    # §2.5 / Q5 (3c-1's cut): a relay that ends normally settles the hold at its full amount; the `else: end()` after
    # it is a no-op, and the owner callback adds nothing
    up = _Up()

    async def go(d: str) -> dict:
        _scripted(monkeypatch, up)
        async with _Rig(d) as rig:
            holds = _capture(rig)
            rig.front.feed_data(body_frames())
            up.feed(b"HTTP/1.1 200 OK\r\ncontent-length: 5\r\n\r\nabcde")
            up.feed(b"")
            await asyncio.wait_for(rig.run(_q()), 5)
            frames = await rig.got()
            await until(lambda: holds[0].state is L.RESOLVED)
            out = {"frames": frames, "charged": rig.ledger.charged(SCOPE), "path": rig.ledger.path,
                   "outcome": holds[0].outcome}
        out["res"] = resolutions(out["path"])
        out["seqs"] = [o["seq"] for o in file_lines(out["path"]) if o["kind"] == "reserved"]
        return out

    out = _run(go)
    assert _types(out["frames"]) == b"ARbe", out["frames"]
    assert out["res"] == [("settled", out["seqs"][0], AMOUNT)]
    assert out["outcome"] == (L.SETTLED, AMOUNT, 0) and out["charged"] == AMOUNT


# --- T20 / T20b: every non-settle exit resolves while the task still runs; the backstop catches a missed one --------

#: (R4 §1.7: the upload is keys' buffer, so the front's abort during it, the old `resp-none` exit, has no path)
EXITS = ["detached", "connect-lost", "front-aborted", "refused-before-r", "upstream-failed",
         "unavailable-at-durable", "unavailable-at-send-gate", "cancel", "stale"]
#: the two `LedgerUnavailable -> keys-unavailable` sites in `_serve_held`: the ledger closes before the durable wait,
#: or after it returns and before `send_gate` (B1 L1 #1)
_UNAVAILABLE = ("unavailable-at-durable", "unavailable-at-send-gate")


def _exit_case(monkeypatch, exit_: str, *, end_stubbed: bool = False) -> dict:
    up = _Up()
    seen = _durable_seen(monkeypatch)
    if end_stubbed:
        monkeypatch.setattr(L.Hold, "end", lambda self: False)
    hook: dict = {}
    if exit_ in _UNAVAILABLE or exit_ == "stale":
        seen_durable = L.Hold.durable

        async def durable(self):
            # `aclose` resolves a live hold `released` (its `closed` column) and closes the ledger; run as its own
            # task, its synchronous part is done after one yield
            if exit_ == "unavailable-at-durable":
                hook["close"] = asyncio.ensure_future(hook["ledger"].aclose(2.0))
                await asyncio.sleep(0)
            await seen_durable(self)
            if exit_ == "unavailable-at-send-gate":
                hook["close"] = asyncio.ensure_future(hook["ledger"].aclose(2.0))
                await asyncio.sleep(0)
            if exit_ == "stale":
                hook["conn"].detached = True   # a phase 1 after the durable wait: `Upstream.write` raises Stale

        monkeypatch.setattr(L.Hold, "durable", durable)

    async def go(d: str) -> dict:
        seen["e"] = asyncio.Event()
        hs_gate = asyncio.Event() if exit_ == "connect-lost" else None
        st = _scripted(monkeypatch, up, hs_gate)
        rel_gate, ref_gate = asyncio.Event(), asyncio.Event()
        at: dict = {}
        async with _Rig(d) as rig:
            holds = _capture(rig)
            led = rig.ledger
            hook["ledger"], hook["conn"] = led, rig.conn
            box: dict = {}

            def snap(where: str) -> None:
                h = holds[0] if holds else None
                at[where] = (h.state if h else None, h.outcome if h else None, box["task"].done())

            real_release = K.release_exchange

            async def gated_release(exchange):
                snap("release")
                await rel_gate.wait()
                return await real_release(exchange)

            monkeypatch.setattr(K, "release_exchange", gated_release)
            real_refuse = K.Keys._refuse

            async def gated_refuse(self, conn, frames, side, name, cap):
                if holds:
                    snap("refuse")
                    await ref_gate.wait()
                return await real_refuse(self, conn, frames, side, name, cap)

            monkeypatch.setattr(K.Keys, "_refuse", gated_refuse)
            ctl = None
            if exit_ == "detached":
                real_admit = rig.k._admit_hold

                def admit_then_detach(*a):
                    h = real_admit(*a)
                    rig.conn.detached = True   # a phase 1 landing right after the admit
                    return h

                rig.k._admit_hold = admit_then_detach
            if exit_ == "front-aborted":
                ctl = FlushCtl()
                monkeypatch.setattr(L, "_flush", ctl)
                ctl.gate.clear()
            if exit_ == "refused-before-r":
                def refuse_head(*a, **kw):
                    raise norm.Refused("upstream-error", "a response head keys refuses")

                rig.k._response_frame = refuse_head
                up.feed(b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n")
            if exit_ == "upstream-failed":
                up.feed(b"")   # EOF before a head
            rig.front.feed_data(body_frames())
            task = box["task"] = rig.run(_q())
            if exit_ == "connect-lost":
                await until(lambda: st["handshakes"] == 1)
                rig.front.feed_data(gwwire.encode(b"X"))
                rig.front.feed_eof()
            elif exit_ == "front-aborted":
                await asyncio.wait_for(seen["e"].wait(), 5)
                rig.front.feed_data(gwwire.encode(b"X"))
                rig.front.feed_eof()
                for _ in range(20):
                    await asyncio.sleep(0)
                ctl.gate.set()
            elif exit_ == "cancel":
                await until(lambda: up.reads >= 1)   # the head is sent: the hold is SENT
                task.cancel()
            await until(lambda: "refuse" in at or "release" in at)
            if "refuse" in at:
                ref_gate.set()
                rig.front.feed_eof()   # ends the refusal's drain
            await until(lambda: "release" in at)
            rel_gate.set()
            if hs_gate is not None:
                hs_gate.set()
            await asyncio.wait([task])
            await asyncio.sleep(0)    # the owner callback runs after the task ends
            out = {"at": at, "head": up.writes[:1], "path": led.path,
                   "state_after": holds[0].state, "outcome_after": holds[0].outcome,
                   "raised": None if task.cancelled() else task.exception()}
            if exit_ in _UNAVAILABLE:
                await asyncio.wait_for(hook["close"], 5)
                out["frames"] = await rig.got()
        out["res"] = resolutions(out["path"])
        out["seqs"] = [o["seq"] for o in file_lines(out["path"]) if o["kind"] == "reserved"]
        return out

    return _run(go)


_SENT_EXITS = ("refused-before-r", "upstream-failed", "cancel")


@pytest.mark.parametrize("exit_", EXITS)
def test_non_settle_exit_resolves_before_task_end(monkeypatch, exit_):
    # T20 [prescription: reasoned]: with `release_exchange` and `_refuse`'s drain gated open, the hold is resolving
    # (`released`, or `uncertain {ended}` if sent) BEFORE the gate is released, while the task is still running.
    # `cancel` and `stale` leave by `except BaseException`: its explicit `end()`, not the owner backstop, resolves
    # them. The two ledger-unavailable sites answer `F{keys-unavailable}` and leave the hold `released`.
    out = _exit_case(monkeypatch, exit_)
    sent = exit_ in _SENT_EXITS
    want = (L.UNCERTAIN, "ended") if sent else (L.RELEASED,)
    first = "refuse" if "refuse" in out["at"] else "release"
    if exit_ in ("refused-before-r", "upstream-failed") + _UNAVAILABLE:
        assert first == "refuse"                               # the refusal's drain comes first
    if exit_ in _UNAVAILABLE:
        assert out["raised"] is None, out["raised"]            # refused, not raised out of the exchange
        assert _types(out["frames"]) == b"AF", out["frames"]
        assert out["frames"][-1] == (b"F", gwwire.encode(b"F", {"refused": "keys-unavailable"})[5:])
    if exit_ == "stale":
        assert out["raised"] is None                           # `_exchange` takes StaleExchange as a detach
    state, outcome, done = out["at"][first]
    assert done is False                                       # the task is still running
    assert state in (L.RESOLVING, L.RESOLVED) and outcome == want, out
    assert (out["head"] != []) is sent
    seq = out["seqs"][0]
    assert out["res"] == [("uncertain", seq, "ended") if sent else ("released", seq, None)]


def test_backstop_catches_a_missed_exit(monkeypatch):
    # T20b [prescription: reasoned]: with `end()` stubbed to do nothing, a sent hold is still SENT while the task runs
    # its release; when the task ends, the ledger's owner callback books it `uncertain {owner-ended}`.
    out = _exit_case(monkeypatch, "upstream-failed", end_stubbed=True)
    state, _outcome, done = out["at"]["release"]
    assert done is False and state is L.SENT                  # nothing resolved it before the task ended
    seq = out["seqs"][0]
    assert out["res"] == [("uncertain", seq, "owner-ended")]
    assert out["outcome_after"] == (L.UNCERTAIN, "owner-ended")


# --- B1 L3 r1: a fenced relay, a misuse at the send gate, `end()` on a closing or unhealthy ledger ------------------

def test_a_fenced_http_relay_is_uncertain_not_settled(monkeypatch):
    # codex MED + L1 #4: preemption's `_fence_exchanges` marks the exchange detached without cancelling it; the relay
    # sees the flag and returns early, so the response was cut short: `uncertain {ended}`, never `settled`
    up = _Up()

    async def go(d: str) -> dict:
        _scripted(monkeypatch, up)
        async with _Rig(d) as rig:
            holds = _capture(rig)
            rig.front.feed_data(body_frames())
            up.feed(b"HTTP/1.1 200 OK\r\ncontent-length: 10\r\n\r\nabcde")
            task = rig.run(_q())
            await until(lambda: up.reads >= 2)   # the relay waits on the body's rest
            rig.k._fence_exchanges()
            up.feed(b"fghij")
            await asyncio.wait_for(task, 5)
            await until(lambda: holds[0].state is L.RESOLVED)
            out = {"frames": await rig.got(), "path": rig.ledger.path, "outcome": holds[0].outcome}
        out["res"] = resolutions(out["path"])
        out["seqs"] = [o["seq"] for o in file_lines(out["path"]) if o["kind"] == "reserved"]
        return out

    out = _run(go)
    assert _types(out["frames"]) == b"ARb", out["frames"]   # no `e`: the relay stopped at the fence
    assert out["res"] == [("uncertain", out["seqs"][0], "ended")], out
    assert out["outcome"] == (L.UNCERTAIN, "ended")


def test_a_websocket_upgrade_on_a_metered_route_is_refused_before_a_hold(monkeypatch):
    # R4 §1.6 (b1_expected's websocket row): one exchange carries many responses, so no single usage settles it; the
    # upgrade is refused at §1.7 step 1, before `A`: no connection, no `reserved` line, `F{body-unbounded:websocket}`
    up = _Up()

    async def go(d: str) -> dict:
        st = _scripted(monkeypatch, up)
        async with _Rig(d) as rig:
            holds = _capture(rig)
            rig.front.feed_data(gwwire.encode(b"E"))
            ws_q = dict(_q("none"), upgrade="websocket", headers=[["sec-websocket-key", "dGhlIHNhbXBsZSBub25jZQ=="],
                                                                ["sec-websocket-version", "13"]])
            await asyncio.wait_for(rig.run(ws_q), 5)
            out = {"frames": await rig.got(), "path": rig.ledger.path, "holds": len(holds),
                   "handshakes": st["handshakes"]}
        out["kinds"] = [o["kind"] for o in file_lines(out["path"])]
        return out

    out = _run(go)
    assert out["frames"] == [(b"F", gwwire.encode(b"F", {"refused": "body-unbounded:websocket"})[5:])], out["frames"]
    assert out["holds"] == 0 and "reserved" not in out["kinds"]
    assert out["handshakes"] == 0 and up.writes == []


def test_a_misuse_at_the_send_gate_is_keys_unavailable(monkeypatch, caplog):
    # complement LOW 1: `LedgerMisuse` from `send_gate` is noted and answered `F{keys-unavailable}`, as `_admit_hold`
    # maps it; nothing goes upstream and the unsent hold is released
    up = _Up()

    def misuse(self, hold):
        raise L.LedgerMisuse("a scripted misuse")

    monkeypatch.setattr(L.Ledger, "_hold_send_gate", misuse)

    async def go(d: str) -> dict:
        _scripted(monkeypatch, up)
        async with _Rig(d) as rig:
            holds = _capture(rig)
            rig.front.feed_data(body_frames())
            task = rig.run(_q())
            await asyncio.wait([task], timeout=5)
            out = {"raised": None if task.cancelled() else task.exception(), "frames": await rig.got(),
                   "path": rig.ledger.path, "head": up.writes[:1]}
            await until(lambda: holds[0].state is L.RESOLVED)
        out["res"] = resolutions(out["path"])
        out["seqs"] = [o["seq"] for o in file_lines(out["path"]) if o["kind"] == "reserved"]
        return out

    with caplog.at_level(logging.WARNING):
        out = _run(go)
    assert out["raised"] is None, out["raised"]
    assert _types(out["frames"]) == b"AF", out["frames"]
    assert out["frames"][-1] == (b"F", gwwire.encode(b"F", {"refused": "keys-unavailable"})[5:])
    assert out["head"] == []
    assert out["res"] == [("released", out["seqs"][0], None)]
    assert "ledger misuse at the send gate" in caplog.text


@pytest.mark.parametrize("ledger_state", ["closing", "unhealthy"])
def test_end_in_the_refusal_handler_does_not_raise(monkeypatch, ledger_state):
    # complement LOW 2, answered by ledger.py: `end()` on a closing ledger finds the hold already resolved by
    # `aclose`'s `closed` column, and on an unhealthy one finds it STRANDED by `_fault`; either way `_end` is a no-op
    # returning False, never a raise, so the refusal handler's `_refuse` still sends `F{name}`. The hold is SENT
    # (the ledger turns after `mark_sent`) and the upstream ends before a head: `upstream-error`.
    up = _Up()
    hook: dict = {}
    ends: list = []
    real_mark_sent, real_end = L.Hold.mark_sent, L.Hold.end

    def mark_sent(self):
        real_mark_sent(self)
        if ledger_state == "closing":
            hook["close"] = asyncio.ensure_future(hook["ledger"].aclose(2.0))   # its synchronous part: the next turn
        else:
            hook["ledger"]._fault(None, "stall")

    def end(self):
        try:
            r = real_end(self)
        except BaseException as e:
            ends.append(e)
            raise
        ends.append(r)
        return r

    monkeypatch.setattr(L.Hold, "mark_sent", mark_sent)
    monkeypatch.setattr(L.Hold, "end", end)

    async def go(d: str) -> dict:
        _scripted(monkeypatch, up)
        async with _Rig(d) as rig:
            holds = _capture(rig)
            hook["ledger"] = rig.ledger
            rig.front.feed_data(body_frames())
            up.feed(b"")   # EOF before a head
            task = rig.run(_q())
            await asyncio.wait([task], timeout=5)
            out = {"raised": None if task.cancelled() else task.exception(), "frames": await rig.got(),
                   "path": rig.ledger.path, "head": up.writes[:1], "state": holds[0].state,
                   "outcome": holds[0].outcome}
            if "close" in hook:
                await asyncio.wait_for(hook["close"], 5)
        out["res"] = resolutions(out["path"])
        out["seqs"] = [o["seq"] for o in file_lines(out["path"]) if o["kind"] == "reserved"]
        return out

    out = _run(go)
    assert out["raised"] is None, out["raised"]
    assert out["head"] != []                                  # the head went: the hold was SENT
    assert ends == [False], ends                              # the handler's `end()`: a no-op, no raise
    assert _types(out["frames"]) == b"AF", out["frames"]
    assert out["frames"][-1] == (b"F", gwwire.encode(b"F", {"refused": "upstream-error"})[5:])
    if ledger_state == "closing":
        assert out["outcome"] == (L.UNCERTAIN, "closed")
        assert out["res"] == [("uncertain", out["seqs"][0], "closed")]
    else:
        assert out["state"] is L.STRANDED and out["res"] == []


# --- T21, keys' half: a route's per-session cap, per route ----------------------------------------------------------

def _two_route_lib(d: str) -> None:
    os.makedirs(os.path.join(d, "routes"))
    os.makedirs(os.path.join(d, "keys"), mode=0o700)

    with open(os.path.join(d, "routes", "base.json"), "wb") as f:
        f.write(canon.canonical_bytes({"format": "levain-routes-base/2",
                                       "routes": [base_route("a", 100, credential=None, headers={}),
                                                  base_route("b", 1000, credential=None, headers={})]}))


def test_route_caps_are_per_route():
    # T21 keys half [prescription: reasoned, codex r2's corrected sequence]: routes `a` (cap 100) and `b` (cap 1,000)
    # for one session: 900 through `b` admits; 100 through `a`; 1 more through `a` is 402; 100 through `b`; 1 more
    # through `b` is 402. The `reserved` lines carry the two scopes and the two caps.
    d = tempfile.mkdtemp(prefix="l2", dir="/tmp")
    _two_route_lib(d)
    path = os.path.join(d, "keys", "ledger.jsonl")

    async def go() -> dict:
        led = keys_ledger(path)
        try:
            k = K.Keys(ledger=led, lib_root=d)
            claims = {"routes": ["a", "b"]}
            pa, pb = k._route_policy(claims, "a"), k._route_policy(claims, "b")
            reg = types.SimpleNamespace(session="s1")
            seq = []
            # the holds are the calls' estimates: 900 on `b`, 100 on `a`, 100 on `b` (the ledger never shrinks one)
            for pol, est in ((pb, 900), (pa, 100), (pa, 1), (pb, 100), (pb, 1)):
                try:
                    h = k._admit_hold(reg, pol, {}, est, "m1", {})
                    seq.append(h.amount)
                except norm.Refused as r:
                    seq.append((r.name, r.status))
            return {"seq": seq, "a": led.charged(("session-route", "s1", "a")),
                    "b": led.charged(("session-route", "s1", "b"))}
        finally:
            await led.aclose(2.0)

    try:
        out = asyncio.run(go())
        res = [(o["scope"], o["cap"], o["amount"]) for o in file_lines(path) if o["kind"] == "reserved"]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    exhausted = ("budget-exhausted", 402)
    assert out["seq"] == [900, 100, exhausted, 100, exhausted]
    assert out["a"] == 100 and out["b"] == 1000
    sa, sb = ["session-route", "s1", "a"], ["session-route", "s1", "b"]
    assert res == [(sb, 1000, 900), (sa, 100, 100), (sb, 1000, 100)]


def test_no_ledger_and_ledger_errors_are_refusals(caplog):
    # §2.5 / Q6: a Keys built with no ledger refuses `budget-exhausted` "no ledger"; a closing ledger is
    # `keys-unavailable`; a misuse is `keys-unavailable` with a WARNING note (§2.2)
    d = tempfile.mkdtemp(prefix="l3", dir="/tmp")
    _two_route_lib(d)

    async def go() -> list:
        reg = types.SimpleNamespace(session="s1")
        out = []
        k = K.Keys(lib_root=d)
        pol = k._route_policy({"routes": ["a"]}, "a")
        for case in ("none", "misuse", "closing"):
            led = None
            if case != "none":
                led = keys_ledger(os.path.join(d, "keys", f"ledger-{case}.jsonl"))
                k.ledger = led
                if case == "misuse":
                    def misuse(*a, **kw):
                        raise L.LedgerMisuse("a ledger call from inside bound")
                    led.admit = misuse
                else:
                    await led.aclose(2.0)
            try:
                k._admit_hold(reg, pol, {}, 1, "m1", {})
                out.append("admitted")
            except norm.Refused as r:
                out.append((r.name, r.detail))
            finally:
                if case == "misuse":
                    await led.aclose(2.0)
        return out

    try:
        with caplog.at_level(logging.WARNING):
            got = asyncio.run(go())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert got[0] == ("budget-exhausted", "no ledger")
    assert got[1] == ("keys-unavailable", "ledger misuse")
    assert got[2][0] == "keys-unavailable"
    assert "ledger misuse at admit" in caplog.text


def test_a_capless_route_is_no_scope():
    # B1 L1 #5: a RoutePolicy with no per-session cap (a dispatch route's shape) is refused `budget-exhausted`
    # "no scope" before the ledger is asked; nothing is admitted
    d = tempfile.mkdtemp(prefix="l4", dir="/tmp")
    _two_route_lib(d)
    path = os.path.join(d, "keys", "ledger.jsonl")

    async def go() -> tuple:
        led = keys_ledger(path)
        try:
            k = K.Keys(ledger=led, lib_root=d)
            pol = dataclasses.replace(k._route_policy({"routes": ["a"]}, "a"), max_session_microdollars=None)
            try:
                k._admit_hold(types.SimpleNamespace(session="s1"), pol, {}, 1, "m1", {})
                got = "admitted"
            except norm.Refused as r:
                got = (r.name, r.detail)
            return got, led.charged(("session-route", "s1", "a"))
        finally:
            await led.aclose(2.0)

    try:
        got, charged = asyncio.run(go())
        kinds_ = [o["kind"] for o in file_lines(path)]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert got == ("budget-exhausted", "no scope")
    assert charged == 0 and "reserved" not in kinds_


def test_a_request_check_refusal_admits_nothing(monkeypatch):
    # B1 L1 #3 (pins `_check_request`'s "the ledger's admit last"): a `Q` refused by the request check (a header off
    # the route's list) is answered `F{policy-header}` with nothing admitted: `charged` is 0 while the refusal runs,
    # and the file holds no `reserved` line
    up = _Up()

    async def go(d: str) -> dict:
        _scripted(monkeypatch, up)
        at: dict = {}
        async with _Rig(d) as rig:
            holds = _capture(rig)
            real_refuse = K.Keys._refuse

            async def snap_refuse(self, conn, frames, side, name, cap):
                at["charged"], at["holds"] = rig.ledger.charged(SCOPE), len(holds)
                return await real_refuse(self, conn, frames, side, name, cap)

            monkeypatch.setattr(K.Keys, "_refuse", snap_refuse)
            q = _q()
            q["headers"] = q["headers"] + [["x-not-on-the-list", "1"]]
            rig.front.feed_eof()
            await asyncio.wait_for(rig.run(q), 5)
            out = {"at": at, "frames": await rig.got(), "path": rig.ledger.path}
        out["kinds"] = [o["kind"] for o in file_lines(out["path"])]
        return out

    out = _run(go)
    assert out["frames"][-1] == (b"F", gwwire.encode(b"F", {"refused": "policy-header"})[5:]), out["frames"]
    assert out["at"] == {"charged": 0, "holds": 0}, out["at"]
    assert "reserved" not in out["kinds"] and up.writes == []


# --- main() and _serve: open before bind, exit 3 on a refused open, attach and close ---------------------------------

def _main_args(tmp_path) -> list:
    return ["--runtime-root", str(tmp_path), "--lib-root", str(tmp_path), "--control", str(tmp_path / "c"),
            "--test-single-uid"]


@pytest.mark.skipif(os.geteuid() == 0, reason="keys refuses to run as root (it runs as _levain_keys): main() exits "
                                              "before the ledger open this test is about")
@pytest.mark.parametrize("why", ["no-keys-dir", "locked"])
def test_main_refuses_to_start_on_a_refused_ledger_open(monkeypatch, tmp_path, capsys, why):
    bound = []
    monkeypatch.setattr(K, "bind_listeners", lambda pairs: bound.append(pairs) or [])
    monkeypatch.setattr(K, "install_log", lambda lib_root: None)
    monkeypatch.setattr(K, "raise_nofile", lambda: None)
    other = None
    if why == "locked":
        (tmp_path / "keys").mkdir(mode=0o700)
        other = L.Ledger.open(str(tmp_path / "keys" / "ledger.jsonl"))
    try:
        rc = K.main(_main_args(tmp_path))
    finally:
        if other is not None:
            os.close(other._fd)
    err = capsys.readouterr().err
    assert rc == 3 and bound == []                       # refused before anything is bound
    assert "ledger refused" in err
    assert ("ledger-locked" in err) if why == "locked" else ("ENOENT" in err)


def test_serve_attaches_before_start_and_closes_after(monkeypatch, tmp_path):
    (tmp_path / "keys").mkdir(mode=0o700)
    led = L.Ledger.open(str(tmp_path / "keys" / "ledger.jsonl"))
    k = K.Keys(single_uid=os.getuid(), control_uid=os.getuid(), ledger=led)
    seen = {}

    async def start(self, socks, admin=None):
        seen["attached_at_start"] = led._loop is not None and led._thread is not None and led._thread.is_alive()
        for s in list(socks) + [admin]:
            s.close()
        self.trigger()

    monkeypatch.setattr(K.Keys, "start", start)
    short = tempfile.mkdtemp(prefix="ls", dir="/tmp")
    try:
        rc = asyncio.run(K._serve(k, os.path.join(short, "f.sock"), None, os.path.join(short, "a.sock"), None))
    finally:
        shutil.rmtree(short, ignore_errors=True)
    assert rc == 0 and seen["attached_at_start"] is True
    assert led.closing is True
    led._thread.join(5)
    assert not led._thread.is_alive()                    # the writer drained, closed the fd and exited
    again = L.Ledger.open(str(tmp_path / "keys" / "ledger.jsonl"))   # the flock is free
    os.close(again._fd)


# --- T4, T4b, T16: tripwires ----------------------------------------------------------------------------------------

LEDGER_METHODS = frozenset({"admit", "attach", "aclose", "charged", "totals", "durable", "send_gate", "mark_sent",
                            "end", "release", "settle", "uncertain"})
#: the one ledger call a `finally` of keys.py may hold: `_serve`'s close, after `wait_done` (design §2.5, §2.7)
FINALLY_ALLOW = {("_serve", "aclose"): 1}


def _tree() -> ast.Module:
    return ast.parse(KEYS_PY.read_text())


def _defs(tree: ast.AST):
    """(qualified name, node) for every function, nested ones included."""
    out = []

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = prefix + ch.name
                out.append((q, ch))
                walk(ch, q + ".")
            elif isinstance(ch, ast.ClassDef):
                walk(ch, prefix + ch.name + ".")
            else:
                walk(ch, prefix)
    walk(tree, "")
    return out


def test_no_ledger_call_in_a_finally():
    # T4: no Hold or Ledger method call inside any `finally:` in keys.py, but the one named close in `_serve`
    found: dict = {}

    def visit(node, fn_name: str, in_finally: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_name, in_finally = node.name, False   # a def inside a finally is not run by it
        if (in_finally and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in LEDGER_METHODS):
            key = (fn_name, node.func.attr)
            found[key] = found.get(key, 0) + 1
        if isinstance(node, ast.Try):
            for field in ("body", "handlers", "orelse"):
                for ch in getattr(node, field):
                    visit(ch, fn_name, in_finally)
            for ch in node.finalbody:
                visit(ch, fn_name, True)
            return
        for ch in ast.iter_child_nodes(node):
            visit(ch, fn_name, in_finally)

    visit(_tree(), "", False)
    assert found == FINALLY_ALLOW, found


def _calls_in(node, name: str):
    return [c for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == name]


def test_send_gate_write_mark_sent_order():
    # T4b: in `send_head`, `hold.send_gate()`, then `up.write(head)`, then `hold.mark_sent()`, with no `await`
    # between the first and the last; `mark_sent` appears nowhere else. In `_serve_q`, the statement that receives
    # `hold` from `_check_request` is the last statement of its block, and the block's `try` (no `else`, no `finally`)
    # is followed directly by the `try:` that wraps `_serve_held`.
    tree = _tree()
    defs = dict(_defs(tree))
    send_head = defs["Keys._serve_held.send_head"]
    gate = [c for c in _calls_in(send_head, "send_gate") if isinstance(c.func.value, ast.Name)
            and c.func.value.id == "hold"]
    write = [c for c in _calls_in(send_head, "write") if isinstance(c.func.value, ast.Name)
             and c.func.value.id == "up" and [ast.dump(a) for a in c.args] == [ast.dump(ast.Name("head", ast.Load()))]]
    mark = [c for c in _calls_in(send_head, "mark_sent") if isinstance(c.func.value, ast.Name)
            and c.func.value.id == "hold"]
    assert len(gate) == len(write) == len(mark) == 1
    g, w, m = gate[0].lineno, write[0].lineno, mark[0].lineno
    assert g < w < m
    awaits = [a.lineno for a in ast.walk(send_head) if isinstance(a, ast.Await)]
    assert not [x for x in awaits if g <= x <= m]
    assert len(_calls_in(tree, "mark_sent")) == 1          # nowhere else in keys.py

    serve_q = defs["Keys._serve_q"]
    sites = []
    for node in ast.walk(serve_q):
        for field in ("body", "orelse", "finalbody", "handlers"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, st in enumerate(block):
                if (isinstance(st, ast.Assign) and isinstance(st.value, ast.Call)
                        and isinstance(st.value.func, ast.Attribute) and st.value.func.attr == "_check_request"):
                    sites.append((node, field, i, st))
    assert len(sites) == 1
    parent, field, i, assign = sites[0]
    targets = assign.targets[0]
    assert isinstance(targets, ast.Tuple) and "hold" in [e.id for e in targets.elts if isinstance(e, ast.Name)]
    assert isinstance(parent, ast.Try) and field == "body" and i == len(parent.body) - 1
    assert parent.orelse == [] and parent.finalbody == []
    outer = serve_q.body
    j = outer.index(parent)
    nxt = outer[j + 1]
    assert isinstance(nxt, ast.Try)
    first = nxt.body[0]
    assert (isinstance(first, ast.Expr) and isinstance(first.value, ast.Await)
            and isinstance(first.value.value, ast.Call) and first.value.value.func.attr == "_serve_held")
    # the four clauses: `end()` in the refusal except, in `except BaseException`, and in `else`; none in `finally`
    assert [h.type.id if isinstance(h.type, ast.Name) else type(h.type).__name__ for h in nxt.handlers] == \
        ["Tuple", "BaseException"]
    for block in [h.body for h in nxt.handlers] + [nxt.orelse]:
        assert isinstance(block[0], ast.Expr) and block[0].value.func.attr == "end"
    assert not [c for st in nxt.finalbody for c in _calls_in(st, "end")]


def test_budget_stopped_is_reread_right_before_the_admit():
    # R4 §10 (3c-2a): `_check_request` and `_admit_hold` are plain functions (no `await` can sit between the re-read
    # and the admit), and in `_admit_hold` the statement right before the `try:` that calls `self.ledger.admit` is the
    # `budget_stopped` check.
    defs = dict(_defs(_tree()))
    for name in ("Keys._check_request", "Keys._admit_hold"):
        assert isinstance(defs[name], ast.FunctionDef), name
    body = defs["Keys._admit_hold"].body
    at = [i for i, st in enumerate(body) if isinstance(st, ast.Try) and _calls_in(st, "admit")]
    assert len(at) == 1 and at[0] > 0
    check = body[at[0] - 1]
    assert isinstance(check, ast.If) and "budget_stopped" in ast.dump(check.test)


def test_unmetered_is_gone():
    # T16: the unmetered seam and its names are gone from levain/ and tests/; no keys or CONTROL unit is installed
    # before 3c-9 (levain/slot/services/ holds neither)
    names = ["--" + "unmetered", "Unmetered" + "Budget", "Refusing" + "Budget"]
    hits = []
    for top in ("levain", "tests"):
        for p in (ROOT / top).rglob("*"):
            if p.is_file() and p.suffix in (".py", ".md", ".txt", ".json", ".sh", ".toml", ".cfg"):
                text = p.read_text(errors="replace")
                hits += [(str(p.relative_to(ROOT)), n) for n in names if n in text]
    assert hits == []
    services = {p.name for p in (ROOT / "levain" / "slot" / "services").iterdir()}
    assert "control.py" not in services and "keys.py" not in services
