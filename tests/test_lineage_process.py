"""S1 3c-6d: the lineage crash proofs as PROCESS runs (3c design r3 §2.4 "The crash table" read through R4 §3: seven
rows, "one fault-gate run per keys row"; r3 §2.2's kill -9 row, invariant 3; head rulings H6-9, H6-12 in
`1024-13/3c6/HEAD_RULINGS_3C6.md`). Rig: `tests/c6_rig.py` (real CONTROL with its sink and ticket store on a thread,
keys a child process SIGKILLed at a ledger writer gate, a real ticketed run).

The steps for one ref r: (1) keys fsyncs the resolution line with its row; (2) keys sends `LINEAGE-APPEND r`; (3)
CONTROL appends + fsyncs; (4) CONTROL acks; (5) keys submits `lineage-acked [r]`; (6) that line is fsynced and r
counts as acked. Keys rows (each one SIGKILL at one gate, CONTROL up): before 1 · after 1, before 3 (=
`test_lineage_survives_kill9`) · after 3, before 6 · after 6; plus the compound fault
`test_lineage_ack_lost_before_fsync_control_up`. The CONTROL rows ("after 2, before 3", "after 3, before 4") are
proved at host level with lineage.py's fault seam (H6-9: `tests/test_lineage.py`); "both, any point" is run here once
(keys killed after 3 before 6, CONTROL restarted from its sink). CONTROL as its own process: 3c-10.

A SIGKILL keeps the page cache (3c-6a RETURN obs 2, H6-26): a line written before the kill is in the file after it,
flushed or not. The power cut that loses an unflushed line is emulated by truncating the file to the size the gate
recorded before that batch."""
from __future__ import annotations

import asyncio
import os
import socket
import sys
from collections import Counter

import pytest

from tests import b2_keys_rig as B2
from tests import c6_rig as R6
from tests.c3_rig import aio_until

pytestmark = [pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets"),
              pytest.mark.skipif(os.geteuid() == 0, reason="keys refuses to run as root (it runs as _levain_keys)"),
              pytest.mark.skipif(not os.path.exists("/usr/bin/ssh-keygen"), reason="no /usr/bin/ssh-keygen")]

SYS_PY = "/usr/bin/python3"
PYS = [sys.executable] + ([SYS_PY] if os.path.exists(SYS_PY) and sys.executable != SYS_PY else [])


@pytest.fixture(params=PYS, ids=lambda p: "usr-bin-python3" if p == SYS_PY else "venv")
def py(request):
    B2.require_h11(request.param)
    return request.param


def go(fn, timeout: float = 150.0):
    return asyncio.run(asyncio.wait_for(fn(), timeout))


def _kinds(w: R6.World) -> list:
    return [o["kind"] for o in w.ledger()]


def _ticketed(w: R6.World) -> list:
    """The ticketed holds' `reserved` lines (their meta carries `lineage_base`)."""
    return [o for o in w.ledger() if o["kind"] == "reserved" and "lineage_base" in o.get("meta", {})]


def test_one_ticketed_exchange_reaches_controls_sink_once(py):
    # the rig's own baseline: a ticketed `Q` through real CONTROL and keys settles, its row reaches the sink once
    async def fn():
        async with R6.World(py) as w:
            await w.start_keys()
            run, f = await w.run()
            ans = await f.q()
            await aio_until(lambda: len(w.rows()) == 1, 20, "the row reached the sink")
            await aio_until(lambda: "lineage-acked" in _kinds(w), 20, "keys' ack line")
            return ans, run.run_id, w.rows(), w.received, w.ledger(), w.stop_keys()

    ans, run_id, rows, received, lines, rc = go(fn)
    assert ans.get("status") == 200, ans
    (row,) = rows
    assert row["settled"] == "yes" and row["run_id"] == run_id and row["model_answered"] == R6.MODEL
    assert "quarantined" not in row and len(received) == 1 and rc == 0


# ---- helpers for the crash rows -------------------------------------------------------------------------------------

def ticketed_triples(lines: list) -> list:
    """Each ticketed hold's triple `(ledger_id, epoch of its reserved line, its seq)`, in file order."""
    lid, epoch, out = None, None, []
    for o in lines:
        if o["kind"] == "header":
            lid = o["ledger_id"]
        elif o["kind"] == "opened":
            epoch = o["epoch"]
        elif o["kind"] == "reserved" and "lineage_base" in o.get("meta", {}):
            out.append((lid, epoch, o["seq"]))
    return out


def acked_triples(lines: list) -> list:
    return [R6.triple_of(t) for o in lines if o["kind"] == "lineage-acked" for t in o["refs"]]


async def exchange(f) -> dict:
    ans = await f.q()
    assert ans.get("status") == 200, ans
    return ans


async def sync_run(w: R6.World, n: int) -> str:
    """A fresh ticketed run's one exchange after a restart, and its row at the sink: the sender works the outbox in
    ref order (the resend on attach open first), so once this row is in, any resend of an older ref has happened."""
    run, f = await w.run("s-00000000000000b%d" % n, "a-sync-%d" % n)
    await exchange(f)
    await aio_until(lambda: any(r.get("run_id") == run.run_id for r in w.rows()), 20, "the sync run's row")
    await aio_until(lambda: set(ticketed_triples(w.ledger())) <= set(acked_triples(w.ledger())), 20,
                    "keys' ack lines for every ticketed ref")
    return run.run_id


def one_row_each(w: R6.World) -> dict:
    """Exactly one row at CONTROL for every ticketed exchange in keys' ledger, none quarantined (invariant 3)."""
    by = w.rows_by_triple()
    tt = ticketed_triples(w.ledger())
    assert sorted(by) == sorted(tt), (sorted(by), tt)
    assert all(len(v) == 1 for v in by.values()), {k: len(v) for k, v in by.items()}
    assert all("quarantined" not in v[0] for v in by.values()), by
    return {k: v[0] for k, v in by.items()}


# ---- the keys rows of the crash table: one SIGKILL at one writer gate each, CONTROL up ------------------------------

def test_lineage_crash_keys_before_the_resolution_line(py):
    # row "keys before 1": the settle's line never reaches the file. The `reserved` line survives; replay writes its
    # `uncertain {restart}` line WITH the row (built from `lineage_base`, the hold's OLD epoch, H6-12); it is sent on
    # the new attachment and appended: one row, `settled: uncertain`
    async def fn():
        async with R6.World(py) as w:
            await w.start_keys("w1:settled")
            run, f = await w.run()
            t = asyncio.ensure_future(f.q())
            await w.kill_at_gate()
            await asyncio.gather(t, return_exceptions=True)
            after_kill = (w.ledger(), list(w.received), w.rows())
            await w.start_keys()
            await sync_run(w, 1)
            return run.run_id, after_kill, w.ledger(), list(w.received), one_row_each(w), w.stop_keys()

    run_id, (k_lines, k_recv, k_rows), lines, received, rows, rc = go(fn)
    assert "settled" not in [o["kind"] for o in k_lines] and k_recv == [] and k_rows == []
    r = ticketed_triples(k_lines)[0]
    restart = [o for o in lines if o["kind"] == "uncertain" and o["ref"] == r[2]]
    assert [o["reason"] for o in restart] == ["restart"]
    assert rows[r]["settled"] == "uncertain" and rows[r]["run_id"] == run_id
    assert Counter(received)[r] == 1 and rc == 0


def test_lineage_survives_kill9(py):
    # row "keys after 1, before 3": SIGKILL keys after the `settled` line's fsync, before keys counted it (the
    # writer's durable callback never ran, so nothing was sent): the row reaches CONTROL after restart, exactly once
    async def fn():
        async with R6.World(py) as w:
            await w.start_keys("w3:settled")
            run, f = await w.run()
            t = asyncio.ensure_future(f.q())
            await w.kill_at_gate()
            await asyncio.gather(t, return_exceptions=True)
            after_kill = (w.ledger(), list(w.received), w.rows())
            await w.start_keys()
            await sync_run(w, 1)
            return run.run_id, after_kill, list(w.received), one_row_each(w), w.stop_keys()

    run_id, (k_lines, k_recv, k_rows), received, rows, rc = go(fn)
    (settled,) = [o for o in k_lines if o["kind"] == "settled"]
    assert "lineage" in settled["meta"] and k_recv == [] and k_rows == []
    r = ticketed_triples(k_lines)[0]
    assert rows[r]["settled"] == "yes" and rows[r]["model_answered"] == R6.MODEL and rows[r]["run_id"] == run_id
    assert Counter(received)[r] == 1 and rc == 0


def test_lineage_crash_keys_after_controls_append_before_the_ack_line(py):
    # row "keys after 3, before 6" (the r2 hole): CONTROL appended and acked, keys' `lineage-acked` line never
    # reached the file. r is unacked at keys and a member at CONTROL: resent on the new attachment, acked by
    # membership WITHOUT appending: received twice, one row
    async def fn():
        async with R6.World(py) as w:
            await w.start_keys("w1:lineage-acked")
            run, f = await w.run()
            await exchange(f)
            await w.kill_at_gate()
            after_kill = (w.ledger(), list(w.received), len(w.rows()))
            await w.start_keys()
            await sync_run(w, 1)
            return after_kill, list(w.received), one_row_each(w), w.stop_keys()

    (k_lines, k_recv, k_rows), received, rows, rc = go(fn)
    r = ticketed_triples(k_lines)[0]
    assert "lineage-acked" not in [o["kind"] for o in k_lines] and k_recv == [r] and k_rows == 1
    assert Counter(received)[r] == 2 and rows[r]["settled"] == "yes" and rc == 0


def test_lineage_crash_keys_after_the_ack_line_is_durable(py):
    # row "keys after 6": the `lineage-acked` line is flushed (the kill lands before keys' own callback counted it):
    # r is durably acked, so the restarted keys never sends it again
    async def fn():
        async with R6.World(py) as w:
            await w.start_keys("w3:lineage-acked")
            run, f = await w.run()
            await exchange(f)
            await w.kill_at_gate()
            after_kill = (w.ledger(), list(w.received))
            await w.start_keys()
            await sync_run(w, 1)
            return after_kill, list(w.received), one_row_each(w), w.stop_keys()

    (k_lines, k_recv), received, rows, rc = go(fn)
    r = ticketed_triples(k_lines)[0]
    assert acked_triples(k_lines) == [r] and k_recv == [r]
    assert Counter(received)[r] == 1 and len(received) == 2 and rc == 0


@pytest.mark.parametrize("loss", ["sigkill", "power-cut"])
def test_lineage_ack_lost_before_fsync_control_up(py, loss):
    # the compound fault (r3 §2.4, R4 §3 drops "after one attach that pruned"): three ticketed exchanges at once,
    # keys SIGKILLed with its writer between the `lineage-acked` line's write and its fsync, CONTROL alive. "sigkill":
    # the kill keeps the page cache, so the ack line replays as acked (H6-26). "power-cut": the file is cut to its
    # size before that batch, so the ack is lost and every ref it named is resent and acked by membership. Either
    # way: exactly one row for each ref, and keys' outbox drains
    async def fn():
        async with R6.World(py) as w:
            await w.start_keys("w2:lineage-acked")
            run, f = await w.run()
            ts = [asyncio.ensure_future(f.q()) for _ in range(3)]
            ev = await w.kill_at_gate()
            await asyncio.gather(*ts, return_exceptions=True)
            k_lines = w.ledger()
            if loss == "power-cut":
                os.truncate(w.ledger_path, ev["size"])
            cut_lines = w.ledger()
            k_recv = list(w.received)
            await w.start_keys()
            await sync_run(w, 1)
            return k_lines, cut_lines, k_recv, list(w.received), one_row_each(w), w.stop_keys()

    k_lines, cut_lines, k_recv, received, rows, rc = go(fn)
    assert "lineage-acked" in [o["kind"] for o in k_lines]          # written before the kill: in the page cache
    lost = set(acked_triples(k_lines)) - set(acked_triples(cut_lines))
    assert bool(lost) == (loss == "power-cut")
    assert lost <= set(k_recv)                                         # each lost ack was for a row CONTROL holds
    assert all(Counter(received)[t] == 2 for t in lost), (lost, received)
    assert len(rows) == 4 and rc == 0                                  # three exchanges + the sync run, one row each


def test_lineage_crash_both_keys_and_control(py):
    # row "both, any point": keys killed after CONTROL's append, before its ack line (as above) AND CONTROL stopped
    # (a stop is a crash to every peer) and restarted on its sink: CONTROL rebuilds membership from the sink, so
    # the resend is acked without appending: one row
    async def fn():
        async with R6.World(py) as w:
            await w.start_keys("w1:lineage-acked")
            run, f = await w.run()
            await exchange(f)
            await w.kill_at_gate()
            k_lines = w.ledger()
            await asyncio.get_running_loop().run_in_executor(None, w.ctl.stop)
            w.start_control()
            await w.start_keys()
            await sync_run(w, 1)
            return k_lines, list(w.received), one_row_each(w), w.stop_keys()

    k_lines, received, rows, rc = go(fn)
    r = ticketed_triples(k_lines)[0]
    assert Counter(received)[r] == 2 and rows[r]["settled"] == "yes" and rc == 0


# ---- r3 §2.2 kill -9, invariant 3 (the ticketed form of test_broker_keys_process::test_keys_restart_keeps_ledger) --

def test_keys_restart_keeps_ledger_lineage_invariant_3(py):
    # SIGKILL keys mid-traffic (no gate) with one ticketed exchange settled and two held at upstream, restart it on
    # the same lib root with CONTROL up: I2 (every line durable before the kill is unchanged after it), I3 (one
    # resolution per reserved line) and invariant 3: every ticketed exchange ends with EXACTLY ONE lineage row at
    # CONTROL (the settled one "yes", the two restarted "uncertain"), and keys' outbox drains
    async def fn():
        async with R6.World(py) as w:
            await w.start_keys()
            run, f = await w.run()
            await exchange(f)
            await aio_until(lambda: len(acked_triples(w.ledger())) == 1, 20, "the first row acked")
            w.up.release.clear()
            held = [asyncio.ensure_future(f.q()) for _ in range(2)]
            await aio_until(lambda: w.up.heads >= 3 and len(ticketed_triples(w.ledger())) == 3, 20, "two held")
            before = open(w.ledger_path, "rb").read()
            w.keys.kill()
            await asyncio.gather(*held, return_exceptions=True)
            w.up.release.set()
            await w.start_keys()
            await aio_until(lambda: len(w.rows()) == 3, 20, "three rows")
            await sync_run(w, 1)
            return before, open(w.ledger_path, "rb").read(), w.ledger(), list(w.received), one_row_each(w), \
                w.stop_keys()

    before, after, lines, received, rows, rc = go(fn)
    assert before.endswith(b"\n") and after.startswith(before)                    # I2
    tt = ticketed_triples(lines)
    res = [o for o in lines if o["kind"] in ("released", "settled", "uncertain")]
    assert sorted(o["ref"] for o in res) == sorted(t[2] for t in tt)               # I3
    first3 = tt[:3]
    assert [rows[t]["settled"] for t in first3].count("yes") == 1
    assert [rows[t]["settled"] for t in first3].count("uncertain") == 2
    assert all(Counter(received)[t] == 1 for t in tt), received                   # each row sent once
    assert rc == 0
