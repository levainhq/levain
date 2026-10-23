"""3c-2b: an overrun (3c R4 §1.4, §8 tests 1 and 2). A settle whose reported usage costs more than its hold is charged
in full and stops the SESSION: its id enters keys' `budget_stopped` set, so every later `Q` of it is 402
`budget-overrun`. Nothing in flight is aborted; each in-flight exchange settles from its own usage. CONTROL is not
told and does not revoke (that wire is 3c-3).

Tests 1 and 2 run keys as a real child process (`tests/b2_keys_rig.py`) against a fake upstream that reports usage;
the set's lifetime tests run keys in-process, where the set and the claims cache can be looked at."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
from typing import Dict, List

import pytest

from levain.broker import gwwire
from levain.broker.norm import digest_of_token
from tests import b2_keys_rig as R
from tests.b2_keys_rig import FakeControl, FakeUpstream, Front, KeysProc, ledger_lines
from tests.ledger_support import file_lines
from tests.test_broker_keys_metered import _Sessions
from tests.test_broker_keys_process import PYS, until
from tests.test_broker_keys_upstream import BODY, BODY_Q, _q, _run, _scripted, _Up

pytestmark = [pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets"),
              pytest.mark.skipif(os.geteuid() == 0, reason="keys refuses to run as root (it runs as _levain_keys)")]

#: the rig body's hold at the `m1` entry (in 1, out 2, cache_write 1.25 microdollars per token): the R4 §1.2
#: estimate, ceil((len(BODY) + overhead 10) x 1.25) + max_tokens 10 x 2
HOLD = -(-(len(BODY) + 10) * 125 // 100) + 20
#: usage above it: 1000 x 1 + 500 x 2 = 2000 microdollars
OVER = {"input_tokens": 1000, "output_tokens": 500}
OVER_COST = 2000
#: usage below it: 30 x 1 + 5 x 2 = 40
UNDER = {"input_tokens": 30, "output_tokens": 5}
UNDER_COST = 40
CAP = 10_000_000   # far above every charge here: no refusal below is `budget-exhausted`


@pytest.fixture(params=PYS, ids=lambda p: "usr-bin-python3" if p == "/usr/bin/python3" else "venv")
def py(request):
    if not R.h11_available(request.param):
        pytest.skip("h11 is not importable by %s (named skip, not a pass)" % request.param)
    return request.param


class PerConnUpstream(FakeUpstream):
    """Connection i answers `responses[i]` once `gates[i]` is set (all set = answer at once)."""

    def __init__(self, responses: List[bytes]) -> None:
        super().__init__()
        self.responses = responses
        self.gates: List[asyncio.Event] = []
        self.closed: List[bool] = []

    async def _on(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        i = self.conns
        self.conns += 1
        self.writers.append(w)
        self.closed.append(False)
        try:
            head = await r.readuntil(b"\r\n\r\n")
            self.heads += 1
            n = 0
            for line in head.split(b"\r\n"):
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    n = int(value.strip())
            self.bodies.append(await r.readexactly(n))
            await self.gates[i].wait()
            w.write(self.responses[i])
            await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            self.closed[i] = True   # keys closed the request: an aborted exchange
        finally:
            w.close()


class World:
    def __init__(self, py: str, responses: List[bytes]) -> None:
        self.py, self.responses = py, responses

    async def __aenter__(self) -> "World":
        self.d = R.short_dir()
        R.lib(self.d, CAP)
        self.control = FakeControl(os.path.join(self.d, "control.sock"))
        await self.control.start()
        self.up = PerConnUpstream(self.responses)
        await self.up.start(hold=False)
        self.up.gates = [asyncio.Event() for _ in self.responses]
        self.keys = KeysProc(self.d, self.py, self.up.port)
        await self.keys.start()
        self.front = await Front(self.keys).claim()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.front.close()
        self.keys.cleanup()
        await self.up.close()
        await self.control.close()
        shutil.rmtree(self.d, ignore_errors=True)


def _resolutions(path: str) -> List[Dict]:
    return [o for o in ledger_lines(path) if o["kind"] in ("settled", "uncertain", "released")]


def test_overrun_charged_in_full_and_session_stops(py):
    # R4 §8 test 1: a fake upstream reports usage above the hold. False if `charged` is below the reported cost, or
    # the session's next `Q` is not 402 `budget-overrun`, or CONTROL revoked the session. CONTROL here is the rig's
    # stand-in: the session not revoked = its token still resolves and its status connection is still open, and the
    # refusal is `budget-overrun`, not a token refusal.
    async def go() -> dict:
        async with World(py, [R.usage_response(OVER)]) as w:
            w.up.gates[0].set()
            first = await w.front.q(attempts=5)
            await until(lambda: len(_resolutions(w.keys.ledger_path)) == 1)
            resolves = w.control.resolves
            second = await w.front.q(attempts=5)
            st_r, st_w = w.front.status
            return {"first": first, "second": second, "lines": ledger_lines(w.keys.ledger_path),
                    "conns": w.up.conns, "resolves": (resolves, w.control.resolves),
                    "status_open": not st_r.at_eof() and not st_w.is_closing(), "rc": w.keys.stop()}

    out = asyncio.run(asyncio.wait_for(go(), 120))
    assert out["first"].get("status") == 200, out["first"]
    (res,) = [o for o in out["lines"] if o["kind"] in ("settled", "uncertain", "released")]
    reserved = [o for o in out["lines"] if o["kind"] == "reserved"]
    assert [o["amount"] for o in reserved] == [HOLD]
    # charged in full: the hold settled, the rest booked as overrun, the sum the reported cost
    assert (res["kind"], res["settled"], res["overrun"]) == ("settled", HOLD, OVER_COST - HOLD), res
    assert res["settled"] + res["overrun"] == OVER_COST
    # the session stops: the next `Q` is refused before anything is held or sent
    assert out["second"].get("refused") == "budget-overrun", out["second"]
    assert out["conns"] == 1 and len(reserved) == 1
    # not revoked: the stand-in CONTROL saw no revoke (keys has no wire to it here) and the front is still attached
    assert out["status_open"] is True
    assert out["rc"] == 0


def test_inflight_calls_finish_after_overrun(py):
    # R4 §8 test 2: two exchanges in flight; the first settles over its hold. False if the second is aborted, or does
    # not settle from its own reported usage. A third `Q`, after, is `budget-overrun`.
    async def go() -> dict:
        async with World(py, [R.usage_response(OVER), R.usage_response(UNDER)]) as w:
            t1 = asyncio.ensure_future(w.front.q(attempts=5))
            await until(lambda: w.up.heads == 1)
            t2 = asyncio.ensure_future(w.front.q(attempts=5))
            await until(lambda: w.up.heads == 2)        # both admitted and sent: both in flight
            w.up.gates[0].set()
            first = await asyncio.wait_for(t1, 15)
            await until(lambda: len(_resolutions(w.keys.ledger_path)) == 1)
            mid = {"t2_done": t2.done(), "closed": list(w.up.closed), "res": _resolutions(w.keys.ledger_path)}
            third = await w.front.q(attempts=5)       # the session is stopped while the second is in flight
            w.up.gates[1].set()
            second = await asyncio.wait_for(t2, 15)
            await until(lambda: len(_resolutions(w.keys.ledger_path)) == 2)
            return {"first": first, "second": second, "third": third, "mid": mid, "conns": w.up.conns,
                    "lines": ledger_lines(w.keys.ledger_path), "closed": list(w.up.closed), "rc": w.keys.stop()}

    out = asyncio.run(asyncio.wait_for(go(), 120))
    mid = out["mid"]
    assert mid["t2_done"] is False and mid["closed"] == [False, False], mid      # nothing in flight was aborted
    assert [(o["kind"], o.get("settled"), o.get("overrun")) for o in mid["res"]] == \
        [("settled", HOLD, OVER_COST - HOLD)]
    assert out["third"].get("refused") == "budget-overrun", out["third"]
    assert out["conns"] == 2                                                   # the third reached nothing
    assert out["first"].get("status") == 200 and out["second"].get("status") == 200
    assert [t for t, _ in out["second"]["frames"]][-1] == b"e"                 # relayed to its end
    reserved = {o["seq"]: o for o in out["lines"] if o["kind"] == "reserved"}
    res = [o for o in out["lines"] if o["kind"] in ("settled", "uncertain", "released")]
    assert len(reserved) == 2 and len(res) == 2
    # the second settles from its OWN usage (below its hold: no overrun), never the first's, never `uncertain`
    assert (res[1]["kind"], res[1]["settled"], res[1]["overrun"]) == ("settled", UNDER_COST, 0), res[1]
    assert res[1]["meta"] == {"usage": {"in": 30, "out": 5, "cache_write": 0, "cache_read": 0}}
    assert res[0]["ref"] != res[1]["ref"] and {res[0]["ref"], res[1]["ref"]} == set(reserved)
    assert out["closed"] == [False, False]
    assert out["rc"] == 0


def _response(usage: dict) -> bytes:
    body = json.dumps({"type": "message", "usage": usage}).encode()
    return b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: %d\r\n\r\n" % len(body) + body


async def _overrun(s: "_Sessions", up: _Up) -> list:
    """Session s0's first exchange, answered with usage above its hold: the session is stopped."""
    front, task, got = await s.start(0, _q())
    front.feed_data(gwwire.encode(b"B", BODY) + gwwire.encode(b"E"))
    up.feed(_response(OVER))
    up.feed(b"")
    await asyncio.wait_for(task, 5)
    return await got()


async def _reregister(s: "_Sessions", reg_token: int) -> None:
    """s0's front registers again with a greater `reg_token` (a replaced registration: `_detach` without revoke)."""
    await s.k.front_register(s.k.admin_epoch, "s0", 70000, 1, 1, 1, reg_token)
    await s.k.front_arm(s.k.admin_epoch, "s0", reg_token)
    s.k.regs[70000].state = "claimed"


def _fresh_resolves(s: "_Sessions") -> list:
    """CONTROL's TOKEN-RESOLVE stand-in: every digest answers s0's claims as a FRESH dict, as a real resolve does."""
    calls: list = []

    async def resolve(digest: str) -> tuple:
        calls.append(digest)
        return ("answer", {"session": "s0", "claims": {"routes": ["r"], "front": {"uid": 70000, "pid": 1, "start": 1},
                                                       "incarnation": 1}})
    s.k._token_resolve = resolve
    return calls


def test_budget_stopped_outlives_the_cache_entry(monkeypatch):
    # L3 r1 complement 1 (head ruling 1023+15): the stop is keys' per-SESSION set, not a flag on the claims-cache
    # entry. A re-registration of the session's front (`_detach` without revoke) drops the session's cache entries, and
    # the next `Q` re-resolves a fresh entry from CONTROL: that `Q` is still 402 `budget-overrun`, no `A`, nothing held,
    # no second upstream. The fail-closed clear (`_detach_all`) keeps the set too.
    up = _Up()

    async def go(d: str) -> dict:
        st = _scripted(monkeypatch, up)
        async with _Sessions(d, 1) as s:
            digest = digest_of_token(s.token(0))
            before = dict(s.k.cache[digest])
            first = await _overrun(s, up)
            stopped = set(s.k.budget_stopped)
            await _reregister(s, 2)
            out = {"cached_after_rereg": digest in s.k.cache, "stopped": stopped}
            calls = _fresh_resolves(s)
            front2, task2, got2 = await s.start(0, _q(BODY_Q))
            front2.feed_data(gwwire.encode(b"B", BODY) + gwwire.encode(b"E"))
            await asyncio.wait_for(task2, 5)
            out.update(first=first, second=await got2(), resolves=list(calls), entry=dict(s.k.cache[digest]),
                       before=before, after_rereg=set(s.k.budget_stopped))
            async with s.k.state_lock:
                s.k._detach_all()
            out.update(after_clear=set(s.k.budget_stopped), handshakes=st["handshakes"], path=s.ledger.path)
        out["kinds"] = [o["kind"] for o in file_lines(out["path"])]
        return out

    out = _run(go)
    assert [t for t, _ in out["first"]][:2] == [b"A", b"R"] and out["first"][-1][0] == b"e"
    assert out["stopped"] == {"s0"}
    assert out["cached_after_rereg"] is False          # the re-registration dropped the entry
    assert len(out["resolves"]) == 1                   # the second `Q` re-resolved a fresh one
    assert out["entry"] == out["before"]               # nothing rides on the entry: it is CONTROL's answer, unchanged
    assert [t for t, _ in out["second"]] == [b"F"], out["second"]
    assert json.loads(out["second"][0][1])["refused"] == "budget-overrun"
    assert out["after_rereg"] == {"s0"} and out["after_clear"] == {"s0"}
    assert out["handshakes"] == 1 and out["kinds"].count("reserved") == 1


def test_drop_session_clears_budget_stopped(monkeypatch):
    # DROP-SESSION (`drop_session`) is the one thing that takes a session out of the set. After it the
    # session, registered again with a new token, is served and settles from its own usage.
    up = _Up()

    async def go(d: str) -> dict:
        st = _scripted(monkeypatch, up)
        async with _Sessions(d, 1) as s:
            first = await _overrun(s, up)
            stopped = set(s.k.budget_stopped)
            await s.k.drop_session(s.k.admin_epoch, "s0")
            out = {"first": first, "stopped": stopped, "after_drop": set(s.k.budget_stopped)}
            await _reregister(s, 2)
            _fresh_resolves(s)
            s.token = lambda i: "c" * 43      # the dropped token's digest is `token-revoked`; a new one resolves
            up2 = _Up()
            _scripted(monkeypatch, up2)
            front2, task2, got2 = await s.start(0, _q(BODY_Q))
            front2.feed_data(gwwire.encode(b"B", BODY) + gwwire.encode(b"E"))
            up2.feed(_response(UNDER))
            up2.feed(b"")
            await asyncio.wait_for(task2, 5)
            out.update(second=await got2(), after=set(s.k.budget_stopped), path=s.ledger.path)
        out["res"] = [o for o in file_lines(out["path"]) if o["kind"] in ("settled", "uncertain", "released")]
        return out

    out = _run(go)
    assert out["stopped"] == {"s0"} and out["after_drop"] == set()
    assert [t for t, _ in out["second"]][:2] == [b"A", b"R"] and out["second"][-1][0] == b"e", out["second"]
    assert [(o["kind"], o["overrun"]) for o in out["res"]] == [("settled", OVER_COST - HOLD), ("settled", 0)]
    assert out["res"][1]["settled"] == UNDER_COST
    assert out["after"] == set()


def test_drop_session_after_the_fail_closed_clear_still_clears_budget_stopped(monkeypatch):
    # L3 r2 codex (a MED the r1 fix introduced): `_detach_all` keeps the set but removes every registration, so a
    # DROP-SESSION that arrives before the session registers again finds no registration. It still takes the session
    # out of the set: re-registered, the session's next `Q` is admitted and settles from its own usage.
    up = _Up()

    async def go(d: str) -> dict:
        _scripted(monkeypatch, up)
        async with _Sessions(d, 1) as s:
            await _overrun(s, up)
            async with s.k.state_lock:
                s.k._detach_all()
            out = {"after_clear": set(s.k.budget_stopped), "reg_after_clear": "s0" in s.k.by_session}
            out["dropped"] = await s.k.drop_session(s.k.admin_epoch, "s0")
            out["after_drop"] = set(s.k.budget_stopped)
            await _reregister(s, 2)
            _fresh_resolves(s)
            up2 = _Up()
            _scripted(monkeypatch, up2)
            front2, task2, got2 = await s.start(0, _q(BODY_Q))
            front2.feed_data(gwwire.encode(b"B", BODY) + gwwire.encode(b"E"))
            up2.feed(_response(UNDER))
            up2.feed(b"")
            await asyncio.wait_for(task2, 5)
            out.update(second=await got2(), after=set(s.k.budget_stopped), path=s.ledger.path)
        out["res"] = [o for o in file_lines(out["path"]) if o["kind"] in ("settled", "uncertain", "released")]
        return out

    out = _run(go)
    assert out["after_clear"] == {"s0"} and out["reg_after_clear"] is False
    assert out["dropped"] == {"dropped": False}            # nothing held: still success
    assert out["after_drop"] == set()
    assert [t for t, _ in out["second"]][:2] == [b"A", b"R"] and out["second"][-1][0] == b"e", out["second"]
    assert [(o["kind"], o["overrun"]) for o in out["res"]] == [("settled", OVER_COST - HOLD), ("settled", 0)]
    assert out["res"][1]["settled"] == UNDER_COST and out["after"] == set()
