"""Wave 3 item 11 (per-run keys, per-run dollar caps), read through the 3c rig (`tests/c6_rig.py`): a TICKETED dispatch
run is a session whose token keys resolves to the run (TOKEN-RESOLVE's `run`), whose every hold books under the run's
scope `("run", run_id)` against the run's `reserved_microdollars`, whatever upstream says it spent.

Every party is real (CONTROL with its ticket store and signed route map, keys as a child process on the real ledger)
except the upstream, a plain-TCP fake that reports the usage a test hands it, and the dispatch supervisor and root
(this process). No real key, no VM, no network. The run's cap is the ticket's share; the route carries none (R4 §1.2:
"a dispatch routemap /3 carries no max_call_microdollars").

Undischarged here (3c-10): a real provider call settling from real usage, the real uids, CONTROL as its own process."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys

import pytest

from tests import b2_keys_rig as B2
from tests import c6_rig as R6
from tests.c3_rig import aio_until

pytestmark = [pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets"),
              pytest.mark.skipif(os.geteuid() == 0, reason="keys refuses to run as root (it runs as _levain_keys)"),
              pytest.mark.skipif(not os.path.exists("/usr/bin/ssh-keygen"), reason="no /usr/bin/ssh-keygen")]

SYS_PY = "/usr/bin/python3"
PYS = [sys.executable] + ([SYS_PY] if os.path.exists(SYS_PY) and sys.executable != SYS_PY else [])

#: the rig route's price (`tests/test_routemaps.PRICE`): 8 microdollars per output token, 2 per input token
OUT_RATE, IN_RATE = 8, 2


@pytest.fixture(params=PYS, ids=lambda p: "usr-bin-python3" if p == SYS_PY else "venv")
def py(request):
    B2.require_h11(request.param)
    return request.param


def go(fn, timeout: float = 150.0):
    return asyncio.run(asyncio.wait_for(fn(), timeout))


def reply(input_tokens: int, output_tokens: int) -> bytes:
    """`c6_rig.response()` (the checked model answers) with the usage the test names."""
    body = json.dumps({"type": "message", "model": R6.MODEL, "provider": "Z.AI",
                       "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}).encode()
    return (b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: %d\r\nconnection: close\r\n\r\n"
            % len(body) + body)


def reserved_lines(w: R6.World) -> list:
    return [o for o in w.ledger() if o["kind"] == "reserved"]


def test_a_run_that_spends_past_its_share_is_charged_in_full_and_its_next_call_refused(py):
    # The reproduced failure (red before the fix, if there is one): a ticketed run reserved $1.00 against a fake
    # upstream whose answer reports $2.40 of usage. False if the settle books less than the reported cost, if the
    # run's next call is admitted, or if the hold was taken under any scope but the run's.
    share = 1_000_000
    out_tokens = 300_000
    cost = out_tokens * OUT_RATE + 3 * IN_RATE

    async def fn():
        async with R6.World(py) as w:
            w.up.response = reply(3, out_tokens)
            await w.start_keys()
            run, f = await w.run(reserved=share)
            first = await f.q()
            await aio_until(lambda: any(o["kind"] in ("settled", "uncertain", "released") for o in w.ledger()), 20,
                            "the settle line")
            second = await f.q()
            return first, second, run.run_id, w.ledger(), w.up.conns, w.stop_keys()

    first, second, run_id, lines, conns, rc = go(fn)
    assert first.get("status") == 200, first
    (hold,) = [o for o in lines if o["kind"] == "reserved"]
    assert hold["scope"] == ["run", run_id] and hold["cap"] == share, hold
    (res,) = [o for o in lines if o["kind"] in ("settled", "uncertain", "released")]
    assert res["kind"] == "settled" and res["overrun"] > 0, res
    assert res["settled"] + res["overrun"] == cost, (res, cost)
    assert second.get("refused") == "budget-overrun", second
    assert conns == 1, "the refused call was sent upstream"
    assert rc == 0


def test_a_run_cannot_be_admitted_past_its_share(py):
    # R4 §8 reservation test, on the run's scope: holds in flight count against the share. The share fits two of the
    # rig body's holds and not three; the third call is 402 `budget-exhausted` and never leaves keys.
    async def fn():
        async with R6.World(py, hold=True) as w:
            await w.start_keys()
            _, probe = await w.run("s-00000000000000a9", "a-probe", reserved=10_000)
            t = asyncio.create_task(probe.q())
            await aio_until(lambda: w.up.heads == 1, 20, "the probe reached the upstream")
            (one,) = reserved_lines(w)
            w.up.release.set()
            await t
            hold = one["amount"]
            share = hold * 2 + hold // 2          # two holds fit, a third does not
            w.up.release.clear()
            run, f = await w.run("s-00000000000000a1", "a-1", reserved=share)
            before = w.up.heads
            t1, t2 = asyncio.create_task(f.q()), asyncio.create_task(f.q())
            await aio_until(lambda: w.up.heads == before + 2, 20, "two holds in flight")
            third = await f.q()
            sent = w.up.heads
            w.up.release.set()
            return third, sent - before, [await t1, await t2], run.run_id, w.ledger(), w.stop_keys()

    third, sent, two, run_id, lines, rc = go(fn)
    assert third.get("refused") == "budget-exhausted", third
    assert sent == 2, "the refused third call reached the upstream"
    assert [a.get("status") for a in two] == [200, 200], two
    mine = [o for o in lines if o["kind"] == "reserved" and o["scope"] == ["run", run_id]]
    assert len(mine) == 2, mine
    assert rc == 0


def test_one_runs_overrun_does_not_touch_another_runs_share(py):
    # per-run keys: the token names ONE run. Run A overruns and stops; run B, minted under its own ticket on the same
    # keys, is admitted and books under its own scope; A stays stopped.
    async def fn():
        async with R6.World(py) as w:
            await w.start_keys()
            # sequential: the rig's fronts are all this process, and a later run's claim displaces an earlier one's
            a_run, a = await w.run("s-00000000000000a1", "a-1", reserved=1_000_000)
            w.up.response = reply(3, 300_000)
            first_a = await a.q()
            await aio_until(lambda: any(o["kind"] in ("settled", "uncertain", "released") for o in w.ledger()), 20,
                            "A's settle line")
            second_a = await a.q()
            b_run, b = await w.run("s-00000000000000a2", "a-2", reserved=1_000_000)
            w.up.response = R6.response()
            first_b = await b.q()
            return first_a, first_b, second_a, a_run.run_id, b_run.run_id, w.ledger(), w.stop_keys()

    first_a, first_b, second_a, a_id, b_id, lines, rc = go(fn)
    assert first_a.get("status") == 200 and first_b.get("status") == 200, (first_a, first_b)
    assert second_a.get("refused") == "budget-overrun", second_a
    scopes = sorted(o["scope"][1] for o in lines if o["kind"] == "reserved")
    assert scopes == sorted([a_id, b_id]), scopes
    assert rc == 0
