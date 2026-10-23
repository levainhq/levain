"""keys' metered request path on interactive base routes (S1 3c-2a, R4 §1.7; the §5 row's DONE tests and §8 tests 3
and 4): the absent ceiling written in and never shrunk, unproven content admitted and marked, the forwarded body is
keys' own serialization, an unpriced model or route refused before anything is held, the body buffer and its read
deadline, the `budget-stopped` flag at both reads, and a body keys cannot walk. Expected verdicts were written first
(`1023-8/3c2a/b1_expected.md`). The in-process cases drive the real `Keys._exchange` against the scripted upstream of
`test_broker_keys_upstream.py`; the concurrent one runs keys as a child process on the B2 rig (`tests/b2_keys_rig.py`).

Undischarged here: a real provider honouring the written-in ceiling, and a real provider's bill (R4 §6, 3c-10)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys

import pytest

from levain.broker import codec, gwwire
from levain.broker import keys as K
from levain.broker.norm import digest_of_token
from levain.broker.policy import parse_price
from levain.contracts import canon
from tests import b2_keys_rig as R
from tests.ledger_support import file_lines, keys_ledger
from tests.test_broker_keys_upstream import (BODY, PRICE, TOKEN, _AdminW, _q, _Rig, _run, _scripted, _types,
                                             _Up, base_route)

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets")

P = parse_price("m1", PRICE)
ANTHROPIC = codec.CODECS["anthropic-messages"]
CHAT = codec.CODECS["openai-chat"]


def _write_map(d: str, *routes: dict, fmt: str = "levain-routes-base/2") -> None:
    with open(os.path.join(d, "routes", "base.json"), "wb") as f:
        f.write(canon.canonical_bytes({"format": fmt, "routes": list(routes)}))


def _sent(up: _Up) -> tuple:
    """The upstream request keys wrote: (head bytes, body bytes)."""
    raw = b"".join(up.writes)
    head, _, body = raw.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", body


def _reserved(path: str) -> list:
    return [o for o in file_lines(path) if o["kind"] == "reserved"]


def _f(name: str) -> tuple:
    return (b"F", gwwire.encode(b"F", {"refused": name})[5:])


async def _one(d: str, monkeypatch, body: bytes, *, response: bytes = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok",
               q: "dict | None" = None, frames: "bytes | None" = None, **kw) -> dict:
    """One exchange on a fresh in-process keys over `d`: its front frames, what the upstream saw, the ledger's
    `reserved` lines (the ledger is closed before this returns, so every queued line is in the file)."""
    up = _Up()
    st = _scripted(monkeypatch, up)
    async with _Rig(d, **kw) as rig:
        rig.front.feed_data(frames if frames is not None else gwwire.encode(b"B", body) + gwwire.encode(b"E"))
        up.feed(response)
        up.feed(b"")
        await asyncio.wait_for(rig.run(q if q is not None else _q("length:%d" % len(body))), 10)
        got = await rig.got()
        path = rig.ledger.path
        buffered = rig.k.body_buffer.used if hasattr(rig.k, "body_buffer") else None   # None: a keys before 3c-2a
    return {"frames": got, "up": up, "handshakes": st["handshakes"], "reserved": _reserved(path),
            "kinds": [o["kind"] for o in file_lines(path)], "buffered": buffered}


# --- §8 test 4: the absent ceiling is written in, and a call that does not fit is refused, never shrunk ----------

def test_absent_ceiling_written_in_no_shrink(monkeypatch):
    # openai-chat with no ceiling: the upstream echo carries `max_completion_tokens` = the entry's `default_output`,
    # held at that estimate. Then, on the same session's scope (the ledger replays the first hold), a call whose
    # estimate exceeds the room: 402 `budget-exhausted`, no connection, never a smaller ceiling upstream.
    first = b'{"model":"m1","messages":[{"role":"user","content":"hi"}]}'
    obj = json.loads(first)
    assert CHAT.write_ceiling(obj, P)
    est1 = CHAT.estimate(obj, P)
    big = b'{"model":"m1","messages":[],"max_completion_tokens":%d}' % (P.default_output * 50)
    cap = est1 + CHAT.estimate(json.loads(big), P) - 1      # the second fits the cap alone, not the room left

    async def go(d: str) -> tuple:
        _write_map(d, base_route(cap=cap, codec="openai-chat"))
        one = await _one(d, monkeypatch, first)
        two = await _one(d, monkeypatch, big)
        return one, two

    one, two = _run(go)
    head, body = _sent(one["up"])
    assert _types(one["frames"]) == b"ARbe", one["frames"]
    assert json.loads(body)["max_completion_tokens"] == P.default_output
    assert body == codec.serialize(obj)
    assert [o["amount"] for o in one["reserved"]] == [est1]
    assert two["frames"] == [(b"A", b"{}"), _f("budget-exhausted")], two["frames"]
    assert two["handshakes"] == 0 and two["up"].writes == []
    assert [o["amount"] for o in two["reserved"]] == [est1]     # the first call's line only: nothing new admitted


# --- §8 test 3, its interactive half: unproven content admitted at its estimate, marked on its line --------------

def test_unproven_content_interactive_admitted_dispatch_refused(monkeypatch):
    # A `document` block, and a `web_search` tool with `max_uses`: both forwarded on the interactive route, each
    # `reserved` line's meta `{"bound": "unproven"}`, and the web_search hold includes max_uses × per_use. (The
    # dispatch half, `body-unbounded:<what>` before upstream, is 3c-5's: no dispatch map is loaded in 3c-2a.)
    priced = dict(PRICE, per_use={"web_search": 10_000})
    doc = (b'{"model":"m1","max_tokens":10,"messages":[{"role":"user","content":[{"type":"document",'
           b'"source":{"type":"text","media_type":"text/plain","data":"x"}}]}]}')
    search = (b'{"model":"m1","max_tokens":10,"messages":[],'
              b'"tools":[{"type":"web_search_20250305","name":"web_search","max_uses":3}]}')
    pp = parse_price("m1", priced)

    async def go(d: str) -> list:
        _write_map(d, base_route(prices={"m1": priced}))
        return [await _one(d, monkeypatch, b) for b in (doc, search)]

    outs = _run(go)
    for out, b in zip(outs, (doc, search)):
        assert _types(out["frames"]) == b"ARbe", out["frames"]
        assert _sent(out["up"])[1] == b                             # already compact: forwarded unchanged
    lines = outs[1]["reserved"]                                    # the second keys replayed the first's line
    assert [o["meta"] for o in lines] == [{"bound": "unproven"}, {"bound": "unproven"}]
    assert lines[0]["amount"] == ANTHROPIC.estimate(json.loads(doc), pp)
    no_tool_price = ANTHROPIC.estimate(json.loads(search), P)
    assert lines[1]["amount"] == ANTHROPIC.estimate(json.loads(search), pp) == no_tool_price + 3 * 10_000


def test_a_bounded_call_has_no_unproven_mark(monkeypatch):
    # the converse: the fixture body holds nothing the estimate cannot see, so its line's meta is empty
    async def go(d: str) -> dict:
        return await _one(d, monkeypatch, BODY)

    out = _run(go)
    assert [o["meta"] for o in out["reserved"]] == [{}]


# --- the forwarded body is keys' own serialization ----------------------------------------------------------------

def test_forwarded_body_is_canonical(monkeypatch):
    # A body sent with spaces, an escape and a chunked `Q`: the upstream reads `codec.serialize(json.loads(sent))`,
    # `content-length` is that length, and there is no `transfer-encoding` (keys frames what it serialized).
    sent = b'{ "model" : "m1",\n "messages": [{"role": "user", "content": "caf\\u00e9"}], "max_tokens": 10 }'
    want = codec.serialize(json.loads(sent))
    q = _q("chunked")

    async def go(d: str) -> dict:
        return await _one(d, monkeypatch, sent, q=q, frames=gwwire.encode(b"B", sent[:20])
                          + gwwire.encode(b"B", sent[20:]) + gwwire.encode(b"E"))

    out = _run(go)
    head, body = _sent(out["up"])
    assert _types(out["frames"]) == b"ARbe", out["frames"]
    assert body == want and body != sent and "café".encode() in body
    lines = [x.lower() for x in head.split(b"\r\n")]
    assert b"content-length: %d" % len(want) in lines, head
    assert not [x for x in lines if x.startswith(b"transfer-encoding")], head
    assert [o["amount"] for o in out["reserved"]] == [ANTHROPIC.estimate(json.loads(sent), P)]


# --- an unpriced model, and a route with no price table -------------------------------------------------------------

def test_unpriced_model_refused(monkeypatch):
    # model `m2` has no entry: `model-not-allowed` after the body is read, no connection, nothing held. A base `/1`
    # route (no `prices`): `model-not-allowed` at step 1, before `A` and before any body byte is buffered.
    m2 = BODY.replace(b'"m1"', b'"m2"')
    v1 = {k: v for k, v in base_route().items() if k != "prices"}
    v1["requests"] = dict(v1["requests"], body_codec="none")
    v1["max_call_microdollars"] = 1000

    async def go(d: str) -> tuple:
        a = await _one(d, monkeypatch, m2)
        _write_map(d, v1, fmt="levain-routes-base/1")
        b = await _one(d, monkeypatch, BODY)
        return a, b

    a, b = _run(go)
    assert a["frames"] == [(b"A", b"{}"), _f("model-not-allowed")], a["frames"]
    assert b["frames"] == [_f("model-not-allowed")], b["frames"]          # no `A`: refused at step 1
    for out in (a, b):
        assert out["handshakes"] == 0 and out["up"].writes == [] and "reserved" not in out["kinds"]
        assert out["buffered"] == 0


# --- the body buffer and the read deadline (r3 §2.2 c step 2, R4 §1.7 step 2) ----------------------------------------

class _Sessions:
    """In-process keys over `d` with `n` claimed registrations (sessions s0..s{n-1}, front uids 70000+i), each with
    its own token's claims cached. `start(i, q)` begins an exchange of session i: (front frames feeder, task, got)."""

    def __init__(self, d: str, n: int, **kw) -> None:
        self.d, self.n, self.kw = d, n, kw
        self.socks: list = []

    @staticmethod
    def token(i: int) -> str:
        return chr(ord("a") + i) * 43

    async def __aenter__(self) -> "_Sessions":
        from levain.broker.creds import CredStore
        self.cred_fd = os.open(os.path.join(self.d, "keys", "cred"), os.O_RDONLY | os.O_DIRECTORY)
        self.ledger = keys_ledger(os.path.join(self.d, "keys", "ledger.jsonl"))
        k = self.k = K.Keys(ledger=self.ledger, lib_root=self.d, creds=CredStore(self.cred_fd))
        for n, v in self.kw.items():
            setattr(k, n, v)
        k._in_loop()
        k.admin = K.AdminConn(None, _AdminW(), k.admin_epoch)
        for i in range(self.n):
            await k.front_register(k.admin_epoch, f"s{i}", 70000 + i, 1, 1, 1, 1)
            await k.front_arm(k.admin_epoch, f"s{i}", 1)
            k.regs[70000 + i].state = "claimed"
            k.cache[digest_of_token(self.token(i))] = {
                "session": f"s{i}", "claims": {"routes": ["r"], "front": {"uid": 70000 + i, "pid": 1, "start": 1},
                                               "incarnation": 1}}
        return self

    async def start(self, i: int, q: dict) -> tuple:
        a, b = socket.socketpair()
        self.socks += [a, b]
        _r, w = await asyncio.open_unix_connection(sock=a)
        front = asyncio.StreamReader()
        conn = K.Conn(w, self.k.regs[70000 + i], "pending")
        conn.reg.pending.add(conn)
        self.k.pool.add(conn)
        side = gwwire.KeysSide()
        side.got(b"Q")
        task = asyncio.ensure_future(self.k._exchange(conn, gwwire.FrameReader(front), side,
                                                      dict(q, token=self.token(i))))
        conn.task = task

        async def got() -> list:
            w.close()
            await asyncio.wait_for(w.wait_closed(), 2)
            b.settimeout(2)
            raw = b""
            while True:
                x = b.recv(65536)
                if not x:
                    break
                raw += x
            out = []
            while raw:
                n = int.from_bytes(raw[1:5], "big")
                out.append((raw[:1], raw[5:5 + n]))
                raw = raw[5 + n:]
            return out
        return front, task, got

    async def __aexit__(self, *exc) -> None:
        for s in self.socks:
            s.close()
        os.close(self.cred_fd)
        await self.ledger.aclose(2.0)


def test_dribbling_bodies_do_not_starve_other_sessions(monkeypatch):
    # Four sessions send chunked bodies one byte at a time; a fifth session's exchange is admitted and served while
    # they dribble (bytes are taken as they arrive, never `body_cap` up front). The dribblers end `body-timeout` at
    # the read deadline, none of them reserved, and the buffer is empty after.
    up = _Up()
    read_s = 0.6
    monkeypatch.setattr(K, "DRAIN_S", 0.2)   # a refused dribbler's front sends nothing more: end its drain soon

    async def go(d: str) -> dict:
        _scripted(monkeypatch, up)
        async with _Sessions(d, 5, body_read_s=read_s,
                             body_buffer=K.BodyBuffer(budget=4 * len(BODY), session_max=2 * len(BODY))) as ss:
            dribblers = [await ss.start(i, _q("chunked")) for i in range(4)]
            for _ in range(3):
                for front, _t, _g in dribblers:
                    front.feed_data(gwwire.encode(b"B", b"{"))
                await asyncio.sleep(0.05)
            used_mid = ss.k.body_buffer.used
            front5, task5, got5 = await ss.start(4, _q())
            front5.feed_data(gwwire.encode(b"B", BODY) + gwwire.encode(b"E"))
            up.feed(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
            up.feed(b"")
            await asyncio.wait_for(task5, 5)
            fifth = await got5()
            done = await asyncio.wait_for(asyncio.gather(*[t for _f, t, _g in dribblers]), read_s + 5)
            frames = [await g() for _f, _t, g in dribblers]
            out = {"used_mid": used_mid, "fifth": fifth, "frames": frames, "done": done,
                   "used_after": ss.k.body_buffer.used, "path": ss.ledger.path}
        out["reserved"] = _reserved(out["path"])
        return out

    out = _run(go)
    assert out["used_mid"] == 4 * 3                                # 3 bytes each, taken as they arrived
    assert _types(out["fifth"]) == b"ARbe", out["fifth"]
    for fr in out["frames"]:
        assert fr == [(b"A", b"{}"), _f("body-timeout")], fr
    assert [o["scope"][1] for o in out["reserved"]] == ["s4"]     # only the fifth session held anything
    assert out["used_after"] == 0


def test_a_session_past_its_share_is_refused_and_another_session_is_not(monkeypatch):
    # The per-session share: a declared length above it is `keys-unavailable` for that exchange only (before `A`,
    # nothing kept); another session's exchange is admitted at once.
    up = _Up()

    async def go(d: str) -> dict:
        _scripted(monkeypatch, up)
        async with _Sessions(d, 2, body_buffer=K.BodyBuffer(budget=4 * len(BODY), session_max=len(BODY))) as ss:
            over = BODY.replace(b'"messages"', b'"messages" ')    # one byte past the share
            f0, t0, g0 = await ss.start(0, _q("length:%d" % len(over)))
            f0.feed_data(gwwire.encode(b"B", over) + gwwire.encode(b"E"))
            await asyncio.wait_for(t0, 5)
            f1, t1, g1 = await ss.start(1, _q())
            f1.feed_data(gwwire.encode(b"B", BODY) + gwwire.encode(b"E"))
            up.feed(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
            up.feed(b"")
            await asyncio.wait_for(t1, 5)
            out = {"zero": await g0(), "one": await g1(), "used": ss.k.body_buffer.used, "path": ss.ledger.path}
        out["reserved"] = _reserved(out["path"])
        return out

    out = _run(go)
    assert out["zero"] == [_f("keys-unavailable")], out["zero"]
    assert _types(out["one"]) == b"ARbe", out["one"]
    assert [o["scope"][1] for o in out["reserved"]] == ["s1"] and out["used"] == 0


def test_body_read_deadline(monkeypatch):
    # A declared-length body that never ends: `body-timeout` (408) at `body_read_s` after `A`, nothing held, nothing
    # upstream, and the declared length taken up front is back in the buffer.
    monkeypatch.setattr(K, "DRAIN_S", 0.2)   # the front sends nothing more: end the refusal's drain soon

    async def go(d: str) -> dict:
        return await _one(d, monkeypatch, BODY, frames=gwwire.encode(b"B", BODY[:10]), body_read_s=0.3)

    out = _run(go)
    assert out["frames"] == [(b"A", b"{}"), _f("body-timeout")], out["frames"]
    assert out["handshakes"] == 0 and "reserved" not in out["kinds"] and out["buffered"] == 0
    from levain.broker import norm
    assert norm.status_of("body-timeout") == 408 and norm.REASON[408] == b"Request Timeout"


def test_buffer_budget_floor_admits_one_max_size_body_per_session():
    # [judged by 1023+8 head, 10-23] the session share must hold one body of the largest size a route may declare
    from levain.broker import policy
    assert K.BODY_BUFFER_SESSION_MAX >= policy.BODY_CAP_MAX
    assert K.BODY_BUFFER_SESSION_MAX * 4 == K.BODY_BUFFER_BUDGET


# --- `budget-stopped`: read at step 1, and again right before the admit ---------------------------------------------

@pytest.mark.parametrize("when", ["before-q", "during-body"])
def test_budget_stopped_session_is_refused_at_both_reads(monkeypatch, when):
    # R4 §1.4 / §10: the flag on the session's claims-cache entry (3c-2b sets it; here the test does). Set before the
    # `Q`: `budget-overrun` (402) at step 1, no `A`. Set while the body is being read (after step 1): the re-read
    # right before `ledger.admit` refuses it. Neither holds anything or reaches upstream.
    up = _Up()

    async def go(d: str) -> dict:
        st = _scripted(monkeypatch, up)
        async with _Rig(d) as rig:
            entry = rig.k.cache[digest_of_token(TOKEN)]
            if when == "before-q":
                entry["budget_stopped"] = True
                rig.front.feed_data(gwwire.encode(b"B", BODY) + gwwire.encode(b"E"))
                await asyncio.wait_for(rig.run(_q()), 5)
            else:
                rig.front.feed_data(gwwire.encode(b"B", BODY[:5]))
                task = rig.run(_q())
                await asyncio.sleep(0.05)                 # step 1 passed; the body read waits for the rest
                entry["budget_stopped"] = True
                rig.front.feed_data(gwwire.encode(b"B", BODY[5:]) + gwwire.encode(b"E"))
                await asyncio.wait_for(task, 5)
            out = {"frames": await rig.got(), "handshakes": st["handshakes"], "path": rig.ledger.path}
        out["kinds"] = [o["kind"] for o in file_lines(out["path"])]
        return out

    out = _run(go)
    want = [_f("budget-overrun")] if when == "before-q" else [(b"A", b"{}"), _f("budget-overrun")]
    assert out["frames"] == want, out["frames"]
    assert out["handshakes"] == 0 and up.writes == [] and "reserved" not in out["kinds"]


# --- a body keys cannot walk ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("where", ["parse", "estimate"])
def test_recursion_in_steps_4_5_is_body_json_with_no_hold(monkeypatch, where):
    # R4 §10: a `RecursionError` anywhere in parse / check / write_ceiling / estimate is `body-json` (400), no hold.
    # "parse": a body nested 100,000 deep (json raises RecursionError; 1,100 deep parses on 3.12, measured); "estimate":
    # the estimate itself raising it.
    if where == "parse":
        body = b'{"model":"m1","max_tokens":10,"messages":' + b"[" * 100_000 \
            + b"]" * 100_000 + b"}"
    else:
        body = BODY

        def deep(self, obj, price):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(codec.Codec, "estimate", deep)

    async def go(d: str) -> dict:
        _write_map(d, base_route(body_cap=len(body) + 1))
        return await _one(d, monkeypatch, body)

    out = _run(go)
    assert out["frames"] == [(b"A", b"{}"), _f("body-json")], out["frames"]
    assert out["handshakes"] == 0 and "reserved" not in out["kinds"] and out["buffered"] == 0


# --- the concurrent no-ceiling calls share the cap (the B2 process rig) --------------------------------------------

SYS_PY = "/usr/bin/python3"
PYS = [sys.executable] + ([SYS_PY] if os.path.exists(SYS_PY) and sys.executable != SYS_PY else [])


@pytest.fixture(params=PYS, ids=lambda p: "usr-bin-python3" if p == SYS_PY else "venv")
def py(request):
    if os.geteuid() == 0:
        pytest.skip("keys refuses to run as root (it runs as _levain_keys)")
    if not R.h11_available(request.param):
        pytest.skip("h11 is not importable by %s (named skip, not a pass)" % request.param)
    return request.param


async def _until(cond, timeout: float = 15.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < end, "condition not met within %s s" % timeout
        await asyncio.sleep(0.02)


def test_concurrent_no_ceiling_calls_share(py):
    # openai-chat, no ceiling, N concurrent `Q`s on an upstream holding every response open: each is held at the
    # estimate with `default_output` written in, so floor(cap / that estimate) = 4 reach upstream, every body
    # carrying `"max_completion_tokens":1000`, and the rest are 402 while the room is below one estimate.
    body = b'{"model":"m1","messages":[]}'
    obj = json.loads(body)
    CHAT.write_ceiling(obj, P)
    est = CHAT.estimate(obj, P)
    admitted, n = 4, 12

    async def go() -> dict:
        d = R.short_dir()
        R.lib(d, admitted * est + est // 2, codec="openai-chat")
        control = R.FakeControl(os.path.join(d, "control.sock"))
        await control.start()
        up = await R.FakeUpstream().start(True)
        keys = R.KeysProc(d, py, up.port)
        front = None
        try:
            await keys.start()
            front = await R.Front(keys).claim()
            tasks = []
            for i in range(n):
                tasks.append(asyncio.ensure_future(front.q(attempts=20, body=body)))
                if (i + 1) % 5 == 0:
                    await asyncio.sleep(0.15)
            await _until(lambda: up.heads >= admitted)
            await _until(lambda: sum(1 for t in tasks if t.done()) >= n - admitted)
            await asyncio.sleep(0.5)               # a fifth, wrongly admitted, would have connected by now
            mid = {"heads": up.heads, "bodies": list(up.bodies),
                   "lines": [(o["kind"], o.get("amount")) for o in R.ledger_lines(keys.ledger_path)]}
            up.release.set()
            res = await asyncio.wait_for(asyncio.gather(*tasks), 30)
            rc = keys.stop()
            return {"mid": mid, "res": res, "rc": rc}
        finally:
            if front is not None:
                await front.close()
            keys.cleanup()
            await up.close()
            await control.close()
            shutil.rmtree(d, ignore_errors=True)

    out = asyncio.run(asyncio.wait_for(go(), 120))
    mid, res = out["mid"], out["res"]
    assert mid["heads"] == admitted, mid
    assert mid["bodies"] == [codec.serialize(obj)] * admitted, mid["bodies"]
    assert all(b'"max_completion_tokens":1000' in b for b in mid["bodies"])
    assert mid["lines"].count(("reserved", est)) == admitted, mid["lines"]
    refused = [r.get("refused") for r in res if r.get("refused")]
    assert refused == ["budget-exhausted"] * (n - admitted), refused
    assert out["rc"] == 0
