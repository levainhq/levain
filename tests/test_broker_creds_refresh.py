"""keys' credential refresh (S1 session 3b, design 1.8, session 3b-5b): the one gate keyed by `(name, rev)`, the
single-flight owned by `CredWriter`, the one `refresh_post` caller, the response caps, the commit's compare, the 401
hook, the closing refusal and shutdown's cancel. Every refresh here goes through the REAL `upstream.refresh_post` to a
local TLS fake token endpoint (a self-signed certificate the patched client context trusts)."""
from __future__ import annotations

import asyncio
import gc
import json
import os
import shutil
import ssl
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from levain.broker import creds as C
from levain.broker import upstream
from levain.broker.keys import count_open_fds
from levain.broker.norm import Refused

HOST = "auth.example"
NAME = "vendor"


# --- the fake token endpoint -----------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    exe = shutil.which("openssl")
    if exe is None:
        pytest.skip("no openssl to make the fake endpoint's certificate")
    d = tmp_path_factory.mktemp("cert")
    subprocess.run([exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(d / "k.pem"), "-out",
                    str(d / "c.pem"), "-days", "2", "-subj", "/CN=" + HOST, "-addext", "subjectAltName=DNS:" + HOST],
                   check=True, capture_output=True)
    return d / "c.pem", d / "k.pem"


class Endpoint:
    """A token endpoint: counts calls; `mode` picks the answer; `hold` (an Event) delays it."""

    def __init__(self) -> None:
        self.calls = 0
        self.mode = "ok"
        self.expires_in = 3600
        self.rotate = True
        self.hold = None
        self.got = None   # made by `_serve`, inside the running loop (3.9 binds an Event at construction)
        self.server = None
        self.port = 0
        self.bodies = []

    async def handle(self, reader, writer) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            n = int([ln.split(b":", 1)[1] for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")][0])
            self.bodies.append(await reader.readexactly(n))
            self.calls += 1
            self.got.set()
            if self.hold is not None:
                await self.hold.wait()
            if self.mode == "reset":
                writer.write(b"HTTP/1.1 200 OK\r\n")
                await writer.drain()
                writer.transport.abort()
                return
            if self.mode == "fail":
                body = b'{"error":"invalid_grant"}'
                status = b"400 Bad Request"
            else:
                d = {"access_token": f"new-access-{self.calls}", "token_type": "Bearer", "expires_in": self.expires_in}
                if self.rotate:
                    d["refresh_token"] = f"new-refresh-{self.calls}"
                if self.mode == "big-token":
                    d["access_token"] = "a" * (C.VALUE_CAP + 1)
                if self.mode == "zero-expiry":
                    d["expires_in"] = 0
                body = json.dumps(d).encode()
                if self.mode == "big-body":
                    body = b'{"pad":"' + b"x" * (upstream.REFRESH_BODY_CAP + 10) + b'"}'
                status = b"200 OK"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            writer.close()
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass


@pytest.fixture
def env(tmp_path, cert, monkeypatch):
    """(endpoint, cred dir, CredStore, CredWriter factory). The client context trusts the fake's certificate; the
    resolution answers loopback (the front's address check would refuse it, and is not what this file tests)."""
    ctx = ssl.create_default_context(cafile=str(cert[0]))
    monkeypatch.setattr(upstream, "_tls_context", lambda: ctx)

    async def pick(host, port):
        assert host == HOST
        return "127.0.0.1"
    monkeypatch.setattr(upstream, "pick_address", pick)
    d = tmp_path / "cred"
    d.mkdir(mode=0o700)
    ep = Endpoint()
    fds = []

    def store():
        fd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
        fds.append(fd)
        return C.CredStore(fd)
    yield ep, d, store, cert, monkeypatch
    for fd in fds:
        os.close(fd)


async def _serve(ep: Endpoint, cert, monkeypatch) -> None:
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(str(cert[0]), str(cert[1]))
    ep.got = asyncio.Event()
    ep.server = await asyncio.start_server(ep.handle, "127.0.0.1", 0, ssl=sctx)
    ep.port = ep.server.sockets[0].getsockname()[1]
    monkeypatch.setattr(C, "TOKEN_PORT", ep.port)


def _fields(expires_at: int, access: str = "old-access", refresh_token: str = "old-refresh") -> dict:
    return {"format": C.FORMAT, "name": NAME, "hosts": [HOST, "api.example"], "inject":
            {"header": "authorization", "scheme": "Bearer"}, "kind": "oauth2-refresh", "access": access,
            "expires_at": expires_at, "refresh": {"token_url": f"https://{HOST}/oauth/token", "client_id": "cid",
                                                   "refresh_token": refresh_token}}


def _write(d: Path, fields: dict) -> str:
    fd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    try:
        out = C.commit(fd, NAME, fields, wait=True)
    finally:
        os.close(fd)
    return out.rev


def _remove(d: Path) -> None:
    fd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    try:
        C.remove(fd, NAME, wait=True)
    finally:
        os.close(fd)


def _file(d: Path) -> dict:
    return json.loads((d / (NAME + ".json")).read_bytes())


class Clock:
    """One fake time for the gate (monotonic) and for `expires_at` (wall)."""

    def __init__(self) -> None:
        self.t = 0.0
        self.base = time.time()

    def mono(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.base + self.t


def _writer(d: Path, clock: "Clock | None" = None) -> C.CredWriter:
    if clock is None:
        return C.CredWriter(str(d))
    return C.CredWriter(str(d), seams=C.WriteSeams(monotonic=clock.mono, clock=clock.wall))


async def _settle(w: C.CredWriter) -> None:
    """Wait for every refresh in flight to end."""
    while w._refresh_tasks:
        await asyncio.gather(*list(w._refresh_tasks), return_exceptions=True)


def run(coro):
    return asyncio.run(coro)


# --- row 8: single-flight, rotation, the compare --------------------------------------------------------------------

def test_an_expired_credential_wanted_by_n_concurrent_exchanges_makes_one_refresh(env):
    ep, d, store, cert, mp = env
    _write(d, _fields(int(time.time()) - 5))

    async def go():
        await _serve(ep, cert, mp)
        ep.hold = asyncio.Event()
        w, s = _writer(d), store()
        waits = [asyncio.ensure_future(C.fresh(w, s, NAME)) for _ in range(8)]
        await ep.got.wait()
        await asyncio.sleep(0.05)
        ep.hold.set()
        got = await asyncio.gather(*waits)
        w.close()
        return got
    got = run(go())
    assert ep.calls == 1
    assert all(c.secret == "new-access-1" for c in got)
    st = os.stat(d / (NAME + ".json"))
    assert stat.S_IMODE(st.st_mode) == 0o600
    f = _file(d)
    assert f["refresh"]["refresh_token"] == "new-refresh-1"   # rotation kept
    assert f["access"] == "new-access-1"
    body = ep.bodies[0].decode()
    assert "grant_type=refresh_token" in body and "refresh_token=old-refresh" in body and "client_id=cid" in body


def test_a_response_without_a_refresh_token_keeps_the_old_one(env):
    ep, d, store, cert, mp = env
    _write(d, _fields(int(time.time()) - 5))
    ep.rotate = False

    async def go():
        await _serve(ep, cert, mp)
        w, s = _writer(d), store()
        c = await C.fresh(w, s, NAME)
        w.close()
        return c
    assert run(go()).secret == "new-access-1"
    assert _file(d)["refresh"]["refresh_token"] == "old-refresh"


def test_n_concurrent_401s_on_the_current_generation_make_one_refresh(env):
    ep, d, store, cert, mp = env
    rev = _write(d, _fields(int(time.time()) + 3600))

    async def go():
        await _serve(ep, cert, mp)
        ep.hold = asyncio.Event()
        w, s = _writer(d), store()
        cred = s.observe(NAME)
        for _ in range(8):
            C.on_upstream_401(w, s, cred, rev)
        await ep.got.wait()
        ep.hold.set()
        await _settle(w)
        # a 401 on the OLD generation after the refresh landed: someone already refreshed, no call
        C.on_upstream_401(w, s, cred, rev)
        await _settle(w)
        w.close()
    run(go())
    assert ep.calls == 1
    assert _file(d)["access"] == "new-access-1"


@pytest.mark.parametrize("act", ["remove", "set", "remove-then-set"])
def test_an_operator_act_during_a_delayed_refresh_wins(env, act):
    ep, d, store, cert, mp = env
    _write(d, _fields(int(time.time()) - 5))

    async def go():
        await _serve(ep, cert, mp)
        ep.hold = asyncio.Event()
        w, s = _writer(d), store()
        t = asyncio.ensure_future(C.fresh(w, s, NAME))
        await ep.got.wait()
        if act in ("remove", "remove-then-set"):
            _remove(d)
        if act in ("set", "remove-then-set"):
            _write(d, _fields(int(time.time()) + 3600, access="operator-access"))
        ep.hold.set()
        try:
            return await t
        except Refused as r:
            return r.name
        finally:
            w.close()
    out = run(go())
    if act == "remove":
        assert out == "credential-unavailable"
        assert not (d / (NAME + ".json")).exists()
    else:
        assert out.secret == "operator-access"
        assert _file(d)["access"] == "operator-access"


def test_a_stale_tmp_from_a_killed_writer_does_not_block_the_next_refresh(env):
    ep, d, store, cert, mp = env
    _write(d, _fields(int(time.time()) - 5))
    stale = d / f".{NAME}.4242.deadbeef.tmp"
    stale.write_bytes(b"partial")
    os.utime(stale, (time.time() - 120, time.time() - 120))

    async def go():
        await _serve(ep, cert, mp)
        w, s = _writer(d), store()
        c = await C.fresh(w, s, NAME)
        w.close()
        return c
    assert run(go()).secret == "new-access-1"
    assert not stale.exists()


# --- row 8 (r2 W8): the one gate --------------------------------------------------------------------------------------

def test_a_failing_endpoint_and_50_sequential_exchanges_make_one_call_then_back_off(env):
    ep, d, store, cert, mp = env
    clk = Clock()
    _write(d, _fields(int(clk.wall()) - 5))
    ep.mode = "fail"

    async def go():
        await _serve(ep, cert, mp)
        w, s = _writer(d, clk), store()
        out = []
        for _ in range(50):
            try:
                await C.fresh(w, s, NAME)
                out.append("served")
            except Refused as r:
                out.append(r.name)
            clk.t += 0.5   # 50 exchanges inside the first 25 s
        calls_25 = ep.calls
        clk.t = 29.0
        with pytest.raises(Refused):
            await C.fresh(w, s, NAME)
        calls_29 = ep.calls
        clk.t = 31.0   # past 30 s x 2^0 from the first failure
        with pytest.raises(Refused):
            await C.fresh(w, s, NAME)
        calls_31 = ep.calls
        clk.t = 31.0 + 59.0   # the second failure shuts it for 30 s x 2^1
        with pytest.raises(Refused):
            await C.fresh(w, s, NAME)
        calls_90 = ep.calls
        clk.t = 31.0 + 61.0
        with pytest.raises(Refused):
            await C.fresh(w, s, NAME)
        w.close()
        return out, calls_25, calls_29, calls_31, calls_90, ep.calls
    out, c25, c29, c31, c90, c92 = run(go())
    assert out == ["credential-unavailable"] * 50
    assert (c25, c29, c31, c90, c92) == (1, 1, 2, 2, 3)


def test_the_backoff_is_capped_at_15_minutes():
    g = C._Gate("r")
    for _ in range(20):
        g.failed(0.0)
    assert g.shut_until == C.GATE_MAX_S


def test_a_short_lived_token_is_refreshed_at_most_once_per_30_s_and_served_meanwhile(env):
    ep, d, store, cert, mp = env
    clk = Clock()
    _write(d, _fields(int(clk.wall()) - 5))
    ep.expires_in = 10

    async def go():
        await _serve(ep, cert, mp)
        w, s = _writer(d, clk), store()
        seen = []
        for step in range(40):   # 40 exchanges, one every 0.5 s: 20 s
            try:
                seen.append((await C.fresh(w, s, NAME)).secret)
            except Refused as r:
                seen.append(r.name)
            clk.t += 0.5
        calls_20 = ep.calls
        clk.t = 30.5   # 30 s from the first attempt's start: the gate opens; the token (10 s) has expired
        c = await C.fresh(w, s, NAME)
        w.close()
        return seen, calls_20, c.secret, ep.calls
    seen, c20, last, c_end = run(go())
    assert c20 == 1
    assert set(seen[:18]) == {"new-access-1"}   # the still-valid token served while the gate is shut (<= 8.5 s)
    assert set(seen[20:]) == {"credential-unavailable"}   # expired (10 s) and the gate shut: 503, no call
    assert c_end == 2 and last == "new-access-2"


def test_a_new_rev_from_the_operator_reopens_a_shut_gate_and_the_same_rev_stays_shut(env):
    """Row 22's gate half (R91): a set-credential landing while the gate is shut after a failed refresh writes a new
    `rev`; the next exchange's refresh is attempted at once, with no failure count. Left alone it stays shut."""
    ep, d, store, cert, mp = env
    clk = Clock()
    _write(d, _fields(int(clk.wall()) - 5))
    ep.mode = "fail"

    async def go():
        await _serve(ep, cert, mp)
        w, s = _writer(d, clk), store()
        with pytest.raises(Refused):
            await C.fresh(w, s, NAME)
        clk.t = 5.0
        with pytest.raises(Refused):
            await C.fresh(w, s, NAME)   # the same rev: shut
        left_alone = ep.calls
        new_rev = _write(d, _fields(int(clk.wall()) - 1, access="operator-but-expired"))
        clk.t = 6.0
        with pytest.raises(Refused):
            await C.fresh(w, s, NAME)   # attempted at once
        g = w._gates[NAME]
        w.close()
        return left_alone, ep.calls, g.rev == new_rev, g.failures
    left_alone, calls, keyed, failures = run(go())
    assert left_alone == 1
    assert calls == 2
    assert keyed and failures == 1   # the new rev's own first failure, not the old count + 1


def test_refresh_connections_are_capped_by_the_refresh_semaphore(env, monkeypatch):
    ep, d, store, cert, mp = env
    monkeypatch.setattr(C, "REFRESH_CONNS", 2)
    names = ["vendor", "vendor-b", "vendor-c"]
    fd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    for n in names:
        f = _fields(int(time.time()) - 5)
        f["name"] = n
        C.commit(fd, n, f, wait=True)
    os.close(fd)

    async def go():
        await _serve(ep, cert, mp)
        ep.hold = asyncio.Event()
        w, s = _writer(d), store()
        waits = [asyncio.ensure_future(C.fresh(w, s, n)) for n in names]
        await asyncio.sleep(0.5)
        held = ep.calls
        ep.hold.set()
        got = await asyncio.gather(*waits)
        w.close()
        return held, ep.calls, got
    held, calls, got = run(go())
    assert held == 2 and calls == 3
    assert all(c.secret.startswith("new-access-") for c in got)


# --- R4: the refresh is detached ------------------------------------------------------------------------------------

def test_the_exchange_that_started_a_refresh_is_cancelled_and_the_refresh_still_commits(env):
    ep, d, store, cert, mp = env
    _write(d, _fields(int(time.time()) - 5))

    async def go():
        await _serve(ep, cert, mp)
        ep.hold = asyncio.Event()
        w, s = _writer(d), store()
        t = asyncio.ensure_future(C.fresh(w, s, NAME))
        await ep.got.wait()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        ep.hold.set()
        await _settle(w)
        later = await C.fresh(w, s, NAME)
        w.close()
        return later
    assert run(go()).secret == "new-access-1"
    assert ep.calls == 1


# --- R61 and shutdown's step (3) -------------------------------------------------------------------------------------

def test_a_refresh_wanted_after_closing_is_refused(env):
    ep, d, store, cert, mp = env
    _write(d, _fields(int(time.time()) - 5))

    async def go():
        await _serve(ep, cert, mp)
        w, s = _writer(d), store()
        w.begin_closing()
        with pytest.raises(Refused) as e:
            await C.fresh(w, s, NAME)
        assert w.want(s.observe(NAME)) is None
        w.close()
        return e.value.name
    assert run(go()) == "credential-unavailable"
    assert ep.calls == 0


def test_cancel_detached_ends_a_held_refresh_cancelled_with_no_open_transport(env):
    ep, d, store, cert, mp = env
    _write(d, _fields(int(time.time()) - 5))

    async def go():
        await _serve(ep, cert, mp)
        ep.hold = asyncio.Event()
        w, s = _writer(d), store()
        await asyncio.sleep(0)
        gc.collect()   # an earlier test's unreferenced fds would close mid-measure
        base = count_open_fds()
        waiter =asyncio.ensure_future(C.fresh(w, s, NAME))
        await ep.got.wait()
        during = count_open_fds()
        (task,) = list(w._refresh_tasks)
        w.begin_closing()
        w.cancel_detached()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(Refused):
            await waiter   # its wait is resolved, and the expired token is not served
        await asyncio.sleep(0.05)
        after = count_open_fds()
        ep.hold.set()
        ep.server.close()
        w.close()
        return task.cancelled(), base, during, after, w._inflight
    cancelled, base, during, after, inflight = run(go())
    assert cancelled
    assert during > base
    assert after == base
    assert inflight == {}


def test_a_refresh_failing_mid_response_leaves_no_open_transport(env):
    """R51: the endpoint resets the connection after its status line."""
    ep, d, store, cert, mp = env
    clk = Clock()
    _write(d, _fields(int(clk.wall()) - 5))
    ep.mode = "reset"

    async def go():
        await _serve(ep, cert, mp)
        w, s = _writer(d, clk), store()
        await asyncio.sleep(0)
        gc.collect()   # an earlier test's unreferenced fds would close mid-measure
        base = count_open_fds()
        with pytest.raises(Refused):
            await C.fresh(w, s, NAME)
        await asyncio.sleep(0.05)
        after = count_open_fds()
        g = w._gates[NAME]
        w.close()
        return base, after, g.failures
    base, after, failures = run(go())
    assert after == base
    assert failures == 1


# --- the response caps --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["big-token", "zero-expiry", "big-body", "fail"])
def test_a_response_past_a_cap_or_refused_is_a_failure_and_the_old_file_stays(env, mode):
    ep, d, store, cert, mp = env
    _write(d, _fields(int(time.time()) - 5))
    ep.mode = mode
    before = (d / (NAME + ".json")).read_bytes()

    async def go():
        await _serve(ep, cert, mp)
        w, s = _writer(d), store()
        with pytest.raises(Refused):
            await C.fresh(w, s, NAME)
        f = w._gates[NAME].failures
        w.close()
        return f
    assert run(go()) == 1
    assert (d / (NAME + ".json")).read_bytes() == before


def test_a_static_credential_never_refreshes(env):
    ep, d, store, cert, mp = env
    fd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    C.commit(fd, NAME, {"format": C.FORMAT, "name": NAME, "hosts": [HOST], "inject": {"header": "x-api-key",
             "scheme": ""}, "kind": "static", "secret": "k"}, wait=True)
    os.close(fd)

    async def go():
        await _serve(ep, cert, mp)
        w, s = _writer(d), store()
        c = await C.fresh(w, s, NAME)
        C.on_upstream_401(w, s, c, c.rev)
        await _settle(w)
        w.close()
        return c
    assert run(go()).secret == "k"
    assert ep.calls == 0


def test_a_valid_token_outside_the_window_is_served_with_no_call(env):
    ep, d, store, cert, mp = env
    _write(d, _fields(int(time.time()) + 3600))

    async def go():
        await _serve(ep, cert, mp)
        w, s = _writer(d), store()
        c = await C.fresh(w, s, NAME)
        w.close()
        return c
    assert run(go()).secret == "old-access"
    assert ep.calls == 0


def test_the_fake_lifecycle_serves_no_expired_token_and_refreshes_nothing(env):
    ep, d, store, cert, mp = env
    _write(d, _fields(int(time.time()) - 5))

    async def go():
        s = store()
        with pytest.raises(Refused):
            await C.fresh(C.FakeCredLifecycle(), s, NAME)
    run(go())


@pytest.mark.parametrize("url", [f"https://{HOST}/a b", f"https://{HOST}/a\r\nx: y", f"https://{HOST}/a#f"])
def test_a_token_url_path_outside_the_request_target_grammar_is_refused_before_any_byte(env, url):
    ep, d, store, cert, mp = env
    f = _fields(int(time.time()) - 5)
    f["refresh"]["token_url"] = url
    try:
        _write(d, f)
    except C.Unparseable:
        return   # the parse already refuses it (a control character): nothing to send
    async def go():
        await _serve(ep, cert, mp)
        w, s = _writer(d), store()
        with pytest.raises(Refused):
            await C.fresh(w, s, NAME)
        w.close()
    run(go())
    assert ep.calls == 0


# --- row 8 (r2 W4): a held lock never stalls the loop ----------------------------------------------------------------

def test_a_set_credential_holding_the_lock_2_s_does_not_stall_the_loop_and_the_commit_lands_after(env):
    ep, d, store, cert, mp = env
    _write(d, _fields(int(time.time()) - 5))
    import fcntl
    lock_fd = os.open(str(d / C.LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    t = threading.Timer(2.0, lambda: fcntl.flock(lock_fd, fcntl.LOCK_UN))

    async def go():
        await _serve(ep, cert, mp)
        w, s = _writer(d), store()
        gaps, last = [], time.monotonic()
        stop = False

        async def ticker():
            nonlocal last
            while not stop:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now
        tk = asyncio.ensure_future(ticker())
        t.start()
        t0 = time.monotonic()
        c = await C.fresh(w, s, NAME)
        took = time.monotonic() - t0
        stop = True
        await tk
        w.close()
        return c, max(gaps), took
    try:
        c, worst, took = run(go())
    finally:
        t.cancel()
        os.close(lock_fd)
    assert c.secret == "new-access-1"
    assert took >= 1.9   # the commit landed after the holder let go
    assert worst < 0.5   # the loop kept running meanwhile


# --- through keys: the REAL CredWriter as the keys server's `cred_lifecycle` (R83, R86) ----------------------------

def _kup():
    import importlib.util
    p = Path(__file__).resolve().parent / "test_broker_keys_upstream.py"
    spec = importlib.util.spec_from_file_location("kup_for_refresh", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _keys_case(env, expires_at: int, vendor: bytes):
    """One exchange through `Keys._exchange` on route `r` (credential `c1`, now an `oauth2-refresh` one) with the real
    CredWriter; the vendor is the scripted upstream, the token endpoint the real TLS fake."""
    ep, _d, _store, cert, mp = env
    kup = _kup()
    real_handshake = upstream._handshake
    up = kup._Up()

    async def pick(host, port):
        return "127.0.0.1" if host == HOST else "93.184.216.34"

    async def go(d: str):
        await _serve(ep, cert, mp)
        kup._scripted(mp, up)
        scripted = upstream._handshake

        async def handshake(addr, port, host):
            return await (real_handshake if host == HOST else scripted)(addr, port, host)
        mp.setattr(upstream, "_handshake", handshake)
        mp.setattr(upstream, "pick_address", pick)
        f = _fields(expires_at)
        f["name"] = "c1"
        f["hosts"] = ["api.example.com", HOST]
        cdir = os.path.join(d, "keys", "cred")
        os.unlink(os.path.join(cdir, "c1.json"))
        fd = os.open(cdir, os.O_RDONLY | os.O_DIRECTORY)
        C.commit(fd, "c1", f, wait=True)
        os.close(fd)
        w = C.CredWriter(cdir)
        async with kup._Rig(d, cred_lifecycle=w) as rig:
            rig.front.feed_data(kup.gwwire.encode(b"E"))
            rig.front.feed_eof()   # a refusal drains the front until EOF
            up.feed(vendor)
            await asyncio.wait_for(rig.run(kup._q("length:0")), 10)
            frames = await rig.got()
            await _settle(w)
            after = json.loads(open(os.path.join(cdir, "c1.json"), "rb").read())
        w.close()
        return frames, after
    frames, after = kup._run(go)
    return frames, after, up, ep


def test_through_keys_an_expired_credential_is_refreshed_before_the_exchange_goes_upstream(env):
    frames, after, up, ep = _keys_case(env, int(time.time()) - 5, b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
    assert b"".join(t for t, _ in frames) == b"ARbe", frames
    sent = b"".join(up.writes)
    assert b"authorization: Bearer new-access-1" in sent and b"old-access" not in sent
    assert ep.calls == 1 and after["access"] == "new-access-1"


def test_through_keys_a_401_is_answered_503_never_replayed_and_wants_one_refresh(env):
    frames, after, up, ep = _keys_case(env, int(time.time()) + 3600,
                                       b"HTTP/1.1 401 Unauthorized\r\ncontent-length: 0\r\n\r\n")
    types = b"".join(t for t, _ in frames)
    assert types == b"AF", frames
    assert b"credential-unavailable" in frames[1][1]
    assert b"".join(up.writes).count(b"POST /v1/messages") == 1   # never replayed
    assert ep.calls == 1 and after["access"] == "new-access-1"


# --- rule (c): EXACTLY ONE refresh_post caller (R76) ------------------------------------------------------------------

def test_rule_c_passes_with_one_caller_and_fails_on_none_and_on_two():
    import importlib.util
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("cap_ast", str(root / "tests" / "test_broker_capability_ast.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src = mod.load_sources(root)
    mods = {rel: mod.Module(rel, s) for rel, s in src.items()}
    assert mod.rule_c(mods) == []
    creds_src = src[mod.CREDS]
    assert creds_src.count("upstream.refresh_post(") == 1
    none = dict(src)
    none[mod.CREDS] = creds_src.replace("upstream.refresh_post(", "upstream.open_exchange_not(")
    assert any("callers" in p for p in mod.rule_c({r: mod.Module(r, s) for r, s in none.items()}))
    two = dict(src)
    two[mod.CREDS] = creds_src + "\n\nasync def _second(req):\n    return await upstream.refresh_post(req)\n"
    assert any("callers" in p for p in mod.rule_c({r: mod.Module(r, s) for r, s in two.items()}))
