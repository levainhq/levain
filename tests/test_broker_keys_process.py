"""keys as a real child process on the real ledger file (S1 3c-1 session B2, design `1023-1/3c/C1_DESIGN.md` §7 T12 and
T13). The child is `tests/b2_keys_driver.py`: `levain.broker.keys.main` with the launcher's argv, its upstream seam
pointed at a plain-TCP fake. This process is the front and CONTROL (`tests/b2_keys_rig.py`).

Undischarged here (design §8, the VM): the real `_levain_keys` / `_levain_gw<n>` uids, TLS to a real upstream, a power
cut. keys refuses uid 0, so these tests skip as root."""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys

import pytest

from tests import b2_keys_rig as R
from tests.b2_keys_rig import FakeControl, FakeUpstream, Front, KeysProc, ledger_lines

pytestmark = [pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets"),
              pytest.mark.skipif(os.geteuid() == 0, reason="keys refuses to run as root (it runs as _levain_keys)")]

SYS_PY = "/usr/bin/python3"
PYS = [sys.executable] + ([SYS_PY] if os.path.exists(SYS_PY) and sys.executable != SYS_PY else [])
CAP = 3000
MAX_CALL = CAP // 3
N = 20
WAVE = 5            # keys' PENDING_PER_PEER is 8: connections opened faster than keys reads their `Q` are closed unread


@pytest.fixture(params=PYS, ids=lambda p: "usr-bin-python3" if p == SYS_PY else "venv")
def py(request):
    if not R.h11_available(request.param):
        pytest.skip("h11 is not importable by %s (named skip, not a pass)" % request.param)
    return request.param


class World:
    def __init__(self, py: str, cap: int = CAP, max_call: int = MAX_CALL, hold: bool = True) -> None:
        self.py, self.cap, self.max_call, self.hold = py, cap, max_call, hold

    async def __aenter__(self) -> "World":
        self.d = R.short_dir()
        R.lib(self.d, self.cap)
        self.control = FakeControl(os.path.join(self.d, "control.sock"))
        await self.control.start()
        self.up = await FakeUpstream().start(self.hold)
        self.keys = KeysProc(self.d, self.py, self.up.port)
        self.front = None
        return self

    async def boot(self) -> None:
        await self.keys.start()
        self.front = await Front(self.keys).claim()

    async def reboot(self) -> None:
        """A restart after a crash: the front is gone with the old keys, the stale socket paths are still there."""
        await self.front.close()
        await self.boot()

    async def __aexit__(self, *exc) -> None:
        if self.front is not None:
            await self.front.close()
        self.keys.cleanup()
        await self.up.close()
        await self.control.close()
        shutil.rmtree(self.d, ignore_errors=True)


async def until(cond, timeout: float = 15.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < end, "condition not met within %s s" % timeout
        await asyncio.sleep(0.02)


def _kinds(path: str) -> list:
    return [o["kind"] for o in ledger_lines(path)]


# ---- T12 ------------------------------------------------------------------------------------------------------------

def test_concurrent_streams_cannot_overshoot(py):
    # [prescription: reasoned, now a RUN] one route, max_call = cap / 3, N = 20 concurrent `Q`s on an upstream holding
    # every response open: exactly 3 reach upstream and 17 get 402 (`budget-exhausted`). When the 3 finish they settle
    # at the full amount, and the next `Q` is refused.
    async def go() -> dict:
        async with World(py) as w:
            await w.boot()
            tasks = []
            for i in range(N):
                tasks.append(asyncio.ensure_future(w.front.q(attempts=20)))
                if (i + 1) % WAVE == 0:
                    await asyncio.sleep(0.15)
            await until(lambda: w.up.heads >= 3)
            await until(lambda: sum(1 for t in tasks if t.done()) >= N - 3)
            await asyncio.sleep(0.5)           # a fourth, wrongly admitted, would have connected by now
            mid = {"conns": w.up.conns, "heads": w.up.heads, "done": sum(1 for t in tasks if t.done()),
                   "lines": [(o["kind"], o.get("amount")) for o in ledger_lines(w.keys.ledger_path)]}
            w.up.release.set()
            res = await asyncio.wait_for(asyncio.gather(*tasks), 30)
            after = await w.front.q(attempts=5)
            await until(lambda: _kinds(w.keys.ledger_path).count("settled") == 3)
            return {"mid": mid, "res": res, "after": after, "conns": w.up.conns,
                    "lines": ledger_lines(w.keys.ledger_path), "rc": w.keys.stop()}

    out = asyncio.run(asyncio.wait_for(go(), 120))
    mid, res = out["mid"], out["res"]
    refused = [r for r in res if r.get("refused")]
    ok = [r for r in res if r.get("status") == 200]
    assert len(ok) == 3 and len(refused) == N - 3, [(r.get("status"), r.get("refused")) for r in res]
    assert all(r["refused"] == "budget-exhausted" for r in refused), [r["refused"] for r in refused]
    assert (mid["conns"], mid["heads"]) == (3, 3), mid            # exactly 3 reached upstream while all were held
    assert out["conns"] == 3
    assert mid["lines"].count(("reserved", MAX_CALL)) == 3 and len(mid["lines"]) == 2 + 3, mid["lines"]
    kinds = [o["kind"] for o in out["lines"]]
    assert kinds.count("reserved") == 3 and kinds.count("settled") == 3, kinds
    assert [o["settled"] for o in out["lines"] if o["kind"] == "settled"] == [MAX_CALL] * 3
    assert out["after"].get("refused") == "budget-exhausted", out["after"]    # settled at full: the cap is spent
    assert out["rc"] == 0


# ---- T13 ------------------------------------------------------------------------------------------------------------

def test_keys_restart_keeps_ledger(py):
    # SIGKILL keys mid-traffic and restart it on the same lib root: every line durable before the kill is in the file
    # after it, unchanged (I2); each open hold comes back `uncertain {restart}` exactly once (I3); and the booked money
    # is real: the route's cap is spent, so the next `Q` is refused.
    async def go() -> dict:
        async with World(py, hold=False) as w:
            await w.boot()
            first = await w.front.q(attempts=5)                      # one completes: reserved + settled
            assert first.get("status") == 200, first
            await until(lambda: _kinds(w.keys.ledger_path).count("settled") == 1)
            w.up.release.clear()
            held = [asyncio.ensure_future(w.front.q(attempts=5)) for _ in range(2)]
            await until(lambda: w.up.heads >= 2 + 1)
            await until(lambda: _kinds(w.keys.ledger_path).count("reserved") == 3)
            before = open(w.keys.ledger_path, "rb").read()
            w.keys.kill()
            await asyncio.gather(*held, return_exceptions=True)
            w.up.release.set()
            await w.reboot()
            after = open(w.keys.ledger_path, "rb").read()
            lines = ledger_lines(w.keys.ledger_path)
            third = await w.front.q(attempts=5)
            rc = w.keys.stop()
            return {"before": before, "after": after, "lines": lines, "third": third, "rc": rc,
                    "err": list(w.keys.err)}

    out = asyncio.run(asyncio.wait_for(go(), 120))
    before, after, lines = out["before"], out["after"], out["lines"]
    assert before.endswith(b"\n")
    assert after.startswith(before), "a line durable before the kill changed or went (I2)"
    reserved = [o["seq"] for o in lines if o["kind"] == "reserved"]
    assert len(reserved) == 3
    res = [(o["kind"], o["ref"], o.get("reason", o.get("settled"))) for o in lines
           if o["kind"] in ("released", "settled", "uncertain")]
    assert sorted(r[1] for r in res) == sorted(reserved), res        # I3: one resolution per reserved line
    assert res[0][:1] == ("settled",) and res[0][2] == MAX_CALL
    assert sorted((r[0], r[2]) for r in res[1:]) == [("uncertain", "restart")] * 2, res
    assert [o["kind"] for o in lines].count("opened") == 2           # the first start's, and the restart's
    assert out["third"].get("refused") == "budget-exhausted", out["third"]   # 1 settled + 2 uncertain = the cap
    assert out["rc"] == 0
