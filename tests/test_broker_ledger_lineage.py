"""The reservation ledger `/3` (S1 3c-6a; R4 `1023-2/3c/R4_DESIGN.md` §3, rulings `1024-13/3c6/HEAD_RULINGS_3C6.md`
H6-5, H6-10, H6-12, H6-17): the `/3` header and the reader of `/2` and `/3`, the `refused` and `lineage-acked` line
kinds and their replay rules, `released` with `meta`, the restart line's lineage payload built from `lineage_base`,
and the outbox (`unacked()` in ref order, `ack()` counted only after its line's flush).

The lineage payload is a seam until 3c-6c: a dict at `meta["lineage"]` on a resolution or `refused` line, built for a
ledger-written resolution from the `reserved` line's `meta["lineage_base"]`."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List

import pytest

from levain.broker import ledger as L
from tests.ledger_support import (AMOUNT, CAP, E1, E2, LID, SCOPE, FlushCtl, Owner, file_lines, kinds, ledger_path,
                                  line, reserved, until, write_file)

ROOT = Path(__file__).resolve().parents[1]
DRIVER = str(ROOT / "tests" / "b2_ledger_driver.py")
SYS_PY = "/usr/bin/python3"
INTERPRETERS = [sys.executable] + ([SYS_PY] if os.path.exists(SYS_PY) and sys.executable != SYS_PY else [])
V2 = "levain-reservation-ledger/2"
V3 = "levain-reservation-ledger/3"
BASE = {"run_id": "r-0123456789abcdef", "route": "r-a", "model_requested": "m-1", "hold_microdollars": AMOUNT}


def header(fmt: str = V3) -> bytes:
    return line("header", 0, format=fmt, ledger_id=LID, created_at="2026-10-23T01:02:03Z")


def opened(seq, epoch=E1):
    return line("opened", seq, epoch=epoch)


def tri(ref, epoch=E1, ledger=LID) -> dict:
    return {"ledger": ledger, "epoch": epoch, "ref": ref}


def acked(seq, *refs, quarantined=None):
    f = {"refs": list(refs)}
    if quarantined is not None:
        f["quarantined"] = quarantined
    return line("lineage-acked", seq, **f)


def refused_line(seq, epoch=E1, count=3, meta=None, run_id="r-0123456789abcdef"):
    return line("refused", seq, epoch=epoch, session="s1", run_id=run_id, route="r-a", reason="budget-exhausted",
                count=count, meta={"lineage": {"refused": "budget-exhausted"}} if meta is None else meta)


def _refusal(path) -> L.LedgerOpenError:
    with pytest.raises(L.LedgerOpenError) as ei:
        L.Ledger.open(path)
    return ei.value


def _close(led) -> None:
    asyncio.run(led.aclose(0))


async def _sent_hold(led, meta=None):
    o = await Owner(led, meta=meta).start()
    await o.hold.durable()
    o.hold.send_gate()
    o.hold.mark_sent()
    return o


# ---- the header and the `/2` reader ------------------------------------------------------------------------------

def test_a_new_file_is_v3(tmp_path):
    path = ledger_path(tmp_path)
    led = L.Ledger.open(path)
    assert L.FORMAT == V3 and L.FORMAT_VERSION == 3
    assert file_lines(path)[0]["format"] == V3
    _close(led)


def test_a_v2_file_is_opened_by_the_v3_reader(tmp_path):
    """R4 §5's DONE: a `/2` file (its `released` line has no `meta`; a ticketed session's payload rides in the `/2`
    lines' `meta`, R4 §10) opens, keeps its header, replays every term, and its payload lines are the outbox."""
    path = ledger_path(tmp_path)
    pay = {"settled": "yes", "run_id": BASE["run_id"]}
    write_file(path, header(V2), opened(1), reserved(2), reserved(3), reserved(4, meta={"lineage_base": BASE}),
               line("released", 5, ref=2),
               line("settled", 6, ref=3, settled=AMOUNT, overrun=0, meta={"usage": {"in": 1}, "lineage": pay}))
    before = open(path, "rb").read()
    led = L.Ledger.open(path)
    after = open(path, "rb").read()
    assert after.startswith(before)                                  # nothing rewritten, the `/2` header kept
    assert file_lines(path)[0]["format"] == V2
    t = led.totals(SCOPE)
    assert (t.settled, t.uncertain_restart, t.outstanding) == (AMOUNT, AMOUNT, 0)
    new = file_lines(path)[7:]
    assert [(o["kind"], o.get("ref")) for o in new] == [("opened", None), ("uncertain", 4)]
    assert [(r, p) for r, p in led.unacked()] == [
        ((LID, E1, 3), pay), ((LID, E1, 4), L.lineage_payload(BASE, L.UNCERTAIN, new[1]["at"]))]
    _close(led)
    led = L.Ledger.open(path)                                        # and the mixed file reopens under `/3`
    assert [r for r, _ in led.unacked()] == [(LID, E1, 3), (LID, E1, 4)]
    _close(led)


@pytest.mark.parametrize("fmt", ["levain-reservation-ledger/1", "levain-reservation-ledger/02"])
def test_only_v2_and_v3_headers_are_read(tmp_path, fmt):
    path = ledger_path(tmp_path)
    write_file(path, header(fmt))
    assert _refusal(path).name == "ledger-corrupt:type:1"


def test_v4_is_newer(tmp_path):
    path = ledger_path(tmp_path)
    write_file(path, header("levain-reservation-ledger/4"))
    assert _refusal(path).name == "ledger-format-newer"


# ---- replay rules for the new kinds --------------------------------------------------------------------------------

PAY = {"lineage": {"settled": "yes"}}

CORRUPT = [
    # an ack naming a ref with no payload line before it is `ledger-corrupt` (R4 §3)
    ("ack-orphan", [header(), opened(1), reserved(2), line("settled", 3, ref=2, settled=1, overrun=0, meta={}),
                    acked(4, tri(2))], 5),                                         # resolved, no payload
    ("ack-orphan", [header(), opened(1), reserved(2), acked(3, tri(2))], 4),        # not resolved yet
    ("ack-orphan", [header(), opened(1), acked(2, tri(9))], 3),                      # no such ref
    ("ack-orphan", [header(), opened(1), reserved(2), line("settled", 3, ref=2, settled=1, overrun=0, meta=PAY),
                    acked(4, tri(2, ledger="d" * 32))], 5),                         # another ledger's triple
    ("ack-orphan", [header(), opened(1), reserved(2), line("settled", 3, ref=2, settled=1, overrun=0, meta=PAY),
                    acked(4, tri(2, epoch=E2))], 5),                                # the right ref, another epoch
    ("ack-orphan", [header(), opened(1), reserved(2), line("settled", 3, ref=2, settled=1, overrun=0, meta=PAY),
                    acked(4, tri(3))], 5),                                          # the resolution's own seq
    ("ack-orphan", [header(), opened(1), refused_line(2), acked(3, tri(2), tri(5)), refused_line(5)], 4),
    ("ack-quarantine", [header(), opened(1), refused_line(2), refused_line(3),
                        acked(4, tri(2), quarantined=[{"source": tri(3), "field": "tokens_in"}])], 5),
    ("key", [header(), opened(1), refused_line(2), line("lineage-acked", 3, refs=[tri(2)], quarantined=[], x=1)], 4),
    ("key", [header(), opened(1), line("refused", 2, session="s1", run_id=None, route="r-a", reason="x", count=1,
                                       meta={})], 3),                              # no epoch (H6-10)
    ("type", [header(), opened(1), refused_line(2), acked(3)], 4),                  # no refs
    ("type", [header(), opened(1), refused_line(2), line("lineage-acked", 3, refs=[[LID, E1, 2]])], 4),
    ("type", [header(), opened(1), refused_line(2), line("lineage-acked", 3, refs=[dict(tri(2), x=1)])], 4),
    ("type", [header(), opened(1), refused_line(2), line("lineage-acked", 3, refs=[tri(True)])], 4),
    ("type", [header(), opened(1), refused_line(2), acked(3, tri(2), quarantined=[])], 4),
    ("type", [header(), opened(1), refused_line(2), acked(3, tri(2), quarantined={"2": "tokens_in"})], 4),
    ("type", [header(), opened(1), refused_line(2), acked(3, tri(2), quarantined=[{"source": tri(2), "field": ""}])],
     4),
    ("type", [header(), opened(1), refused_line(2, count=True)], 3),
    ("type", [header(), opened(1), refused_line(2, run_id="")], 3),
    ("type", [header(), opened(1), refused_line(2, meta={"lineage": "x"})], 3),
    ("type", [header(), opened(1), reserved(2, meta={"lineage_base": ["x"]})], 3),
    ("type", [header(), opened(1), reserved(2, meta={"lineage_base": {"x": "y" * (L.META_MAX - 40)}})], 3),
    ("amount", [header(), opened(1), refused_line(2, count=0)], 3),
    ("epoch", [header(), opened(1, E1), opened(2, E2), refused_line(3, epoch=E1)], 4),
    ("epoch", [header(), refused_line(1)], 2),
]


@pytest.mark.parametrize("what,lines,n", CORRUPT, ids=["%s-%d" % (c[0], i) for i, c in enumerate(CORRUPT)])
def test_corrupt_refuses(tmp_path, what, lines, n):
    path = ledger_path(tmp_path)
    write_file(path, *lines)
    before = open(path, "rb").read()
    assert _refusal(path).name == "ledger-corrupt:%s:%d" % (what, n)
    assert open(path, "rb").read() == before


def test_replay_keeps_the_payload_set_and_the_durable_acks(tmp_path):
    """Replay rebuilds the outbox: payload lines minus acked triples, in ref order whatever the file order; a repeated
    ack of an acked triple is not corrupt; the quarantine map survives."""
    path = ledger_path(tmp_path)
    p = {"lineage": {"n": 1}}
    write_file(path, header(), opened(1), reserved(2), reserved(3), reserved(4), refused_line(5),
               line("settled", 6, ref=4, settled=1, overrun=0, meta=p),
               line("uncertain", 7, ref=2, reason="upstream-reset", meta=p),
               line("released", 8, ref=3, meta=p),
               acked(9, tri(3), quarantined=[{"source": tri(3), "field": "tokens_in"}]),
               acked(10, tri(3)), opened(11, E2))
    led = L.Ledger.open(path)
    assert [r for r, _ in led.unacked()] == [(LID, E1, 2), (LID, E1, 4), (LID, E1, 5)]
    assert led.outbox_size == 3
    assert led.quarantined == {(LID, E1, 3): "tokens_in"}
    assert led.charged(SCOPE) == 1 + AMOUNT          # settled 1, released 0, uncertain at the full amount
    _close(led)


# ---- restart lines, released meta, refused lines -------------------------------------------------------------------

def test_restart_line_carries_a_payload_built_from_lineage_base(tmp_path):
    """Replay's `uncertain {restart}` for a ticketed hold carries `lineage_payload(base, uncertain, at)` and keeps the
    hold's OLD epoch in its triple (H6-12); an interactive hold (no base) gets `meta: {}` and no outbox entry."""
    path = ledger_path(tmp_path)
    write_file(path, header(), opened(1), reserved(2, meta={"lineage_base": BASE, "bound": "unproven"}), reserved(3))
    led = L.Ledger.open(path)
    rs = [o for o in file_lines(path) if o["kind"] == "uncertain"]
    assert [(o["ref"], o["reason"]) for o in rs] == [(2, "restart"), (3, "restart")]
    want = dict(BASE, settled="uncertain", ended_at=rs[0]["at"][:19] + "Z")    # H6-31: cut to its second
    assert rs[0]["meta"] == {"lineage": want} and rs[1]["meta"] == {}
    assert led.unacked() == [((LID, E1, 2), want)]
    assert led.epoch != E1
    _close(led)


def test_released_carries_meta(tmp_path):
    """H6-5: a released ticketed exchange has a payload (`settled: "no"`); an interactive one has `meta: {}`."""
    path = ledger_path(tmp_path)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        a = await Owner(led, meta={"lineage_base": BASE}, then_durable=True).start()
        b = await Owner(led, then_durable=True).start()
        await until(lambda: a.hold.state is L.RESERVED and b.hold.state is L.RESERVED)
        a.hold.release({"lineage": dict(BASE, settled="no")})   # H6-28: keys' own row on its own call
        b.hold.release()
        await until(lambda: a.hold.state is L.RESOLVED and b.hold.state is L.RESOLVED)
        out = led.unacked()
        await a.finish()
        await b.finish()
        await led.aclose(5)
        return a.hold.seq, out

    ref, out = asyncio.run(go())
    rel = [o for o in file_lines(path) if o["kind"] == "released"]
    assert [sorted(o) for o in rel] == [["at", "kind", "meta", "ref", "seq"]] * 2
    assert rel[0]["meta"]["lineage"] == dict(BASE, settled="no")
    assert rel[1]["meta"] == {}
    assert [r[2] for r, _ in out] == [ref]


def test_a_caller_payload_is_kept_and_an_owner_end_is_built(tmp_path):
    """keys' own `meta["lineage"]` on settle is written as given; a hold the owner abandons after SENT gets the
    ledger-built payload on its `uncertain {owner-ended}` line, so the exchange still has its row."""
    path = ledger_path(tmp_path)
    mine = {"settled": "yes", "tokens_in": 5}

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        a = await _sent_hold(led, meta={"lineage_base": BASE})
        b = await _sent_hold(led, meta={"lineage_base": BASE})
        a.hold.settle(40, {"usage": {"in": 5}, "lineage": mine})
        await b.finish()                                               # b's owner ends while SENT
        await until(lambda: a.hold.state is L.RESOLVED and b.hold.state is L.RESOLVED)
        out = led.unacked()
        await a.finish()
        await led.aclose(5)
        return out

    out = asyncio.run(go())
    ls = {o["kind"]: o for o in file_lines(path) if o["kind"] in ("settled", "uncertain")}
    assert ls["settled"]["meta"] == {"usage": {"in": 5}, "lineage": mine}
    assert ls["uncertain"]["reason"] == "owner-ended"
    assert ls["uncertain"]["meta"] == {"lineage": dict(BASE, settled="uncertain",
                                                          ended_at=ls["uncertain"]["at"][:19] + "Z")}
    assert [p for _, p in out] == [mine, ls["uncertain"]["meta"]["lineage"]]


def test_refused_line_carries_epoch_and_its_own_seq_as_ref(tmp_path):
    """H6-10: the `refused` line carries the current epoch; its triple is `(ledger_id, epoch, own seq)`; it enters
    the outbox only once its line is durable; the replay of the same file lists the same triple."""
    path = ledger_path(tmp_path)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        pay = {"refused": "budget-exhausted", "count": 1000}
        ref, fut = led.refused("s1", "r-a", "budget-exhausted", 1000, {"lineage": pay}, run_id="r-0123456789abcdef")
        assert led.unacked() == []                                     # queued, not durable
        await fut
        out = led.unacked()
        ids = (led.ledger_id, led.epoch)
        await led.aclose(5)
        return ref, out, ids, pay

    ref, out, (lid, epoch), pay = asyncio.run(go())
    (rl,) = [o for o in file_lines(path) if o["kind"] == "refused"]
    assert rl["epoch"] == epoch and rl["count"] == 1000 and rl["run_id"] == "r-0123456789abcdef"
    assert ref == (lid, epoch, rl["seq"])
    assert out == [(ref, pay)]
    led = L.Ledger.open(path)
    assert led.unacked() == [(ref, pay)]
    _close(led)


def test_refused_refuses_malformed_and_unhealthy(tmp_path):
    path = ledger_path(tmp_path)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop(), _writer=False)
        for args in [("", "r", "x", 1, {}), ("s", "r", "Bad", 1, {}), ("s", "r", "x", 0, {}),
                     ("s", "r", "x", True, {}), ("s", "r", "x", 1, {"lineage": 1}), ("s", "r", "x", 1, [])]:
            with pytest.raises(L.LedgerMisuse):
                led.refused(*args)
        led._healthy = False
        with pytest.raises(L.LedgerUnavailable):
            led.refused("s", "r", "x", 1, {})
        assert [o for o in _q(led)] == []

    asyncio.run(go())


def _q(led):
    out = []
    while True:
        try:
            out.append(led._q.get_nowait())
        except Exception:
            return out


# ---- the ack rule --------------------------------------------------------------------------------------------------

def test_an_ack_is_not_counted_before_its_flush(tmp_path, monkeypatch):
    """R4 §3 / r3's keys half: the ack's future resolves after the flush, and only then does the triple leave
    `unacked()`; while the flush is held the triple is still listed and still counts against the outbox."""
    path = ledger_path(tmp_path)
    ctl = FlushCtl()

    async def go():
        led = L.Ledger.open(path)
        monkeypatch.setattr(L, "_flush", ctl)
        led.attach(asyncio.get_running_loop())
        o = await _sent_hold(led, meta={"lineage_base": BASE})
        o.hold.settle(10, {"lineage": dict(BASE, settled="yes")})
        await until(lambda: o.hold.state is L.RESOLVED)
        (t, _), = led.unacked()
        ctl.gate.clear()
        fut = led.ack([t])
        await until(ctl.entered.is_set)
        for _ in range(20):
            await asyncio.sleep(0.005)
        assert not fut.done()
        assert [r for r, _ in led.unacked()] == [t] and led.outbox_size == 1
        assert "lineage-acked" in kinds(path)                         # written, not flushed
        assert led.ack([t]).done()                                     # a repeat while pending writes no second line
        ctl.gate.set()
        await fut
        assert led.unacked() == [] and led.outbox_size == 0
        assert led.ack([t]).done()                                     # already acked: no line
        await o.finish()
        await led.aclose(5)

    asyncio.run(go())
    assert kinds(path).count("lineage-acked") == 1


def test_an_ack_whose_flush_fails_is_not_counted(tmp_path, monkeypatch):
    path = ledger_path(tmp_path)
    ctl = FlushCtl()

    async def go():
        led = L.Ledger.open(path)
        monkeypatch.setattr(L, "_flush", ctl)
        led.attach(asyncio.get_running_loop())
        o = await _sent_hold(led, meta={"lineage_base": BASE})
        o.hold.settle(10, {"lineage": dict(BASE, settled="yes")})
        await until(lambda: o.hold.state is L.RESOLVED)
        (t, _), = led.unacked()
        ctl.errors.append(OSError(5, "EIO"))
        fut = led.ack([t])
        with pytest.raises(L.LedgerUnavailable):
            await fut
        assert [r for r, _ in led.unacked()] == [t]
        await o.finish()
        await led.aclose(1)

    asyncio.run(go())


def test_ack_refuses_an_unknown_triple_and_writes_nothing(tmp_path):
    """An ack the file could not replay (`ack-orphan`) is never written: keys would brick its own ledger."""
    path = ledger_path(tmp_path)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop(), _writer=False)
        o = await Owner(led, meta={"lineage_base": BASE}).start()
        assert [json.loads(x[1])["kind"] for x in _q(led)] == ["reserved"]
        led._durable([o.hold.seq])
        o.hold.send_gate()
        o.hold.mark_sent()
        o.hold.settle(5, {"lineage": dict(BASE, settled="yes")})
        (res,) = [json.loads(x[1]) for x in _q(led)]
        t = (led.ledger_id, led.epoch, o.hold.seq)
        with pytest.raises(L.LedgerMisuse):
            led.ack([t])                                              # its line is not durable yet
        led._durable([res["seq"]])
        for bad in [(led.ledger_id, led.epoch, 999), ("d" * 32, led.epoch, o.hold.seq), (led.ledger_id, E2, o.hold.seq),
                    (led.ledger_id, led.epoch, res["seq"]), ["x"], (led.ledger_id, led.epoch, True)]:
            with pytest.raises(L.LedgerMisuse):
                led.ack([t, bad])
        with pytest.raises(L.LedgerMisuse):
            led.ack([t], {(led.ledger_id, led.epoch, 999): "tokens_in"})   # a quarantine outside the refs
        with pytest.raises(L.LedgerMisuse):
            led.ack([t], {t: ""})
        assert _q(led) == []
        led.ack([t], {t: "tokens_in"})
        (a,) = [json.loads(x[1]) for x in _q(led)]
        assert a["refs"] == [{"ledger": t[0], "epoch": t[1], "ref": t[2]}]           # H6-17: objects
        assert a["quarantined"] == [{"source": a["refs"][0], "field": "tokens_in"}]
        led._durable([a["seq"]])
        await asyncio.sleep(0)
        assert led.quarantined == {t: "tokens_in"} and led.unacked() == []
        await o.finish()

    asyncio.run(go())


def test_unacked_is_in_ref_order_and_the_listener_sees_each_durable_line(tmp_path):
    path = ledger_path(tmp_path)
    seen: List[tuple] = []

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        led.on_outbox = lambda ref, pay: seen.append(ref)
        a = await _sent_hold(led, meta={"lineage_base": BASE})
        b = await _sent_hold(led, meta={"lineage_base": BASE})
        b.hold.settle(1, {"lineage": dict(BASE, settled="yes")})
        await until(lambda: b.hold.state is L.RESOLVED)
        a.hold.uncertain("upstream-reset", {"lineage": dict(BASE, settled="uncertain")})
        await until(lambda: a.hold.state is L.RESOLVED)
        out = [r[2] for r, _ in led.unacked()]
        await a.finish()
        await b.finish()
        await led.aclose(5)
        return a.hold.seq, b.hold.seq, out

    sa, sb, out = asyncio.run(go())
    assert out == [sa, sb] and [r[2] for r in seen] == [sb, sa]


# ---- kill -9 between the ack line's write and its flush -----------------------------------------------------------

def _kill(py: str, mode: str, d: str):
    path = os.path.join(d, "keys", "ledger.jsonl")
    os.makedirs(os.path.dirname(path), mode=0o700)
    events = os.path.join(d, "events.jsonl")
    p = subprocess.Popen([py, DRIVER, mode, path, events], env={**os.environ, "PYTHONPATH": str(ROOT)},
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        end = time.monotonic() + 30
        while True:
            evs = [json.loads(x) for x in open(events, "rb").read().splitlines()] if os.path.exists(events) else []
            if any(e.get("at") == "gate" for e in evs):
                break
            assert p.poll() is None, p.stderr.read().decode()
            assert time.monotonic() < end, evs
            time.sleep(0.02)
        p.send_signal(signal.SIGKILL)
        p.wait(10)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        p.stderr.close()
    return path, evs


@pytest.mark.parametrize("py", INTERPRETERS, ids=lambda p: "usr-bin-python3" if p == SYS_PY else "venv")
@pytest.mark.parametrize("mode", ["ack-w2", "ack-w1-torn"])
def test_an_ack_line_lost_before_its_flush_replays_unacked(py, mode):
    """The driver settles a ticketed hold (its line durable), submits `lineage-acked` and is SIGKILLed with the writer
    held between the line's write and its flush (`ack-w2`) or after half the line (`ack-w1-torn`). The driver never
    counted the ack (its future never resolved). A power cut loses an unflushed write; SIGKILL keeps the page cache, so
    `ack-w2` truncates the file to the size the driver recorded before the ack (the power-cut emulation) and also
    checks the kill alone: the complete line replays as acked, which is safe (CONTROL already holds the row)."""
    d = tempfile.mkdtemp(prefix="b6a", dir="/tmp")
    try:
        path, evs = _kill(py, mode, d)
        (o,) = [e for e in evs if e["at"] == "outbox"]
        t = tuple(o["triple"])
        assert not any(e["at"] == "ack-counted" for e in evs)
        if mode == "ack-w2":
            assert open(path, "rb").read().endswith(b"\n") and "lineage-acked" in kinds(path)
            as_killed = d + "/as-killed.jsonl"
            shutil.copy(path, as_killed)
            led = L.Ledger.open(as_killed)
            assert led.unacked() == []                                # the kill alone: the page cache kept it
            _close(led)
            with open(path, "r+b") as f:
                f.truncate(o["size"])
        led = L.Ledger.open(path)
        assert [r for r, _ in led.unacked()] == [t]
        assert "lineage-acked" not in kinds(path)
        _close(led)
    finally:
        shutil.rmtree(d, ignore_errors=True)
