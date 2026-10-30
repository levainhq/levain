"""S1 3c-6c: keys' lineage records and outbox (SCHEMAS §2.1 `levain-dispatch-lineage/3`; R4 §3 "Delivery"; head rulings
H6-3, H6-6, H6-7, H6-8, H6-10, H6-11, H6-15 .. H6-17, H6-19, H6-21, H6-22, H6-28 in `1024-13/3c6/HEAD_RULINGS_3C6.md`).

The row cases drive the real `Keys._exchange` in-process against the scripted upstream of `test_broker_keys_upstream`
on a dispatch-shaped route (`test_tickets_keys.dispatch_pol`), with a ticketed session's claims, and read the ledger
lines. The delivery cases run a REAL CONTROL (`control.serve` on a thread, its lineage sink under a temp dir) and keys
in-process on its attachment, and read CONTROL's sink."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from levain.broker import codec, gwwire
from levain.broker import keys as K
from levain.broker import ledger as L
from levain.broker import upstream as U
from levain.broker.creds import CredStore
from levain.broker.norm import digest_of_token
from levain.contracts import canon
from levain.slot import control as C
from levain.slot import lineage as LN
from tests.ledger_support import file_lines, keys_ledger
from tests.test_broker_keys_upstream import BODY as IBODY
from tests.test_broker_keys_upstream import CLAIMS, TOKEN, _AdminW, _lib, _one_address, _Plain, _q, _Up
from tests.test_slot_s2a_control import OP_UID
from tests.test_tickets_keys import BODY, FRONT_CLAIMS, MODEL, RUN, dispatch_pol

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets")

KEYS_UID = OP_UID + 3000
TICKETED = {"routes": ["glm"], **FRONT_CLAIMS, "run": dict(RUN)}
INTERACTIVE = dict(CLAIMS)


def _h621_landed() -> bool:
    """H6-21: True when `RECORD_CHECKS["model_answered"]` accepts a null on a refused / released row."""
    return LN.RECORD_CHECKS["model_answered"](None)


def http_json(obj: dict, status: int = 200) -> bytes:
    body = json.dumps(obj).encode()
    return (b"HTTP/1.1 %d OK\r\ncontent-type: application/json\r\ncontent-length: %d\r\n\r\n" % (status, len(body))
            + body)


def run_go(fn):
    d = tempfile.mkdtemp(prefix="kl", dir="/tmp")
    _lib(d)
    try:
        return asyncio.run(asyncio.wait_for(fn(d), 120))
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def until(cond, timeout: float = 30.0, what: str = "") -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "not met within %s s: %s" % (timeout, what)
        await asyncio.sleep(0.01)


class KeysWorld:
    """keys in-process with one claimed registration (session s1); `q()` is one whole exchange on a fresh connection
    against a fresh scripted upstream. Not attached (no CONTROL): the ledger is the outbox, and nothing is sent."""

    def __init__(self, d: str, monkeypatch, pol=None, **attrs) -> None:
        self.d, self.mp, self.pol, self.attrs = d, monkeypatch, pol if pol is not None else dispatch_pol(), attrs

    async def __aenter__(self) -> "KeysWorld":
        self.cred_fd = os.open(os.path.join(self.d, "keys", "cred"), os.O_RDONLY | os.O_DIRECTORY)
        self.ledger = keys_ledger(os.path.join(self.d, "keys", "ledger.jsonl"))
        pol = self.pol
        k = self.k = K.Keys(ledger=self.ledger, lib_root=self.d, creds=CredStore(self.cred_fd),
                            dispatch_policy=(lambda r, pin: pol))
        for n, v in self.attrs.items():
            setattr(k, n, v)
        k._in_loop()
        k.admin = K.AdminConn(None, _AdminW(), k.admin_epoch)
        await k.front_register(k.admin_epoch, "s1", 70000, 1, 1, 1, 1)
        await k.front_arm(k.admin_epoch, "s1", 1)
        self.reg = k.regs[70000]
        self.reg.state = "claimed"
        return self

    async def q(self, claims: dict, *, route: str = "glm", body: bytes = BODY, response=None,
                connect_fails: bool = False) -> list:
        self.k.cache[digest_of_token(TOKEN)] = {"session": "s1", "claims": claims}
        up = _Up()
        for part in (response if response is not None else (http_json({"usage": {"input_tokens": 1,
                                                                                 "output_tokens": 1}}), b"")):
            up.feed(part)

        async def handshake(addr, port, host):
            if connect_fails:
                raise ConnectionRefusedError(61, "refused")
            return up, up, _Plain()

        self.mp.setattr(U, "_handshake", handshake)
        self.mp.setattr(U, "_resolve", _one_address)
        a, b = socket.socketpair()
        try:
            _r, w = await asyncio.open_unix_connection(sock=a)
            front = asyncio.StreamReader()
            front.feed_data(gwwire.encode(b"B", body) + gwwire.encode(b"E"))
            conn = K.Conn(w, self.reg, "pending")
            self.reg.pending.add(conn)
            self.k.pool.add(conn)
            side = gwwire.KeysSide()
            side.got(b"Q")
            q = dict(_q("length:%d" % len(body)), route=route)
            await asyncio.wait_for(asyncio.ensure_future(
                self.k._exchange(conn, gwwire.FrameReader(front), side, q)), 10)
            w.close()
            await asyncio.wait_for(w.wait_closed(), 2)
            b.settimeout(2)
            raw = b""
            while True:
                x = b.recv(65536)
                if not x:
                    break
                raw += x
        finally:
            a.close()
            b.close()
        # the exchange's resolution line is queued when it returns: wait for the writer to make it durable
        await until(lambda: not self.ledger._lines and not self.ledger._live, 10, "the ledger drained")
        out = []
        while raw:
            n = int.from_bytes(raw[1:5], "big")
            out.append((raw[:1], raw[5:5 + n]))
            raw = raw[5 + n:]
        return out

    def lines(self) -> List[dict]:
        return file_lines(self.ledger.path)

    async def __aexit__(self, *exc) -> None:
        await self.ledger.aclose(2.0)
        os.close(self.cred_fd)


def _types(frames: list) -> bytes:
    return b"".join(t for t, _ in frames)


def _refused_name(frames: list) -> Optional[str]:
    for t, b in frames:
        if t == b"F":
            return json.loads(b)["refused"]
    return None


def _one(lines: List[dict], kind: str) -> dict:
    got = [o for o in lines if o["kind"] == kind]
    assert len(got) == 1, (kind, [o["kind"] for o in lines])
    return got[0]


def _wire_ok(row: dict, ref: int = 1) -> Optional[str]:
    """CONTROL's own record check (`levain.slot.lineage.check_record`) of the row as keys sends it."""
    return LN.check_record(K.wire_record(("a" * 32, "b" * 32, ref), row))


# ---- the rows ------------------------------------------------------------------------------------------------------

def test_every_row_shape_carries_every_field_and_the_bounds_are_the_sinks():
    # H6-15: every `/3` field always present, one type per field; keys' envelope bounds are CONTROL's (H6-11, H6-19)
    base = K.lineage_base(RUN, "2026-10-24T12:00:00Z", 100, False)
    rows = [base, K.refused_row(RUN, reason="budget-exhausted", count=3, at="2026-10-24T12:00:00Z",
                                ended_at="2026-10-24T12:01:00Z"),
            K.lineage_row(base, settled="no", ended_at="2026-10-24T12:00:01Z", md=None),
            K.lineage_row(base, settled="yes", ended_at="2026-10-24T12:00:01Z", md=codec.Metadata(),
                          usage=codec.Usage(1, 2, 3, 4), cost=7, overrun=0, status=200)]
    for row in rows:
        assert set(row) == set(LN.RECORD_CHECKS), sorted(set(row) ^ set(LN.RECORD_CHECKS))
    assert K.LINEAGE_FORMAT == LN.FORMAT
    assert K.LINEAGE_ENVELOPE_RECORDS == LN.ENVELOPE_MAX == 64
    assert K.LINEAGE_ENVELOPE_BYTES == getattr(LN, "ENVELOPE_BYTES_MAX", 48 * 1024) == 48 * 1024
    assert K.LINEAGE_OUTBOX_MAX == 4096 and K.REFUSED_ROW_S == 60.0
    # H6-31: the record goes as the outbox holds it plus its `source` (no stamp is re-cut on the wire: the ledger
    # writes its own rows' stamps in CONTROL's form, `test_a_ledger_built_row_is_acked_not_quarantined`)
    rec = K.wire_record(("a" * 32, "b" * 32, 9), base)
    assert rec == dict(base, source={"ledger": "a" * 32, "epoch": "b" * 32, "ref": 9})
    assert LN.check_record(rec) is None


def test_a_settle_with_a_reported_cost_and_no_counts_carries_the_cost():
    # verify r2 R2: another model's reported cost past the hold is settled with no usage; its row says the cost and the
    # overrun, the token counts null, and CONTROL's record check takes it
    base = K.lineage_base(RUN, "2026-10-24T12:00:00Z", 100, False)
    row = K.lineage_row(base, settled="yes", ended_at="2026-10-24T12:00:01Z",
                        md=codec.Metadata("m2", None, 10_000, models=("m2",)), usage=None, cost=10_000, overrun=9_900,
                        status=200)
    assert (row["cost_microdollars"], row["overrun"], row["tokens_in"], row["tokens_out"]) == (10_000, 9_900, None, None)
    assert _wire_ok(row) is None


def test_settled_row_is_whole_from_lineage_base_and_the_response(monkeypatch):
    # R4 §3 "the record is on disk before it is sent": the `reserved` line carries `lineage_base`, and the settle
    # carries keys' whole row (H6-28): the run's facts renamed per H6-3, the response's own metadata (H6-6), the
    # counts and the priced cost; CONTROL's check passes it
    usage = {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 2}

    async def go(d):
        async with KeysWorld(d, monkeypatch) as w:
            # a dated variant of the checked model (3c-9: any other model would be charged its hold, not priced)
            frames = await w.q(TICKETED, response=(http_json({"type": "message", "model": MODEL + "-20260901",
                                                              "usage": usage}), b""))
            return frames, w.lines()

    frames, lines = run_go(go)
    assert _types(frames) == b"ARbe", frames
    res, st = _one(lines, "reserved"), _one(lines, "settled")
    base = res["meta"]["lineage_base"]
    assert set(base) == set(LN.RECORD_CHECKS)
    row = st["meta"]["lineage"]
    cost = codec.price_usage(codec.Usage(3, 4, 0, 2), codec_price())
    want = dict(base, settled="yes", ended_at=row["ended_at"], model_answered=MODEL + "-20260901",
                provider_answered="unknown", cost_reported=None, tokens_in=5, tokens_out=4, cost_microdollars=cost,
                overrun=None, status=200)
    assert row == want
    assert st["meta"]["usage"] == {"in": 3, "out": 4, "cache_write": 0, "cache_read": 2}
    assert base["run_id"] == RUN["run_id"] and base["route_policy_digest"] == RUN["route_policy_digest"]
    assert base["hold_microdollars"] == res["amount"] and base["run_cap_microdollars"] == RUN["reserved_microdollars"]
    assert base["model_requested"] == MODEL and base["unproven"] is False and base["count"] is None
    assert _wire_ok(row) is None


def codec_price():
    return dispatch_pol().prices[MODEL]


def test_overrun_row_is_settled_yes_with_its_overrun(monkeypatch):
    # H6-7: an overrun row is `settled: "yes"` plus `overrun` (the settled amount is known; the overrun its own fact)
    async def go(d):
        async with KeysWorld(d, monkeypatch) as w:
            frames = await w.q(TICKETED, response=(http_json({"model": MODEL, "usage": {
                "input_tokens": 0, "output_tokens": 100000}}), b""))
            return frames, w.lines()

    frames, lines = run_go(go)
    assert _types(frames) == b"ARbe", frames
    res, st = _one(lines, "reserved"), _one(lines, "settled")
    row = st["meta"]["lineage"]
    cost = codec.price_usage(codec.Usage(0, 100000, 0, 0), codec_price())
    assert cost > res["amount"] and st["overrun"] == cost - res["amount"]
    assert row["settled"] == "yes" and row["overrun"] == cost - res["amount"] and row["cost_microdollars"] == cost
    assert row["hold_microdollars"] == res["amount"]
    assert _wire_ok(row) is None


def test_released_row_is_settled_no_with_the_calls_facts_null(monkeypatch):
    # H6-5 / H6-16 / H6-21: a released ticketed exchange (no upstream contact, no money) is a row with `settled: "no"`,
    # and the facts of a call that never happened are null, never "unknown"
    async def go(d):
        async with KeysWorld(d, monkeypatch) as w:
            frames = await w.q(TICKETED, connect_fails=True)
            return frames, w.lines()

    frames, lines = run_go(go)
    assert _refused_name(frames) == "upstream-error", frames
    rel = _one(lines, "released")
    row = rel["meta"]["lineage"]
    assert row["settled"] == "no" and row["refused"] == "upstream-error" and row["status"] == 0
    for k in ("model_answered", "provider_answered", "cost_reported", "tokens_in", "tokens_out", "cost_microdollars",
              "overrun", "count"):
        assert row[k] is None, k
    assert set(row) == set(LN.RECORD_CHECKS)
    assert _wire_ok(row) in ((None,) if _h621_landed() else ("model_answered",))


def test_lineage_for_aborted_stream(monkeypatch):
    # r3 §2.4 DONE: a stream cut after its head is ONE row, `uncertain` (the call happened, its cost unknown): the
    # model the stream named in its first event, the relayed status, the refusal that cut it; no counts
    start = {"type": "message_start", "message": {"model": "glm-5.2-0901", "usage": {"input_tokens": 3}}}
    head = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: 100000\r\n\r\n")
    event = b"event: message_start\ndata: " + json.dumps(start).encode() + b"\n\n"

    async def go(d):
        async with KeysWorld(d, monkeypatch) as w:
            frames = await w.q(TICKETED, response=(head + event, b""))
            return frames, w.lines()

    frames, lines = run_go(go)
    assert frames[:2][0][0] == b"A" and frames[1][0] == b"R" and b"e" not in _types(frames), frames
    unc = _one(lines, "uncertain")
    assert unc["reason"] == "ended"
    assert not [o for o in lines if o["kind"] in ("settled", "released")]
    row = unc["meta"]["lineage"]
    assert row["settled"] == "uncertain" and row["model_answered"] == "glm-5.2-0901"
    assert row["provider_answered"] == "unknown" and row["status"] == 200 and row["refused"] == "upstream-error"
    assert row["tokens_in"] is None and row["cost_microdollars"] is None and row["cost_reported"] is None
    assert _wire_ok(row) is None


def test_an_interactive_exchange_writes_no_lineage(monkeypatch):
    # Q9: an interactive session writes no lineage: no base, no payload, nothing in the outbox
    async def go(d):
        async with KeysWorld(d, monkeypatch) as w:
            frames = await w.q(INTERACTIVE, route="r", body=IBODY)
            return frames, w.lines(), w.ledger.outbox_size

    frames, lines, size = run_go(go)
    assert _types(frames) == b"ARbe", frames
    assert size == 0 and not [o for o in lines if "lineage" in o.get("meta", {}) or "lineage_base" in o.get("meta", {})]


# ---- H6-28: one place computes a row --------------------------------------------------------------------------------

ROW = {"settled": "yes", "tokens_in": 5}


def test_ledger_refuses_a_keys_resolution_of_a_ticketed_hold_without_its_row(tmp_path):
    path = str(tmp_path / "ledger.jsonl")

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())

        async def hold(meta, sent):
            h = led.admit(("run", "r-0123456789abcdef"), 10 ** 6, lambda room: 10, "m", meta)
            await h.durable()
            if sent:
                h.send_gate()
                h.mark_sent()
            return h

        base = {"lineage_base": {"run_id": "r-0123456789abcdef"}}
        s = await hold(base, True)
        for call in (lambda: s.settle(5), lambda: s.settle(5, {"usage": {"in": 1}}), lambda: s.uncertain("no-usage"),
                     lambda: s.end()):
            with pytest.raises(L.LedgerMisuse):
                call()
            assert s.state is L.SENT
        assert s.settle(5, {"usage": {"in": 1}, "lineage": ROW}) is True
        u = await hold(base, False)
        for call in (lambda: u.release(), lambda: u.end(), lambda: u.end({})):
            with pytest.raises(L.LedgerMisuse):
                call()
            assert u.state is L.RESERVED
        assert u.end({"lineage": dict(ROW, settled="no")}) is True
        e = await hold(base, True)
        assert e.end({"lineage": dict(ROW, settled="uncertain")}) is True
        # an interactive hold carries no row, and a resolved hold's later end is a no-op, never a misuse
        i = await hold({}, True)
        assert i.settle(5) is True and s.end() is False and s.end({}) is False
        j = await hold({}, False)
        assert j.release() is True
        await until(lambda: all(h.state is L.RESOLVED for h in (s, u, e, i, j)))
        await led.aclose(5)

    asyncio.run(go())
    by = {o["ref"]: o for o in file_lines(path) if o["kind"] in ("settled", "released", "uncertain")}
    assert len(by) == 5
    rows = [o["meta"].get("lineage") for o in by.values()]
    assert rows.count(ROW) == 1 and dict(ROW, settled="no") in rows and dict(ROW, settled="uncertain") in rows
    assert rows.count(None) == 2   # the interactive holds


# ---- the response-metadata reader (H6-6) ------------------------------------------------------------------------------

def test_the_one_metadata_reader():
    def body(c, obj):
        r = codec.CODECS[c].usage(b"application/json")
        r.feed(json.dumps(obj).encode())
        return r

    def sse(c, *events):
        r = codec.CODECS[c].usage(b"text/event-stream")
        for e in events:
            r.feed(b"data: " + json.dumps(e).encode() + b"\n\n")
        return r

    # OpenRouter's chat completion: its model, the provider that served it, and its reported cost (credits, USD)
    r = body("openai-chat", {"model": "z-ai/glm-5.2", "provider": "Z.AI",
                             "usage": {"prompt_tokens": 3, "completion_tokens": 4, "cost": 0.000123}})
    glm = codec.Metadata("z-ai/glm-5.2", "Z.AI", 123, models=("z-ai/glm-5.2",))
    assert r.metadata() == glm
    assert r.result() is not None and r.metadata() == glm
    # a stream: every chunk names the model, the final one carries the cost (rounded up to a microdollar)
    r = sse("openai-chat", {"model": "m-1", "provider": "P"}, {"model": "m-1", "usage": {
        "prompt_tokens": 1, "completion_tokens": 1, "cost": 1.0000001}})
    assert r.metadata() == codec.Metadata("m-1", "P", 1000001, models=("m-1",))
    # Anthropic names its model in `message_start.message` (a body at its top level), no provider, no cost
    r = sse("anthropic-messages", {"type": "message_start", "message": {"model": "claude-x", "usage": {}}})
    assert r.metadata() == codec.Metadata("claude-x", None, None, models=("claude-x",))
    assert body("anthropic-messages", {"model": "claude-y", "usage": {}}).metadata().model == "claude-y"
    # Responses: the Response object, in the events as `response`
    r = sse("openai-responses", {"type": "response.created", "response": {"model": "gpt-x"}})
    assert r.metadata().model == "gpt-x"
    # what is not printable ASCII 1..128, or not a finite non-negative number, was not said
    for bad in ({"model": "é"}, {"model": "x" * 129}, {"model": 5}, {"model": ""},
                {"usage": {"cost": True}}, {"usage": {"cost": -1}}, {"usage": {"cost": "0.1"}}):
        # 3c-9: a model stated outside the grammar is recorded as not said, and marked, so the settle fails closed
        want = codec.Metadata(model_unreadable="model" in bad)
        assert body("openai-chat", bad).metadata() == want, bad
    # a body past the copy cap, or cut short, said nothing readable; a stopped stream keeps what it read before
    cut = codec.CODECS["openai-chat"].usage(b"application/json")
    cut.feed(b'{"model": "m-1", "usag')
    assert cut.metadata() == codec.Metadata()
    r = sse("anthropic-messages", {"type": "message_start", "message": {"model": "claude-x"}})
    r.feed(b"data: " + b"x" * (codec.USAGE_EVENT_MAX + 1))
    assert r.stopped == "event-over-cap" and r.metadata().model == "claude-x"


# ---- refusals (H6-10, H6-16) ------------------------------------------------------------------------------------------

def test_refused_rows_aggregated(monkeypatch):
    # r3 §2.4 DONE as H6-10 reads it: 1,000 refused ticketed `Q`s in one window are ONE `refused` line, `count`
    # 1,000, written at the window's close (counts in memory until then), carrying its row (H6-16)
    async def go(d):
        async with KeysWorld(d, monkeypatch, refused_row_s=3600.0) as w:
            names = set()
            for _ in range(1000):
                names.add(_refused_name(await w.q(dict(TICKETED, routes=["glm", "r"]), route="r")))
            before = [o for o in w.lines() if o["kind"] == "refused"]
            (key,) = list(w.k._refusals)
            count = w.k._refusals[key][1]
            w.k._refused_close(key)        # the window's own timer callback (REFUSED_ROW_S after its first refusal)
            await until(lambda: w.ledger.outbox_size == 1)
            return names, before, key, count, w.lines(), w.ledger.unacked(), w.ledger.epoch, w.ledger.ledger_id

    names, before, key, count, lines, outbox, epoch, lid = run_go(go)
    assert names == {"route-not-allowed"} and before == [] and key == ("s1", "route-not-allowed") and count == 1000
    line = _one(lines, "refused")
    assert (line["count"], line["reason"], line["session"], line["run_id"], line["route"], line["epoch"]) == \
        (1000, "route-not-allowed", "s1", RUN["run_id"], RUN["route"], epoch)
    assert not [o for o in lines if o["kind"] == "reserved"]
    ((triple, row),) = outbox
    assert triple == (lid, epoch, line["seq"])                       # H6-10: its own seq is its ref
    assert row == line["meta"]["lineage"]
    assert (row["count"], row["refused"], row["settled"], row["hold_microdollars"], row["status"]) == \
        (1000, "route-not-allowed", "no", 0, 0)
    for k in ("model_answered", "provider_answered", "tokens_in", "tokens_out", "cost_microdollars", "cost_reported",
              "overrun"):
        assert row[k] is None, k
    assert row["at"] <= row["ended_at"] and set(row) == set(LN.RECORD_CHECKS)
    assert _wire_ok(row) in ((None,) if _h621_landed() else ("model_answered",))


def test_refused_window_closes_on_its_timer_and_interactive_refusals_write_none(monkeypatch):
    # H6-10's window by its own timer: refusals inside it are one line; one after its close opens the next. A
    # parameterised name keeps its `:<what>` in the row, the ledger line names its head. An interactive session's
    # refusal (Q9) opens no window.
    async def go(d):
        async with KeysWorld(d, monkeypatch, refused_row_s=0.5) as w:
            for _ in range(3):
                await w.q(dict(TICKETED, routes=["glm", "r"]), route="r")
            await w.q(INTERACTIVE, route="nope")
            assert len(w.k._refusals) == 1
            await until(lambda: [o for o in w.lines() if o["kind"] == "refused"], 5)
            await w.q(dict(TICKETED, routes=["glm", "r"]), route="r")
            w.k._note_refusal("s1", RUN, "body-unbounded:document")
            w.k.flush_refusals()
            await until(lambda: len([o for o in w.lines() if o["kind"] == "refused"]) == 3, 5)
            return w.lines()

    lines = run_go(go)
    got = [(o["reason"], o["count"], o["meta"]["lineage"]["refused"]) for o in lines if o["kind"] == "refused"]
    assert got == [("route-not-allowed", 3, "route-not-allowed"), ("route-not-allowed", 1, "route-not-allowed"),
                   ("body-unbounded", 1, "body-unbounded:document")], got


def test_lineage_outbox_full_refuses_ticketed(monkeypatch):
    # H6-11: at LINEAGE_OUTBOX_MAX a ticketed `Q` is `keys-unavailable` with no hold; an interactive session is
    # unaffected; once acks drain the outbox, the ticketed `Q` is admitted again
    async def go(d):
        async with KeysWorld(d, monkeypatch, lineage_outbox_max=2) as w:
            futs = [w.ledger.refused("s1", "glm", "budget-exhausted", 1, {"lineage": {"n": i}})[1] for i in range(2)]
            await asyncio.gather(*futs)
            full = await w.q(TICKETED)
            reserved_after_full = len([o for o in w.lines() if o["kind"] == "reserved"])
            inter = await w.q(INTERACTIVE, route="r", body=IBODY)
            await w.ledger.ack([t for t, _ in w.ledger.unacked()])
            again = await w.q(TICKETED)
            return full, reserved_after_full, inter, again

    full, reserved_after_full, inter, again = run_go(go)
    assert _refused_name(full) == "keys-unavailable" and reserved_after_full == 0, full
    assert _types(inter) == b"ARbe" and _types(again) == b"ARbe"


def test_a_full_outbox_stays_at_its_bound_through_refusal_windows(monkeypatch):
    # r1 F1 (bound): with the outbox at `lineage_outbox_max`, each ticketed `Q` is refused `keys-unavailable` and its
    # window closes; the window's record does not fit, so it is COUNTED in `lineage_status()["dropped"]` and never
    # written. Window after window, the outbox stays at its bound and no `refused` line reaches the file.
    async def go(d):
        async with KeysWorld(d, monkeypatch, lineage_outbox_max=2, refused_row_s=3600.0) as w:
            await fill(w.ledger, [good_row(), good_row()])
            sizes, names = [], set()
            for _ in range(3):
                for _ in range(4):
                    names.add(_refused_name(await w.q(TICKETED)))
                w.k.flush_refusals()
                await until(lambda: not w.ledger._lines, 5, "drained")
                sizes.append(w.ledger.outbox_size)
            refused = [o for o in w.lines() if o["kind"] == "refused"]
            return names, sizes, w.k.lineage_status(), len(refused)

    names, sizes, status, refused = run_go(go)
    assert names == {"keys-unavailable"}
    assert sizes == [2, 2, 2] and status["outbox"] == 2
    assert status["dropped"] == 3 and refused == 2      # the two prefilled lines only
    assert "dropped" in (status["failure"] or "")


# ---- delivery against a real CONTROL (R4 §3 "Delivery"; H6-8, H6-11, H6-19) -----------------------------------------

class Ctl:
    """CONTROL as `control.serve` runs it, on a thread, with its lineage sink at `<d>/lineage/lineage.jsonl`; every
    connection to its socket is keys' (the `peer_uid` seam)."""

    def __init__(self, d: str) -> None:
        self.d = Path(d)
        (self.d / "sessions").mkdir(mode=0o700)
        (self.d / "lineage").mkdir(mode=0o750)
        self.sock = str(self.d / "c.sock")
        self.stop_event = threading.Event()
        self._ready = threading.Event()
        self.server: Any = None

    def _cb(self, server) -> None:
        self.server = server
        self._ready.set()

    def start(self) -> "Ctl":
        cfg = C.ServeConfig(store_dir=self.d / "sessions", socket_path=Path(self.sock), operator_uid=OP_UID,
                            keys_uid=KEYS_UID, peer_uid=lambda s: KEYS_UID, ready=self._cb, stop=self.stop_event,
                            lineage_dir=self.d / "lineage")
        self.thread = threading.Thread(target=lambda: C.serve(cfg), daemon=True, name="kl-control")
        self.thread.start()
        assert self._ready.wait(10), "CONTROL did not start"
        return self

    @property
    def control(self) -> C.Control:
        return self.server.control

    def rows(self) -> List[dict]:
        p = self.d / "lineage" / LN.SINK
        return [canon.parse(x) for x in p.read_bytes().split(b"\n")[:-1]] if p.exists() else []

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(10)


class Spy:
    """Every request keys sends on its attachment, in order, with its answer."""

    def __init__(self, k: K.Keys) -> None:
        self.calls: List[tuple] = []
        orig = k._attach_call

        async def spy(att, op, args, wait=None):
            ans = await orig(att, op, args, wait)
            size = len(canon.canonical_bytes(args["envelope"])) if op == "LINEAGE-APPEND" else None
            n = len(args["envelope"]["records"]) if op == "LINEAGE-APPEND" else None
            self.calls.append((op, size, n, ans))
            return ans

        k._attach_call = spy

    def ops(self, op: str) -> list:
        return [c for c in self.calls if c[0] == op]


def good_row(**over) -> dict:
    base = K.lineage_base(RUN, "2026-10-24T12:00:00Z", 100, False)
    row = K.lineage_row(base, settled="yes", ended_at="2026-10-24T12:00:01Z",
                        md=codec.Metadata("glm-5.2", "Z.AI", None), usage=codec.Usage(1, 2, 0, 0), cost=5,
                        status=200)
    row.update(over)
    return row


async def fill(led: L.Ledger, rows: List[dict]) -> List[tuple]:
    """`rows` as durable payload lines (the ledger carries any payload; the sender sends what the outbox holds)."""
    out = [led.refused("s1", "glm", "keys-unavailable", 1, {"lineage": r}, run_id=RUN["run_id"]) for r in rows]
    await asyncio.gather(*(f for _, f in out))
    return [t for t, _ in out]


def delivery(fn, **attrs):
    """`fn(ctl, keys, ledger, d)` with CONTROL up, the ledger open and `prefill(ledger)` done before keys attaches."""
    d = tempfile.mkdtemp(prefix="kd", dir="/tmp")

    async def go():
        ctl = Ctl(d).start()
        led = keys_ledger(os.path.join(d, "ledger.jsonl"))
        k: Optional[K.Keys] = None
        try:
            pre = attrs.pop("prefill", None)
            if pre is not None:
                await pre(led)
            k = K.Keys(ledger=led, attach=True, control_path=ctl.sock)
            for n, v in attrs.items():
                setattr(k, n, v)
            k._in_loop()
            spy = Spy(k)
            k.start_attach()
            await until(lambda: k.attached is not None, 10, "attached")
            return await fn(ctl, k, led, spy)
        finally:
            if k is not None:
                await k.shutdown()
            await led.aclose(5)
            await asyncio.get_running_loop().run_in_executor(None, ctl.stop)

    try:
        return asyncio.run(asyncio.wait_for(go(), 180))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_grammar_maximum_envelope_is_split_in_two_and_the_attachment_stays_up():
    # H6-19: 64 records whose every string is at its grammar's maximum encode past the frame cap; keys packs by
    # bytes too, so they go as two envelopes, each under LINEAGE_ENVELOPE_BYTES; CONTROL appends all 64 and the
    # attachment that carried them is the one keys opened
    big = good_row(job_id="j" * 128, model_requested="m" * 128, route="a" * 48, model_answered="~" * 128,
                   provider_answered="P" * 128, refused="r" + "x" * 63)
    assert _wire_ok(big) is None
    probe = K.lineage_envelope([(("a" * 32, "b" * 32, i), big) for i in range(64)], max_bytes=10 ** 9)[1]
    assert len(canon.canonical_bytes(probe)) > K.ADMIN_FRAME_MAX   # one envelope of them could not even be framed

    async def fn(ctl, k, led, spy):
        att = k.attached
        await until(lambda: led.outbox_size == 0, 30, "outbox drained")
        return att is k.attached, ctl.control.keys_attached(), spy.ops("LINEAGE-APPEND"), ctl.rows()

    async def prefill(led):
        await fill(led, [big] * 64)

    same, attach_no, sent, rows = delivery(fn, prefill=prefill)
    assert same and attach_no == 1
    assert len(sent) == 2 and sum(n for _, _, n, _ in sent) == 64
    assert all(size <= K.LINEAGE_ENVELOPE_BYTES for _, size, _, _ in sent)
    assert len(rows) == 64 and all("quarantined" not in r for r in rows)


def test_a_4096_record_resend_on_attach_open_keeps_the_sessions_ticks_alive():
    # H6-11: LINEAGE_OUTBOX_MAX records waiting at attach open go one envelope at a time (64 records, one fsync at
    # CONTROL, the next only after the ack), with a SESSIONS due every envelope: every tick is answered within its
    # (scaled) V23, so the attachment never ends, and every record reaches the sink once
    async def fn(ctl, k, led, spy):
        att = k.attached
        await until(lambda: led.outbox_size == 0, 120, "outbox drained")
        return att is k.attached, att.misses, ctl.control.keys_attached(), spy.calls, ctl.rows(), led

    async def prefill(led):
        await fill(led, [good_row()] * K.LINEAGE_OUTBOX_MAX)

    same, misses, attach_no, calls, rows, led = delivery(fn, prefill=prefill, reconcile_s=0.001, attach_answer_s=1.0)
    assert same and misses == 0 and attach_no == 1
    appends = [i for i, c in enumerate(calls) if c[0] == "LINEAGE-APPEND"]
    # each envelope at either bound (64 records, or the byte bound first: a typical row is ~750 bytes)
    assert sum(calls[i][2] for i in appends) == K.LINEAGE_OUTBOX_MAX
    assert all(calls[i][2] <= 64 and calls[i][1] <= K.LINEAGE_ENVELOPE_BYTES for i in appends)
    assert len(appends) <= K.LINEAGE_OUTBOX_MAX // 32
    assert all(c[3] is not None and c[3]["ok"] for c in calls), [c[:3] for c in calls if c[3] is None or not c[3]["ok"]]
    gaps = [any(calls[j][0] == "SESSIONS" for j in range(a + 1, b)) for a, b in zip(appends, appends[1:])]
    assert sum(gaps) >= len(gaps) // 2, gaps       # the ticks go between envelopes, not behind the resend
    assert len(rows) == K.LINEAGE_OUTBOX_MAX and len({json.dumps(r["source"]) for r in rows}) == len(rows)


def test_quarantine_and_a_refused_envelope_are_counted_in_keys_status(monkeypatch):
    # H6-8: a record CONTROL quarantines is acked (the outbox drains) with its quarantine in the `lineage-acked` line,
    # counted and noted as a FAILURE in keys' status; an envelope CONTROL refuses (`lineage-record:envelope`, a keys
    # bug) is counted, noted and sent again after `lineage_retry_s`, appended once
    real = K.lineage_envelope
    bad = {"n": 0}

    def once_bad(items, *a, **kw):
        got = real(items, *a, **kw)
        if got is not None and bad["n"] == 0:
            bad["n"] += 1
            sent, env = got
            return sent, dict(env, epoch="c" * 32)     # an element's source names another epoch: H6-14
        return got

    monkeypatch.setattr(K, "lineage_envelope", once_bad)

    async def fn(ctl, k, led, spy):
        # the outbox drains in the ack line's callback; the sender notes the quarantine on its next turn
        await until(lambda: led.outbox_size == 0 and "quarantined" in (k.lineage_failure or ""), 30, "drained")
        return k.lineage_status(), spy.ops("LINEAGE-APPEND"), ctl.rows(), file_lines(led.path)

    async def prefill(led):
        await fill(led, [good_row(), good_row(tokens_in=-1)])

    status, sent, rows, lines = delivery(fn, prefill=prefill, lineage_retry_s=0.05)
    assert [a["ok"] for _, _, _, a in sent] == [False, True]
    assert sent[0][3]["refused"] == "lineage-record:envelope"
    assert status["envelopes_refused"] == 1 and status["quarantined"] == 1 and status["outbox"] == 0
    assert "quarantined" in status["failure"] and "tokens_in" in status["failure"]
    assert len(rows) == 2 and [r.get("quarantined") for r in rows] == [None, "tokens_in"]
    (ack,) = [o for o in lines if o["kind"] == "lineage-acked"]
    assert len(ack["refs"]) == 2 and [q["field"] for q in ack["quarantined"]] == ["tokens_in"]


def test_flush_refusals_reaches_every_window_whatever_one_raises(monkeypatch):
    # r1 complement LOW 5: a window whose line raises anything (not only the ledger's two errors) loses its own counts,
    # noted; `flush_refusals` still reaches every other window
    async def go(d):
        async with KeysWorld(d, monkeypatch, refused_row_s=3600.0) as w:
            w.k._note_refusal("s1", RUN, "route-not-allowed")
            w.k._note_refusal("s1", RUN, "budget-exhausted")
            real, calls = w.ledger.refused, []

            def refused(*a, **kw):
                calls.append(a[2])
                if len(calls) == 1:
                    raise RuntimeError("a test's failed window")
                return real(*a, **kw)

            w.ledger.refused = refused
            w.k.flush_refusals()
            await until(lambda: not w.ledger._lines, 5, "drained")
            return calls, w.k._refusals, [o["reason"] for o in w.lines() if o["kind"] == "refused"]

    calls, left, written = run_go(go)
    assert len(calls) == 2 and left == {} and len(written) == 1 and written[0] == calls[1]


def test_end_meta_never_raises_even_on_its_holds_own_reads():
    # r1 F8: `_end_meta` is evaluated in `except BaseException` before a raise goes on, so its contract is NEVER
    # raises; the reads of `hold.state` and `hold.base` are inside it too. A hold whose reads raise gets None; one
    # whose row cannot be built gets the base-only row
    class Unreadable:
        @property
        def state(self):
            raise RuntimeError("a test's unreadable state")

        base = {"x": 1}

    class NoRow:
        state = L.RESERVED
        base = ["not", "a", "dict"]

    assert K.Keys._end_meta(None, K.Conn.__new__(K.Conn), Unreadable()) is None
    assert K.Keys._end_meta(None, K.Conn.__new__(K.Conn), NoRow()) is None


def test_a_malformed_ack_triple_is_not_an_ack_and_never_raises():
    # r1 F3 (a): `triple_of` takes only the on-disk triple shape (32-hex `ledger` and `epoch`, int `ref` >= 1); any
    # other answer is None, never a raise (an unhashable `ledger` once raised TypeError and killed the sender)
    t = ("a" * 32, "b" * 32, 1)
    good = {"ledger": t[0], "epoch": t[1], "ref": 1}
    assert K.read_lineage_ack({"ok": True, "acked": [good], "quarantined": []}, [t]) == ([t], {})
    for v in [dict(good, ledger=[]), dict(good, epoch={}), dict(good, ledger="A" * 32), dict(good, epoch="b" * 31),
              dict(good, ref=0), dict(good, ref=True), dict(good, ref="1"), dict(good, ref=[1])]:
        assert K.read_lineage_ack({"ok": True, "acked": [v], "quarantined": []}, [t]) is None, v
        assert K.read_lineage_ack({"ok": True, "acked": [good], "quarantined": [{"source": v, "field": "x"}]},
                                  [t]) is None, v


def _first_append(monkeypatch, change):
    """CONTROL's first `LINEAGE-APPEND` answer passed through `change` (the rest as CONTROL gave them)."""
    real = K.Keys._attach_call
    n = {"n": 0}

    async def call(self, att, op, args, wait=None):
        ans = await real(self, att, op, args, wait)
        if op == "LINEAGE-APPEND" and n["n"] == 0 and ans is not None:
            n["n"] += 1
            return change(ans)
        return ans

    monkeypatch.setattr(K.Keys, "_attach_call", call)
    return n


def test_a_malformed_ack_does_not_stop_the_sender(monkeypatch):
    # r1 F3 (a): an ack whose triple's `ledger` is a list is not an ack: noted, the envelope sent again after
    # `lineage_retry_s`, the outbox drains on the same attachment, and the FAILURE note clears after the good cycle
    n = _first_append(monkeypatch, lambda a: dict(a, acked=[dict(a["acked"][0], ledger=[])] + a["acked"][1:]))

    async def fn(ctl, k, led, spy):
        att = k.attached
        await until(lambda: led.outbox_size == 0, 10, "drained")
        await until(lambda: k.lineage_failure is None, 5, "the note cleared")
        return att is k.attached, k.lineage_status(), len(ctl.rows())

    async def prefill(led):
        await fill(led, [good_row(), good_row()])

    same, status, rows = delivery(fn, prefill=prefill, lineage_retry_s=0.05)
    assert n["n"] == 1 and same and rows == 2
    assert status["envelopes_refused"] == 1 and status["outbox"] == 0 and status["failure"] is None


def test_an_ack_the_ledger_cannot_take_is_retried_not_the_end_of_the_sender(monkeypatch):
    # r1 F3 (b): an exception from `led.ack` is noted and the envelope retried after `lineage_retry_s` (the outbox
    # still holds its triples), never the sender's exit
    async def fn(ctl, k, led, spy):
        await until(lambda: led.outbox_size == 0, 10, "drained")
        await until(lambda: k.lineage_failure is None, 5, "the note cleared")
        return calls["n"], k.lineage_status(), len(ctl.rows())

    calls = {"n": 0}

    async def prefill(led):
        await fill(led, [good_row()])
        real = led.ack

        def ack(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise L.LedgerUnavailable("a test's failed ack")
            return real(*a, **kw)

        led.ack = ack

    n, status, rows = delivery(fn, prefill=prefill, lineage_retry_s=0.05)
    assert n >= 2 and status["outbox"] == 0 and status["failure"] is None and rows == 1


def test_new_lines_are_sent_as_they_become_durable():
    # R4 §3: after the resend, each new payload line is sent once its line is durable (the ledger's listener)
    async def fn(ctl, k, led, spy):
        assert spy.ops("LINEAGE-APPEND") == []
        await fill(led, [good_row()])
        await until(lambda: led.outbox_size == 0 and len(ctl.rows()) == 1, 10, "first")
        await fill(led, [good_row(), good_row()])
        await until(lambda: led.outbox_size == 0 and len(ctl.rows()) == 3, 10, "second")
        return [n for _, _, n, _ in spy.ops("LINEAGE-APPEND")]

    assert delivery(fn) == [1, 2]


# ---- H6-31 / H6-32 (3c-6d): the rows the LEDGER builds, at the source ----------------------------------------------

_SECOND = __import__("re").compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


def _ledger_built(tmp_path, *, sent: bool, end: str) -> tuple:
    """One ticketed hold (keys' real `lineage_base`) that the LEDGER resolves itself: its owner task ends
    (`end="owner"`) or the ledger closes (`end="closed"`), SENT or not. Returns (its resolution line, its outbox
    entry)."""
    from tests.ledger_support import Owner
    path = os.path.join(str(tmp_path), "ledger.jsonl")
    base = K.lineage_base(RUN, "2026-10-24T12:00:00Z", 100, False)

    async def go():
        led = L.Ledger.open(path)
        led.attach(asyncio.get_running_loop())
        o = await Owner(led, meta={"lineage_base": base}).start()
        await o.hold.durable()
        if sent:
            o.hold.send_gate()
            o.hold.mark_sent()
        if end == "owner":
            await o.finish()
            await until(lambda: o.hold.state is L.RESOLVED, 10, "resolved")
            out = led.unacked()
            await led.aclose(5)
        else:
            await led.aclose(5)
            led2 = L.Ledger.open(path)
            out = led2.unacked()
            await led2.aclose(0)
            await o.finish()
        return out

    out = asyncio.run(go())
    (res,) = [o for o in file_lines(path) if o["kind"] in ("released", "uncertain")]
    assert len(out) == 1, out
    return res, out[0]


@pytest.mark.parametrize("end", ["owner", "closed"])
def test_a_ledger_built_row_is_acked_not_quarantined(tmp_path, end):
    # H6-31: a row the ledger builds itself (a SENT ticketed hold whose owner ended, or the ledger closed under it)
    # carries its stamps in CONTROL's second-precision form AS THE LEDGER WROTE IT: the record CONTROL receives is
    # the outbox payload plus its `source`, with nothing in keys re-cutting it, and CONTROL's own record check
    # (the sink's quarantine decision) accepts it
    res, (triple, payload) = _ledger_built(tmp_path, sent=True, end=end)
    assert res["kind"] == "uncertain" and res["reason"] == ("owner-ended" if end == "owner" else "closed")
    raw = dict(payload, source={"ledger": triple[0], "epoch": triple[1], "ref": triple[2]})
    assert LN.check_record(raw) is None, LN.check_record(raw)       # base 3e8aa639: quarantined "ended_at"
    assert _SECOND.fullmatch(payload["ended_at"]) and _SECOND.fullmatch(payload["at"]), payload
    assert payload["ended_at"] == res["at"][:19] + "Z"               # the line's own moment, cut to its second
    assert K.wire_record(triple, payload) == raw                     # keys sends the payload as the ledger wrote it


@pytest.mark.parametrize("end", ["owner", "closed"])
def test_an_unsent_holds_ledger_built_row_says_no_call(tmp_path, end):
    # H6-32 (H6-21): a ticketed hold the ledger resolves before any upstream call (owner-ended / closed while
    # RESERVED: `released`, settled "no") made no call, so its row's answered fields are null, not "unknown"; status 0
    res, (_t, payload) = _ledger_built(tmp_path, sent=False, end=end)
    assert res["kind"] == "released"
    assert payload["settled"] == "no" and payload["status"] == 0
    assert payload["model_answered"] is None and payload["provider_answered"] is None
    assert payload["cost_reported"] is None and payload["tokens_in"] is None
    assert set(payload) == set(LN.RECORD_CHECKS) - {"source"}
    assert _wire_ok(payload) in ((None,) if _h621_landed() else ("model_answered",))
    # a SENT hold's ledger-built row keeps "unknown": the call happened, the response did not say
    (tmp_path / "s").mkdir()
    _res2, (_t2, sent_payload) = _ledger_built(tmp_path / "s", sent=True, end=end)
    assert sent_payload["model_answered"] == "unknown" and sent_payload["provider_answered"] == "unknown"
