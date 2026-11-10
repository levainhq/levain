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


def _enc_len(meta) -> int:
    return len(json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def test_a_base_must_leave_room_for_both_payloads_the_ledger_may_build():
    """r1 F5 (bound): the admit-time check of `lineage_base` covers BOTH payloads the ledger may build from it: the
    `uncertain` one (restart, a SENT owner-end) and the `released` one (an unsent owner-end, close), which sets
    `model_answered` / `provider_answered` null and `status` 0 and so is LONGER when the base lacks those keys. A base
    whose uncertain payload fits exactly but whose released one does not is refused."""
    def unc(b):
        return _enc_len({"lineage": L.lineage_payload(b, L.UNCERTAIN, L._AT_SAMPLE)})

    base = {"x": ""}
    base["x"] = "y" * (L.META_MAX - unc(base))
    assert unc(base) == L.META_MAX
    assert _enc_len({"lineage": L.lineage_payload(base, L.RELEASED, L._AT_SAMPLE)}) > L.META_MAX
    assert not L._lineage_ok({"lineage_base": base})
    small = dict(base, x="y" * 10)
    assert L._lineage_ok({"lineage_base": small})


def test_outbox_committed_counts_pending_lines_and_unresolved_ticketed_holds(tmp_path):
    """r1 F1: `outbox_committed` is the outbox the ledger is committed to: durable payload lines with no ack, payload
    lines queued but not durable, and every live ticketed hold not yet resolved (each resolves with exactly one
    payload). An interactive hold commits nothing. keys checks its bound against this, so the outbox never passes it."""
    path = ledger_path(tmp_path)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        seen = [led.outbox_committed]
        a = await Owner(led, meta={"lineage_base": BASE}).start()
        b = await Owner(led).start()
        seen.append(led.outbox_committed)                   # a's resolution is owed
        _ref, fut = led.refused("s1", "r-a", "budget-exhausted", 1, {"lineage": {"n": 1}})
        seen.append(led.outbox_committed)                   # + one queued line
        await fut
        seen.append(led.outbox_committed)                   # the line is durable: still one
        await a.hold.durable()
        a.hold.release({"lineage": dict(BASE, settled="no")})
        seen.append(led.outbox_committed)                   # resolved, its line queued: counted once
        await until(lambda: a.hold.state is L.RESOLVED)
        seen.append((led.outbox_committed, led.outbox_size))
        await a.finish()
        await b.finish()
        await led.aclose(5)
        return seen

    assert asyncio.run(go()) == [0, 1, 2, 2, 2, (2, 2)]


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


# ---- the run-final record (W3-3b): a run's count, written only when every row of it is acked -------------------------

RID = BASE["run_id"]
RID2 = "r-fedcba9876543210"


def _run_hold(led, rid=RID, amount=AMOUNT):
    return Owner(led, amount=amount, cap=10 * AMOUNT, scope=("run", rid), meta={"lineage_base": dict(BASE, run_id=rid)})


async def _resolved_row(led, rid=RID, quarantined=False):
    """One sent, settled, durable ticketed hold of the run; returns its triple."""
    o = await _run_hold(led, rid).start()
    await o.hold.durable()
    o.hold.send_gate()
    o.hold.mark_sent()
    o.hold.settle(1, {"lineage": dict(BASE, run_id=rid, settled="yes")})
    await until(lambda: o.hold.state is L.RESOLVED)
    await o.finish()
    return o.hold.ref


def _finals(path):
    return [o for o in file_lines(path) if o["kind"] == "run-final"]


def test_a_final_is_written_only_after_every_triple_is_durably_acked(tmp_path):
    path = ledger_path(tmp_path)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        a = await _resolved_row(led)
        b = await _resolved_row(led)
        other = await _resolved_row(led, RID2)                      # another run's row never counts here
        assert not led.run_ready(RID)                               # both rows are durable and unacked
        with pytest.raises(L.LedgerMisuse):
            led.run_final(RID)
        assert _finals(path) == []
        await led.ack([a])
        assert not led.run_ready(RID)                               # one still unacked
        await led.ack([b], {b: "tokens_in"})
        assert led.run_ready(RID)
        await led.run_final(RID)
        (fin,) = _finals(path)
        assert fin["run_id"] == RID and fin["epoch"] == led.epoch
        pay = fin["meta"]["lineage"]
        assert pay == {"format": L.RUN_FINAL_FORMAT, "run_id": RID, "rows": 2, "quarantined": 1,
                       "triples_digest": L.triples_digest([a, b]), "at": fin["at"][:19] + "Z"}
        assert L.triples_digest([a, b]) != L.triples_digest([a, other])
        # the final rides the outbox as its own triple; it is no row of its run
        assert [(r, p.get("format")) for r, p in led.unacked() if p.get("format")] == [
            ((led.ledger_id, led.epoch, fin["seq"]), L.RUN_FINAL_FORMAT)]
        await led.aclose(5)

    asyncio.run(go())
    led = L.Ledger.open(path)                                       # replay finds the same count and the same fence
    assert led.run_finalized(RID) and not led.run_finalized(RID2)
    assert [p.get("format") for _r, p in led.unacked()].count(L.RUN_FINAL_FORMAT) == 1      # the final is an unacked payload
    _close(led)


def test_a_final_waits_for_an_open_hold_and_counts_a_refused_row(tmp_path):
    path = ledger_path(tmp_path)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        done = await _resolved_row(led)
        o = await _run_hold(led).start()
        await o.hold.durable()
        o.hold.send_gate()
        o.hold.mark_sent()                                          # SENT: outstanding, no row yet
        await led.ack([done])
        assert not led.run_ready(RID)
        with pytest.raises(L.LedgerMisuse):
            led.run_final(RID)
        o.hold.settle(1, {"lineage": dict(BASE, settled="yes")})
        await until(lambda: o.hold.state is L.RESOLVED)
        await o.finish()
        assert not led.run_ready(RID)                               # its row is durable, not acked
        t, fut = led.refused("s1", "r-a", "budget-exhausted", 2, {"lineage": dict(BASE, refused="budget-exhausted")},
                             run_id=RID)
        assert not led.run_ready(RID)                               # queued, not durable
        await fut
        await led.ack([o.hold.ref, t])
        assert led.run_ready(RID)
        await led.run_final(RID)
        await led.aclose(5)

    asyncio.run(go())
    assert _finals(path)[0]["meta"]["lineage"]["rows"] == 3


def test_the_fence_refuses_any_row_of_a_finalized_run(tmp_path):
    path = ledger_path(tmp_path)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        a = await _resolved_row(led)
        await led.ack([a])
        fut = led.run_final(RID)                                    # queued, not yet durable: the fence is already up
        o = await _run_hold(led).start()
        assert isinstance(o.error, L.RunFinalized) and isinstance(o.error, L.LedgerUnavailable)
        assert o.error.detail == "run-finalized" and o.hold is None
        with pytest.raises(L.RunFinalized):
            led.refused("s1", "r-a", "budget-exhausted", 1, {"lineage": dict(BASE, refused="x")}, run_id=RID)
        with pytest.raises(L.RunFinalized):                         # the payload names the run though the argument does not
            led.refused("s1", "r-a", "budget-exhausted", 1, {"lineage": dict(BASE, refused="x")})
        await fut
        assert [o["kind"] for o in file_lines(path)].count("refused") == 0
        # another run, and a refusal naming no run, are untouched
        o2 = await _run_hold(led, RID2).start()
        assert o2.error is None
        await o2.finish()
        t, f2 = led.refused("s1", "r-a", "budget-exhausted", 1, {}, run_id=None)
        await f2
        await led.aclose(5)

    asyncio.run(go())


def test_a_reused_run_id_refuses_live_and_after_a_restart(tmp_path):
    path = ledger_path(tmp_path)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        a = await _resolved_row(led)
        await led.ack([a])
        await led.run_final(RID)
        with pytest.raises(L.LedgerMisuse):
            led.run_final(RID)
        await led.aclose(5)

    asyncio.run(go())
    assert len(_finals(path)) == 1

    async def again():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        assert led.run_finalized(RID)
        with pytest.raises(L.LedgerMisuse):
            led.run_final(RID)
        o = await _run_hold(led).start()
        assert isinstance(o.error, L.RunFinalized)
        await led.aclose(5)

    asyncio.run(again())
    assert len(_finals(path)) == 1


def test_a_run_replayed_after_a_restart_finalizes_with_the_same_count(tmp_path):
    path = ledger_path(tmp_path)

    async def first():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        a = await _resolved_row(led)
        b = await _resolved_row(led)
        await led.ack([a, b])
        await led.aclose(5)
        return a, b

    a, b = asyncio.run(first())

    async def second():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        assert not led.run_finalized(RID) and led.run_ready(RID)
        await led.run_final(RID)
        await led.aclose(5)

    asyncio.run(second())
    pay = _finals(path)[0]["meta"]["lineage"]
    assert (pay["rows"], pay["quarantined"], pay["triples_digest"]) == (2, 0, L.triples_digest([a, b]))


def test_a_restart_booked_row_is_a_row_of_its_run(tmp_path):
    """A hold open at a crash is booked `uncertain {restart}` with its payload at the next open: that row is the run's
    too, and the run is ready only after its ack."""
    path = ledger_path(tmp_path)

    async def first():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        o = await _run_hold(led).start()
        await o.hold.durable()
        o.hold.send_gate()
        o.hold.mark_sent()
        return led, o

    loop = asyncio.new_event_loop()
    led, o = loop.run_until_complete(first())
    held = o.hold.ref
    os.close(led._fd)                                               # a kill: nothing more is written, the lock goes
    led._fd = -1
    loop.close()

    async def second():
        led2 = L.Ledger.open(path)
        led2.attach(asyncio.get_running_loop())
        assert led2.run_ready(RID) is False                         # its restart row is unacked
        await led2.ack([held])
        assert led2.run_ready(RID)
        await led2.run_final(RID)
        await led2.aclose(5)

    asyncio.run(second())
    pay = _finals(path)[0]["meta"]["lineage"]
    assert (pay["rows"], pay["triples_digest"]) == (1, L.triples_digest([held]))


def test_a_second_final_line_for_one_run_is_corrupt(tmp_path):
    path = ledger_path(tmp_path)
    p1 = {"format": L.RUN_FINAL_FORMAT, "run_id": RID, "rows": 0, "quarantined": 0,
          "triples_digest": L.triples_digest([]), "at": "2026-11-09T01:02:03Z"}

    def fin(seq):
        return line("run-final", seq, epoch=E1, run_id=RID, meta={"lineage": p1})

    write_file(path, header(), opened(1), fin(2))
    led = L.Ledger.open(path)
    assert led.run_finalized(RID)
    _close(led)
    write_file(path, header(), opened(1), fin(2), fin(3))
    assert _refusal(path).name == "ledger-corrupt:final-dup:4"


@pytest.mark.parametrize("bad", [
    {"run_id": "r-1"},                                              # not the run id grammar
    {"meta": {"lineage": {"format": "levain-dispatch-run-final/2"}}},
    {"meta": {}},
    {"meta": {"lineage": {"extra": 1}}},
])
def test_a_malformed_final_line_is_corrupt(tmp_path, bad):
    path = ledger_path(tmp_path)
    p1 = {"format": L.RUN_FINAL_FORMAT, "run_id": RID, "rows": 0, "quarantined": 0,
          "triples_digest": L.triples_digest([]), "at": "2026-11-09T01:02:03Z"}
    fields = {"epoch": E1, "run_id": RID, "meta": {"lineage": p1}}
    if "meta" in bad and "lineage" in bad["meta"]:
        fields["meta"] = {"lineage": dict(p1, **bad["meta"]["lineage"])}
    else:
        fields.update(bad)
    write_file(path, header(), opened(1), line("run-final", 2, **fields))
    assert _refusal(path).name == "ledger-corrupt:type:3"


def test_a_final_for_a_run_the_ledger_never_saw_refuses(tmp_path):
    path = ledger_path(tmp_path)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        assert not led.run_known(RID2) and led.open_runs() == []
        with pytest.raises(L.LedgerMisuse):
            led.run_final(RID2)                                     # no note, no hold, no row: nothing settles it at zero
        assert not led.run_finalized(RID2)                          # the refusal left no fence
        await led.aclose(5)

    asyncio.run(go())
    assert _finals(path) == []


def test_a_noted_run_with_no_hold_finalizes_at_zero(tmp_path):
    path = ledger_path(tmp_path)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        led.note_run(RID)
        assert led.run_known(RID) and led.run_ready(RID)
        await led.run_final(RID)
        await led.aclose(5)

    asyncio.run(go())
    pay = _finals(path)[0]["meta"]["lineage"]
    assert (pay["rows"], pay["quarantined"], pay["triples_digest"]) == (0, 0, L.triples_digest([]))


def test_the_digest_is_the_compact_ascii_json_of_the_sorted_triples():
    import hashlib
    a, b = ("b" * 32, "c" * 32, 2), ("a" * 32, "c" * 32, 9)
    raw = '[["%s","%s",9],["%s","%s",2]]' % ("a" * 32, "c" * 32, "b" * 32, "c" * 32)
    want = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert L.triples_digest([a, b]) == L.triples_digest([b, a]) == want
    assert L.triples_digest([{"ledger": a[0], "epoch": a[1], "ref": a[2]}, list(b)]) == want


def test_a_run_final_whose_line_fails_takes_its_fence_down(tmp_path, monkeypatch):
    # runfinal r1 (complement LOW 3, deepseek MED, gemini HIGH): the fence stands for a count that is queued or
    # written, never for one whose line failed; the ledger is unhealthy then, so no line of the run can follow it
    import errno
    path = ledger_path(tmp_path)
    real = L._write_all

    def full_for_finals(fd, data):
        if b'"kind":"run-final"' in data:
            raise OSError(errno.ENOSPC, "no space")
        return real(fd, data)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        led.note_run(RID)
        monkeypatch.setattr(L, "_write_all", full_for_finals)
        fut = led.run_final(RID)
        queued = led.run_finalized(RID)
        with pytest.raises(L.LedgerUnavailable):
            await fut
        await asyncio.sleep(0)
        out = (queued, led.run_finalized(RID), led.healthy)
        await led.aclose(5)
        return out

    assert asyncio.run(go()) == (True, False, False)
    assert _finals(path) == []


class _QueueThatFails:
    """The writer's queue with `put` failing while `fail` is set; `get` and `get_nowait` reach the real queue (the
    writer thread is already blocked in the real one)."""

    def __init__(self, real) -> None:
        self.real, self.fail = real, True

    def put(self, item) -> None:
        if self.fail:
            raise RuntimeError("the queue refused the line")
        self.real.put(item)

    def get(self, *a, **k):
        return self.real.get(*a, **k)

    def get_nowait(self):
        return self.real.get_nowait()


def test_a_line_that_cannot_be_queued_leaves_no_bookkeeping_behind(tmp_path, monkeypatch):
    # codex K3x: a submit that raises after touching the in-flight maps rolls every entry back, so nothing eats the
    # outbox bound, no fence goes up, and the same count queues cleanly on the next call
    path = ledger_path(tmp_path)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        led.note_run(RID)
        led.note_run(RID2)
        q = _QueueThatFails(led._q)
        monkeypatch.setattr(led, "_q", q)
        with pytest.raises(RuntimeError):
            led.run_final(RID)
        failed = (dict(led._lines), dict(led._line_futs), dict(led._pending_outbox), led.outbox_committed,
                  led.run_finalized(RID))
        # an ack whose line cannot be queued leaves no triple pending, so a later ack of it is not skipped
        q.fail = False
        await led.run_final(RID2)
        triple = led.unacked()[0][0]
        q.fail = True
        with pytest.raises(RuntimeError):
            led.ack([triple])
        await asyncio.sleep(0)
        ack_pending = set(led._ack_pending)
        q.fail = False
        await led.ack([triple])
        await led.run_final(RID)
        out = (failed, ack_pending, led.run_finalized(RID), led.outbox_size, led.healthy)
        await led.aclose(5)
        return out

    failed, ack_pending, done, size, healthy = asyncio.run(go())
    assert failed == ({}, {}, {}, 0, False)
    assert ack_pending == set()
    assert (done, size, healthy) == (True, 1, True)
    assert sorted(f["run_id"] for f in _finals(path)) == sorted([RID, RID2])
