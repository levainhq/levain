"""keys' exchange against a scripted upstream (S1 session 3b-4, L3 r1's findings): what reaches the logs, the
credential's marker rule, the front's abort during the connect, an early final response during the upload, a detached
relay, and the connect and resolve deadlines. Each case drives the real `Keys._exchange` in-process; the upstream is
the seam above `_connect` (`_handshake`), scripted here."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import socket
import tempfile
import time

import pytest

from levain.broker import creds as CR
from levain.broker import gwwire
from levain.broker import keys as K
from levain.broker import redact as RD
from levain.broker import upstream as U
from levain.contracts import canon
from tests.ledger_support import keys_ledger

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets")

SECRET = "sk-upstream-test-SECRET-0123456789"
TOKEN = "T" * 43
CLAIMS = {"routes": ["r"], "front": {"uid": 70000, "pid": 1, "start": 1}, "incarnation": 1}

#: the fixtures' one priced model (R4 §1.3): rates in microdollars per million tokens
PRICE = {"in": 1_000_000, "out": 2_000_000, "cache_write": 1_250_000, "cache_read": 100_000, "default_output": 1000,
         "overhead_tokens": 10, "image_tokens": 100, "source": "https://example.com/pricing"}
#: the JSON body every exchange sends, already compact, so keys' own serialization of it is these bytes
BODY = b'{"model":"m1","max_tokens":10,"messages":[]}'
BODY_Q = "length:%d" % len(BODY)


def body_frames(body: bytes = BODY) -> bytes:
    """The front's `B` + `E` for one body."""
    return gwwire.encode(b"B", body) + gwwire.encode(b"E")


def big_body(n: int) -> bytes:
    """A JSON body of exactly `n` bytes (a padding string field), compact as `BODY` is."""
    head, tail = b'{"model":"m1","max_tokens":10,"messages":[],"pad":"', b'"}'
    return head + b"x" * (n - len(head) - len(tail)) + tail


def base_route(rid: str = "r", cap: int = 100000, *, body_cap: int = 4096, credential: "str | None" = "c1",
               headers: "dict | None" = None, codec: str = "anthropic-messages", prices: "dict | None" = None) -> dict:
    """One base `/2` brokered route on api.example.com with the `m1` price entry."""
    r = {"route": rid, "type": "brokered",
         "upstream": {"scheme": "https", "host": "api.example.com", "base_path": ""},
         "requests": {"templates": [{"method": "POST", "path": "/v1/messages"}],
                      "headers": {"content-type": "application/json"} if headers is None else headers,
                      "body_cap": body_cap, "body_codec": codec},
         "max_session_microdollars": cap, "prices": {"m1": PRICE} if prices is None else prices}
    if credential is not None:
        r["credential"] = credential
    return r


def _q(body: str = BODY_Q) -> dict:
    return {"v": 1, "token": TOKEN, "route": "r", "method": "POST", "path": "/v1/messages", "query": "",
            "headers": [["content-type", "application/json"]], "body": body, "upgrade": "none"}


def _lib(d: str) -> None:
    os.makedirs(os.path.join(d, "routes"))
    os.makedirs(os.path.join(d, "keys", "cred"))
    with open(os.path.join(d, "routes", "base.json"), "wb") as f:
        f.write(canon.canonical_bytes({"format": "levain-routes-base/2", "routes": [base_route()]}))
    with open(os.path.join(d, "keys", "cred", "c1.json"), "w") as f:
        f.write(json.dumps({"format": CR.FORMAT, "name": "c1", "rev": "0" * 32, "hosts": ["api.example.com"],
                            "inject": {"header": "x-api-key", "scheme": ""}, "kind": "static", "secret": SECRET}))


class _Tr:
    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        pass


class _Plain:
    def __init__(self) -> None:
        self.transport = _Tr()


class _Up:
    """The scripted upstream: `writes` records what keys sent; `drain` blocks forever from drain number `stall_at`
    (an upstream that stopped reading); `read` hands out `feed`'s items in order, one per call, b"" at EOF."""

    def __init__(self, stall_at: int = 0) -> None:
        self.transport = _Tr()
        self.writes: list = []
        self.drains = 0
        self.stall_at = stall_at
        self._q = None
        self.reads = 0
        self._read_called = None

    # built on first use, inside the running loop: on 3.9 an Event/Queue binds get_event_loop() at construction
    @property
    def q(self) -> asyncio.Queue:
        if self._q is None:
            self._q = asyncio.Queue()
        return self._q

    @property
    def read_called(self) -> asyncio.Event:
        if self._read_called is None:
            self._read_called = asyncio.Event()
        return self._read_called

    def feed(self, data: bytes) -> None:
        self.q.put_nowait(data)

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        self.drains += 1
        if self.stall_at and self.drains >= self.stall_at:
            await asyncio.Event().wait()
        await asyncio.sleep(0)

    async def read(self, n: int) -> bytes:
        self.reads += 1
        self.read_called.set()
        return await self.q.get()

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


class _Rig:
    """One claimed registration with the token cached, and one exchange connection whose front frames the test
    feeds (`front`) and whose keys-side bytes the test reads (`got()`)."""

    def __init__(self, d: str, **kw) -> None:
        self.d, self.kw = d, kw

    async def __aenter__(self) -> "_Rig":
        from levain.broker.creds import CredStore
        from levain.broker.norm import digest_of_token
        self.cred_fd = os.open(os.path.join(self.d, "keys", "cred"), os.O_RDONLY | os.O_DIRECTORY)
        self.a, self.b = socket.socketpair()
        self.ledger = keys_ledger(os.path.join(self.d, "keys", "ledger.jsonl"))
        k = self.k = K.Keys(ledger=self.ledger, lib_root=self.d, creds=CredStore(self.cred_fd))
        for n, v in self.kw.items():
            setattr(k, n, v)
        k._in_loop()
        k.admin = K.AdminConn(None, _AdminW(), k.admin_epoch)
        await k.front_register(k.admin_epoch, "s1", 70000, 1, 1, 1, 1)
        await k.front_arm(k.admin_epoch, "s1", 1)
        reg = k.regs[70000]
        reg.state = "claimed"
        k.cache[digest_of_token(TOKEN)] = {"session": "s1", "claims": dict(CLAIMS)}
        _r, self.w = await asyncio.open_unix_connection(sock=self.a)
        self.front = asyncio.StreamReader()
        self.conn = K.Conn(self.w, reg, "pending")
        reg.pending.add(self.conn)
        k.pool.add(self.conn)
        return self

    def run(self, q: dict) -> asyncio.Future:
        side = gwwire.KeysSide()
        side.got(b"Q")
        return asyncio.ensure_future(self.k._exchange(self.conn, gwwire.FrameReader(self.front), side, q))

    async def got(self) -> list:
        self.w.close()
        await asyncio.wait_for(self.w.wait_closed(), 2)
        self.b.settimeout(2)
        raw = b""
        while True:
            x = self.b.recv(65536)
            if not x:
                break
            raw += x
        out = []
        while raw:
            n = int.from_bytes(raw[1:5], "big")
            out.append((raw[:1], raw[5:5 + n]))
            raw = raw[5 + n:]
        return out

    async def __aexit__(self, *exc) -> None:
        self.a.close()
        self.b.close()
        os.close(self.cred_fd)
        await self.ledger.aclose(2.0)


class _AdminW:
    def __init__(self) -> None:
        self.transport = _Tr()

    def close(self) -> None:
        pass


def _scripted(monkeypatch, up: _Up, gate: "asyncio.Event | None" = None) -> dict:
    st = {"handshakes": 0}

    async def handshake(addr, port, host):
        st["handshakes"] += 1
        if gate is not None:
            await gate.wait()
        return up, up, _Plain()

    monkeypatch.setattr(U, "_handshake", handshake)
    monkeypatch.setattr(U, "_resolve", _one_address)
    return st


async def _one_address(host: str, port: int) -> list:
    return ["93.184.216.34"]


def _types(frames: list) -> bytes:
    return b"".join(t for t, _ in frames)


def _run(coro_fn):
    d = tempfile.mkdtemp(prefix="lu", dir="/tmp")
    _lib(d)
    try:
        return asyncio.run(asyncio.wait_for(coro_fn(d), 20))
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- L3 r1 codex HIGH 1 = complement MED 1: upstream bytes in a note -------------------------------------------------

_MALFORMED = {
    "header-line": b"HTTP/1.1 200 OK\r\nx-echo: " + SECRET.encode() + b"\x00\r\n\r\n",
    "status-line": b"HTTP/1.1 2" + SECRET.encode() + b" OK\r\n\r\n",
    "chunk-line": (b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n" + SECRET.encode() + b"\r\n"),
}


@pytest.mark.parametrize("where", sorted(_MALFORMED))
def test_a_malformed_response_echoing_the_secret_never_reaches_a_log(monkeypatch, where):
    # Reproduced first (3b4fix/repro_before.txt): h11's refusal text carries the offending bytes, BadResponse kept it
    # as `detail`, and the note wrote it to gw.jsonl and stderr. Now a refusal is logged by a fixed token only.
    up = _Up()
    _scripted(monkeypatch, up)
    stream = io.StringIO()
    lg = logging.getLogger("levain.broker")

    async def go(d: str) -> tuple:
        log = K.GwLog(d)
        handlers = [K.GwLogHandler(log), logging.StreamHandler(stream)]
        handlers[1].setLevel(logging.WARNING)
        old = lg.level
        lg.setLevel(logging.INFO)
        for h in handlers:
            lg.addHandler(h)
        try:
            async with _Rig(d) as rig:
                rig.k.gw_log = log
                rig.front.feed_data(body_frames())
                up.feed(_MALFORMED[where])
                up.feed(b"")
                await rig.run(_q())
                frames = await rig.got()
        finally:
            for h in handlers:
                lg.removeHandler(h)
            lg.setLevel(old)
        with open(log.path, "rb") as f:
            return frames, f.read()

    frames, gw = _run(go)
    assert SECRET.encode() not in gw, gw
    assert SECRET not in stream.getvalue(), stream.getvalue()
    assert b"upstream failed (BadResponse: protocol)" in gw, gw
    if where == "chunk-line":
        assert _types(frames) == b"AR"   # after `R` the body is cut short: no `F`
    else:
        assert frames[-1] == (b"F", gwwire.encode(b"F", {"refused": "upstream-error"})[5:])


# --- L3 r1 codex HIGH 2: a secret the marker can contain or rebuild -------------------------------------------------

@pytest.mark.parametrize("secret", ["levain", "]x", "d]x", "x[", "x[lev", "a[levain-redacted]b", "e"])
def test_a_secret_the_marker_could_hold_or_rebuild_is_refused_at_load(secret):
    # Reproduced first (3b4fix/repro_before.txt): a printable-ASCII secret passed the parse, and the scanner's output
    # still held it ("levain" inside the marker; "]x" rebuilt across the marker's end).
    raw = json.dumps({"format": CR.FORMAT, "name": "c1", "rev": "0" * 32, "hosts": ["api.example.com"],
                      "inject": {"header": "x-api-key", "scheme": ""}, "kind": "static", "secret": secret}).encode()
    with pytest.raises(CR.Unparseable):
        CR.parse(raw, "c1")


@pytest.mark.parametrize("secret", ["levain", "]x", "x[", "a[levain-redacted]b"])
def test_the_marker_rule_is_what_keeps_the_scanner_output_clean(secret):
    # The rule's other half: each refused secret does survive the scanner (so the refusal is load-bearing), and a
    # vendor-shaped key that passes the rule never does, whatever surrounds it.
    s = secret.encode()
    assert RD.unsafe_with_marker(s)
    sc = RD.Scanner([s])
    body = {"levain": b"x" + s, "]x": s + b"x", "x[": b"x" + s, "a[levain-redacted]b": b"a" + s + b"b"}[secret]
    assert s in sc.feed(body) + sc.flush()
    key = b"sk-ant-api03-AbC_dEf-0123456789"
    assert not RD.unsafe_with_marker(key)
    for wrap in (b"", b"[", b"]", RD.MARKER, b"[levain-redacted", b"levain-redacted]"):
        sc = RD.Scanner([key])
        out = b""
        for ch in (wrap + key + key + wrap + key[:5], key[5:] + wrap):
            out += sc.feed(ch)
        out += sc.flush()
        assert key not in out


# --- L3 r1 codex HIGH 3: the front's abort during the connect -------------------------------------------------------

def test_an_abort_during_the_handshake_sends_no_request(monkeypatch):
    # Reproduced first (3b4fix/repro_before.txt): keys read no front frame until its head had gone, so an `X` sent
    # while the TLS handshake was still in progress was found only after the credential-bearing head was written.
    up = _Up()
    gate = asyncio.Event
    holder: dict = {}

    async def go(d: str) -> tuple:
        holder["gate"] = gate()
        st = _scripted(monkeypatch, up, holder["gate"])
        async with _Rig(d) as rig:
            rig.front.feed_data(body_frames())
            task = rig.run(_q())
            for _ in range(200):
                if st["handshakes"]:
                    break
                await asyncio.sleep(0.005)
            rig.front.feed_data(gwwire.encode(b"X"))
            rig.front.feed_eof()
            await asyncio.sleep(0.05)
            holder["gate"].set()   # the handshake completes after the abort
            await task
            return await rig.got(), st["handshakes"]

    frames, hs = _run(go)
    assert hs == 1
    assert up.writes == [], up.writes                  # no request byte reached the upstream
    assert _types(frames) == b"A", frames               # accepted, then no answer to an aborted exchange


# --- L3 r1 codex MED 5: an early final response while the upload is blocked ------------------------------------------

def test_an_early_413_during_the_upload_is_relayed(monkeypatch):
    # Reproduced first (3b4fix/repro_before.txt): the exchange was upload-then-download; an upstream that answered
    # 413 after the head and stopped reading held keys in `drain()` forever.
    up = _Up(stall_at=2)   # the head's drain returns; the first body drain never does

    async def go(d: str) -> list:
        _scripted(monkeypatch, up)
        async with _Rig(d) as rig:
            big = big_body(4000)
            rig.front.feed_data(gwwire.encode(b"B", big[:3000]) + gwwire.encode(b"B", big[3000:])
                                + gwwire.encode(b"E"))
            up.feed(b"HTTP/1.1 413 Payload Too Large\r\ncontent-length: 4\r\n\r\nnope")
            await asyncio.wait_for(rig.run(_q("length:4000")), 5)
            return await rig.got()

    frames = _run(go)
    assert _types(frames) == b"ARbe", frames
    assert json.loads(frames[1][1])["status"] == 413 and frames[2][1] == b"nope"


class _ResetUp(_Up):
    """An upstream that answers 413 during the upload, then resets: the head reaches keys' reader, and the next body
    drain fails, so the upload ends in an upstream failure while the head is already there."""

    async def drain(self) -> None:
        self.drains += 1
        if self.drains == 2:   # the head's drain returned; the first body drain meets the reset
            # the head and the reset in one step: both tasks are done before the exchange looks at either
            self.feed(b"HTTP/1.1 413 Payload Too Large\r\ncontent-length: 4\r\n\r\nnope")
            raise ConnectionResetError(54, "Connection reset by peer")
        await asyncio.sleep(0)


def test_an_early_413_then_a_reset_relays_the_413(monkeypatch):
    # L3 r2 N2 (complement MED), reproduced first (3b4m/r2fix_before.txt): after the race, a failed upload was
    # consulted before a head that had already arrived, so the 413 became `upstream-error`. A completed head wins.
    up = _ResetUp()

    async def go(d: str) -> list:
        _scripted(monkeypatch, up)
        async with _Rig(d) as rig:
            big = big_body(4000)
            rig.front.feed_data(gwwire.encode(b"B", big[:3000]) + gwwire.encode(b"B", big[3000:])
                                + gwwire.encode(b"E"))
            await asyncio.wait_for(rig.run(_q("length:4000")), 5)
            return await rig.got()

    frames = _run(go)
    assert _types(frames) == b"ARbe", frames
    assert json.loads(frames[1][1])["status"] == 413 and frames[2][1] == b"nope"


# --- L3 r1 complement MED 2: a detached exchange relays nothing more -------------------------------------------------

def test_a_detached_exchange_relays_no_more_body(monkeypatch):
    # Reproduced first (3b4fix/repro_before.txt): only `R` checked `detached`; the `b` loop kept relaying what the
    # reader already held after a revoke or preemption had detached the exchange.
    up = _Up()

    async def go(d: str) -> list:
        _scripted(monkeypatch, up)
        async with _Rig(d) as rig:
            rig.front.feed_data(body_frames())
            up.feed(b"HTTP/1.1 200 OK\r\ncontent-length: 10\r\n\r\nabcde")
            task = rig.run(_q())
            while up.reads < 2:
                up.read_called.clear()
                await up.read_called.wait()
            rig.conn.detached = True   # phase 1, landing while the relay waits for the next body bytes
            up.feed(b"fghij")
            up.feed(b"")
            await task
            return await rig.got()

    frames = _run(go)
    assert _types(frames) == b"ARb", frames
    assert frames[2][1] == b"abcde"


# --- L3 r1 codex MED 6 / MED 7: the refresh's connect and the exchange's resolve, each bounded ------------------------

def test_refresh_connect_is_bounded(monkeypatch):
    # Reproduced first (3b4fix/repro_before.txt): `refresh_post`'s TCP connect had no timeout of its own.
    async def black_hole(addr, port):
        await asyncio.sleep(3600)

    monkeypatch.setattr(U, "_connect", black_hole)
    monkeypatch.setattr(U, "CONNECT_TIMEOUT_S", 0.2)

    async def go() -> float:
        t0 = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(U.refresh_post(U.RefreshRequest("93.184.216.34", 443, "h.example", b"", b"")), 5)
        return time.monotonic() - t0

    assert asyncio.run(go()) < 2.0


def test_a_hung_resolve_is_upstream_timeout(monkeypatch):
    # Reproduced first (3b4fix/repro_before.txt): the resolve ran with no deadline, so a hung getaddrinfo held the
    # exchange (and its reservation) with no answer.
    async def hung(host, port):
        await asyncio.sleep(3600)

    async def go(d: str) -> list:
        monkeypatch.setattr(U, "_resolve", hung)
        async with _Rig(d, connect_timeout_s=0.2) as rig:
            rig.front.feed_data(body_frames())
            await asyncio.wait_for(rig.run(_q()), 5)
            return await rig.got()

    frames = _run(go)
    assert _types(frames) == b"AF", frames
    assert json.loads(frames[1][1]) == {"refused": "upstream-timeout"}
