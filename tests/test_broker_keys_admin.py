"""keys' admin server (S1 session 3b-3b, design 1.5): who may ask. Run in-process against keys' real listeners; the
preemption case drives them with CONTROL's own admin client, the EOF case with raw `levain-control/1` frames so the
frame order is the test's."""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
import time

import pytest

from levain.broker import gwwire
from levain.broker import keys as K
from levain.broker.peer import start_of
from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets")


def _hold(monkeypatch, holds: dict, entered: dict, cancels: list) -> None:
    """FRONT-REGISTER for a session in `holds` waits for that event first; with `swallow`, a cancel delivered while
    it waits is recorded and ignored, so the task outlives preemption's cancel and its bounded await."""
    orig = K.Keys.front_register

    async def held(self, epoch, session, *a):
        if session in holds:
            gate, swallow = holds[session]
            entered.setdefault(session, asyncio.Event()).set()
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    cancels.append(session)
                    if not swallow:
                        raise
        return await orig(self, epoch, session, *a)

    monkeypatch.setattr(K.Keys, "front_register", held)


async def _serve(d: str) -> tuple:
    me = os.getuid()
    k = K.Keys(single_uid=me, control_uid=me)
    front, admin = os.path.join(d, "f.sock"), os.path.join(d, "a.sock")
    socks = K.bind_listeners([(front, None), (admin, None)])
    await k.start(socks[:1], admin=socks[1])
    return k, front, admin


def _status(front: str, session: str) -> socket.socket:
    """A STATUS connection from this process (the registration names its pid and start): `H`, then keys' `A`."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(front)
    s.sendall(gwwire.encode(b"H", {"v": 1, "session": session, "incarnation": 1}))
    a = gwwire.encode(b"A")
    got = b""
    while len(got) < len(a):
        b = s.recv(len(a) - len(got))
        assert b, "keys closed the STATUS connection instead of `A`"
        got += b
    assert got == a
    return s


def _rest(s: socket.socket) -> bytes:
    out = b""
    while True:
        b = s.recv(65536)
        if not b:
            return out
        out += b


def test_admin_new_connection_preempts(monkeypatch):
    d = tempfile.mkdtemp(prefix="lk", dir="/tmp")
    holds, entered, cancels = {}, {}, []
    _hold(monkeypatch, holds, entered, cancels)
    pid, start = os.getpid(), start_of(os.getpid())

    async def run() -> dict:
        loop = asyncio.get_running_loop()
        ex = lambda f, *a: loop.run_in_executor(None, f, *a)   # noqa: E731
        k, front, admin = await _serve(d)
        holds["s-held"] = (asyncio.Event(), True)
        a1, a2 = C.KeysAdmin(admin, answer_s=1.0), C.KeysAdmin(admin, answer_s=1.0)
        try:
            e1 = await ex(a1.front_register, "s-old", 70000, pid, start, 1, 3)
            await ex(a1.front_arm, "s-old", 3)
            st = await ex(_status, front, "s-old")
            held = ex(a1.front_register, "s-held", 70001, 1, 1, 1, 1)
            await asyncio.wait_for(entered.setdefault("s-held", asyncio.Event()).wait(), 5)
            t0 = time.monotonic()
            e2 = await ex(a2.front_register, "s-new", 70002, 2, 2, 1, 1)   # the new connection: preempts
            took = time.monotonic() - t0
            k_then_eof = await ex(_rest, st)
            try:
                await asyncio.wait_for(held, 10)
                held_out = "answered"
            except Refused as e:
                held_out = e.name
            holds["s-held"][0].set()                    # released past the clear: its epoch is stale
            await asyncio.sleep(0.2)
            after_release = sorted(k.regs)
            try:
                await ex(a2.front_register, "s-old2", 70000, pid, start, 1, 3)
                fence = None
            except C.AdminRefused as e:
                fence = (e.name, e.answer.get("reg_high"))
            await ex(a2.front_register, "s-new5", 70002, 2, 2, 1, 5)
            try:
                await ex(a2.front_register, "s-new4", 70002, 2, 2, 1, 4)
                lower = None
            except C.AdminRefused as e:
                lower = (e.name, e.answer.get("reg_high"))
            return {"epochs": (e1, e2), "took": took, "k_then_eof": k_then_eof, "held": held_out,
                    "cancels": list(cancels), "after_release": after_release, "fence": fence, "lower": lower,
                    "survivor": k.regs[70002].session, "admin_epoch": k.admin_epoch}
        finally:
            for gate, _swallow in holds.values():   # a failed run must not leave a hold spinning in loop teardown
                gate.set()
            a1.close()
            a2.close()
            await asyncio.wait_for(k.shutdown(), 10)

    try:
        out = asyncio.run(run())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PREEMPT", out)
    assert out["epochs"] == (1, 1)                      # each KeysAdmin's own first connection
    assert out["admin_epoch"] == 2                      # keys adopted two connections
    assert out["cancels"] == ["s-held"]                 # step (2)'s cancel reached the held task, which ignored it
    assert K.PREEMPT_WAIT_S - 0.1 <= out["took"] < K.PREEMPT_WAIT_S + 2   # step (2)'s await is bounded
    assert out["k_then_eof"] == gwwire.encode(b"K")     # the old connection's front got `K`, then EOF
    assert out["held"] == "keys-unavailable"            # the preempted connection's caller saw it end
    assert out["after_release"] == [70002]              # the held task, released after the clear, made nothing
    assert out["fence"] == ("registration-stale", 3)    # `reg_high` survives a preemption
    assert out["lower"] == ("registration-stale", 5)    # a lower `reg_token` after a higher one is refused
    assert out["survivor"] == "s-new5"                  # ... and the newer registration is intact


async def _send(w: asyncio.StreamWriter, rid: int, op: str, **args) -> None:
    w.write(K.encode_admin({"v": 1, "op": op, "id": rid, **args}))
    await w.drain()


async def _answer(r: asyncio.StreamReader) -> dict:
    n = int.from_bytes(await asyncio.wait_for(r.readexactly(4), 5), "big")
    return canon.parse(await r.readexactly(n))


class _Recorder(asyncio.Protocol):
    """A raw client: every received byte is kept, and the connection's end, EOF or reset, is the one terminal event."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.got = bytearray()
        self.lost: asyncio.Future = loop.create_future()

    def data_received(self, data: bytes) -> None:
        self.got += data

    def connection_lost(self, exc) -> None:
        if not self.lost.done():
            self.lost.set_result(exc)


async def _foreign_peer(loop: asyncio.AbstractEventLoop, admin: str) -> bytes:
    """Connect, send one frame, wait (one deadline for the whole exchange) for the connection to end, and return what
    arrived. A stream reader raises a stored reset before it returns bytes it already buffered, so it could not tell
    "closed unread" from "answered, then reset"; a protocol sees `data_received` first, whatever ends the connection."""
    deadline = loop.time() + 5
    transport, rec = await asyncio.wait_for(loop.create_unix_connection(lambda: _Recorder(loop), admin), 5)
    try:
        # a send-side close is just another terminal event: it never fails the test
        transport.write(K.encode_admin({"v": 1, "op": "DROP-SESSION", "id": 1, "session": "s-x"}))
        await asyncio.wait_for(rec.lost, max(0.0, deadline - loop.time()))
    finally:
        transport.abort()
    return bytes(rec.got)


def test_admin_eof_kills_cached_tokens_and_fronts(monkeypatch):
    d = tempfile.mkdtemp(prefix="lk", dir="/tmp")
    holds, entered, cancels = {}, {}, []
    _hold(monkeypatch, holds, entered, cancels)
    pid, start = os.getpid(), start_of(os.getpid())

    async def run() -> dict:
        loop = asyncio.get_running_loop()
        k, front, admin = await _serve(d)
        out: dict = {}
        try:
            # a peer that is not `_levain_control` is closed unread and unanswered. Closed unread, its frame can still
            # sit in keys' receive queue at the close: Linux then resets the peer, macOS gives it EOF. Either is
            # "closed"; an answer, or the connection staying open, is not. `_foreign_peer` records every byte the peer
            # receives, so an answer followed by a reset still fails.
            k.control_uid = os.getuid() + 1
            out["foreign"] = await _foreign_peer(loop, admin)
            k.control_uid = os.getuid()

            r, w = await asyncio.open_unix_connection(admin)
            await _send(w, 1, "RELOAD", generation=1)
            await _send(w, 2, "FRONT-ARM", session="s-a", reg_token=1, extra=1)
            out["refusals"] = sorted([await _answer(r), await _answer(r)], key=lambda a: a["id"])
            # one session's requests run in frame order: the DROP read second runs second
            holds["s-a"] = (asyncio.Event(), False)
            await _send(w, 3, "FRONT-REGISTER", session="s-a", front_uid=70001, pid=1, start=1, incarnation=1,
                        reg_token=1)
            await _send(w, 4, "DROP-SESSION", session="s-a")
            await asyncio.wait_for(entered.setdefault("s-a", asyncio.Event()).wait(), 5)
            await asyncio.sleep(0.1)
            holds["s-a"][0].set()
            out["order"] = [(await _answer(r))["id"], (await _answer(r))["id"]]
            out["after_order"] = sorted(k.regs)
            # a live front, and a request of this connection still in flight at its EOF
            await _send(w, 5, "FRONT-REGISTER", session="s-b", front_uid=70000, pid=pid, start=start, incarnation=1,
                        reg_token=1)
            await _send(w, 6, "FRONT-ARM", session="s-b", reg_token=1)
            out["armed"] = [await _answer(r), await _answer(r)]
            st = await loop.run_in_executor(None, _status, front, "s-b")
            holds["s-c"] = (asyncio.Event(), True)
            await _send(w, 7, "FRONT-REGISTER", session="s-c", front_uid=70002, pid=2, start=2, incarnation=1,
                        reg_token=1)
            await asyncio.wait_for(entered.setdefault("s-c", asyncio.Event()).wait(), 5)
            w.close()                                     # CONTROL's EOF
            out["k_then_eof"] = await loop.run_in_executor(None, _rest, st)
            out["after_eof"] = sorted(k.regs)
            holds["s-c"][0].set()
            await asyncio.sleep(0.2)
            out["after_release"] = sorted(k.regs)
            out["admin"] = k.admin
            return out
        finally:
            for gate, _swallow in holds.values():   # a failed run must not leave a hold spinning in loop teardown
                gate.set()
            await asyncio.wait_for(k.shutdown(), 10)

    try:
        out = asyncio.run(run())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("EOF", out)
    assert out["foreign"] == b""
    assert out["refusals"] == [{"id": 1, "ok": False, "refused": K.RELOAD_REFUSED},
                               {"id": 2, "ok": False, "refused": "unknown-key"}]
    assert out["order"] == [3, 4] and out["after_order"] == []
    assert out["armed"] == [{"id": 5, "ok": True}, {"id": 6, "ok": True}]
    assert out["k_then_eof"] == gwwire.encode(b"K")       # EOF: the front got `K`, then its connection closed
    assert out["after_eof"] == []                         # every registration cleared
    assert out["after_release"] == []                     # the in-flight request, released after the EOF, made none
    assert out["admin"] is None


def test_admin_socket_group_admits_keys_and_control():
    """keys binds admin.sock as itself and gives it ADMIN_GROUP: a non-root process may only do that for a group it
    is in, so the install's membership table must put keys in it (else keys exits at startup), and CONTROL, the
    one admitted peer, must reach the socket through it. It is a shared group the install makes, never an
    account's own primary group, and nobody else is in it."""
    from levain.slot.accounts import OPERATOR_GROUPS, supplementary_groups
    from levain.slot.ids import SHARED_GROUPS, SINGLETONS
    assert K.ADMIN_GROUP == "levain-keysadmin" and K.ADMIN_GROUP in SHARED_GROUPS
    assert K.FRONTS_GROUP in supplementary_groups(K.KEYS_ACCOUNT)
    everyone = (*SINGLETONS, *(f"_levain_{p}{i}" for p in ("s", "gw", "v", "c") for i in (0, 1)))
    assert {n for n in everyone if K.ADMIN_GROUP in supplementary_groups(n)} == {K.KEYS_ACCOUNT, K.CONTROL_ACCOUNT}
    assert K.ADMIN_GROUP not in OPERATOR_GROUPS


def test_admin_stalled_peer_closed_within_bound(monkeypatch):
    """A CONTROL that stops mid-frame, or that stops reading its answers, is closed within ADMIN_FRAME_S: the
    connection's EOF, so everything clears."""
    monkeypatch.setattr(K, "ADMIN_FRAME_S", 0.5)
    d = tempfile.mkdtemp(prefix="lk", dir="/tmp")

    async def until_cleared(k, secs: float):
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            if k.admin is None:
                return time.monotonic() - t0
            await asyncio.sleep(0.05)
        return None

    async def run() -> dict:
        k, _front, admin = await _serve(d)
        out: dict = {}
        try:
            # (a) two bytes of a length header, then nothing
            r, w = await asyncio.open_unix_connection(admin)
            await _send(w, 1, "RELOAD", generation=1)
            await _answer(r)
            w.write(b"\x00\x00")
            await w.drain()
            out["header"] = await until_cleared(k, 3.0)
            out["header_eof"] = await asyncio.wait_for(r.read(), 2)
            w.close()
            # (b) requests sent in small batches, no answer ever read; the in-flight cap is lifted so the bound this
            # measures is the write's own
            monkeypatch.setattr(K, "ADMIN_INFLIGHT_MAX", 10 ** 6)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
            s.connect(admin)
            r, w = await asyncio.open_unix_connection(sock=s)
            await _send(w, 1, "RELOAD", generation=1)
            await _answer(r)                              # adopted; from here no answer is read
            t0, rid = time.monotonic(), 1
            try:
                while k.admin is not None and time.monotonic() - t0 < 5.0:
                    for _ in range(20):
                        rid += 1
                        w.write(K.encode_admin({"v": 1, "op": "RELOAD", "id": rid, "generation": 1}))
                    await asyncio.wait_for(w.drain(), 2.0)   # keys may stop reading: never hang here
                    await asyncio.sleep(0.005)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                pass
            out["unread"] = await until_cleared(k, 2.0)
            out["unread_after"] = time.monotonic() - t0
            w.close()
            return out
        finally:
            await asyncio.wait_for(k.shutdown(), 10)

    try:
        out = asyncio.run(run())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("STALLED", out)
    assert out["header"] is not None and out["header"] < 0.5 + 1.0   # the header is under the frame's deadline
    assert out["header_eof"] == b""
    assert out["unread"] is not None and out["unread_after"] < 5.0     # a write that cannot drain closes it


# --- P4: the `Q` handler's token states ------------------------------------------------------------------------------

def _tok(n: int) -> str:
    return f"{n:043d}"


def _by_path(monkeypatch) -> None:
    """Peers are told apart by the path each client socket binds before it connects: `A*` is (pid 1, start 1), `B*`
    (pid 2, start 2), so two sessions' fronts can live in this one process."""
    def peer(sock):
        name = os.path.basename(sock.getpeername())
        return (os.getuid(), 1, 1) if name.startswith("A") else (os.getuid(), 2, 2)

    monkeypatch.setattr(K, "peer_of", peer)


def _client(d: str, name: str, front: str) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.bind(os.path.join(d, name))
    s.connect(front)
    return s


def _claim(d: str, name: str, front: str, session: str) -> socket.socket:
    s = _client(d, name, front)
    s.sendall(gwwire.encode(b"H", {"v": 1, "session": session, "incarnation": 1}))
    assert s.recv(64) == gwwire.encode(b"A")
    return s


def _q(token: str) -> bytes:
    return gwwire.encode(b"Q", {"v": 1, "token": token, "route": "r", "method": "GET", "path": "/", "query": "",
                                "headers": [], "body": "none", "upgrade": "none"}) + gwwire.encode(b"E")


class _FakeControl:
    """CONTROL's socket for TOKEN-RESOLVE: `held` digests are answered only when `release` is set, with `answers`;
    others at once. `inflight` counts requests per digest prefix class while held."""

    def __init__(self, answers: dict, held: set) -> None:
        self.answers, self.held = answers, held
        self.release = asyncio.Event()
        self.seen: list = []
        self.inflight = 0
        self.peak = 0

    async def handle(self, reader, writer) -> None:
        try:
            n = int.from_bytes(await reader.readexactly(4), "big")
            req = canon.parse(await reader.readexactly(n))
            digest = req["token_digest"]
            self.seen.append(digest)
            if digest in self.held:
                self.inflight += 1
                self.peak = max(self.peak, self.inflight)
                try:
                    await self.release.wait()
                finally:
                    self.inflight -= 1
            writer.write(K.encode_admin({"id": req["id"], **self.answers.get(digest, {"ok": False,
                                                                                     "refused": "token-unknown"})}))
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def _keys_with_control(d: str, fake: _FakeControl) -> tuple:
    ctl = os.path.join(d, "c.sock")
    srv = await asyncio.start_unix_server(fake.handle, path=ctl)
    me = os.getuid()
    k = K.Keys(single_uid=me, control_uid=me, control_path=ctl)
    k.pending_per_peer = 64
    front, admin = os.path.join(d, "f.sock"), os.path.join(d, "a.sock")
    socks = K.bind_listeners([(front, None), (admin, None)])
    await k.start(socks[:1], admin=socks[1])
    return k, srv, front, admin


def test_flooding_session_leaves_another_sessions_first_q_resolved(monkeypatch):
    # R6 (design 1.5 "Resolve slots"): a front sending 32 unknown tokens, every resolve held unanswered at CONTROL,
    # leaves another session's first `Q` resolved inside the deadline; F-B row 21 (keys half): that `Q`'s token is
    # unknown, so the real handler answers `token-invalid` through the one `F` path and drains to the front's `E`.
    _by_path(monkeypatch)
    d = tempfile.mkdtemp(prefix="lk", dir="/tmp")
    from levain.broker.norm import digest_of_token
    flood = {digest_of_token(_tok(i)) for i in range(32)}
    fake = _FakeControl({}, flood)

    async def run() -> dict:
        loop = asyncio.get_running_loop()
        ex = lambda f, *a: loop.run_in_executor(None, f, *a)   # noqa: E731
        k, srv, front, admin = await _keys_with_control(d, fake)
        a = C.KeysAdmin(admin, answer_s=2.0)
        socks = []
        try:
            for sess, uid, n in (("s-flood", 70000, 1), ("s-other", 70001, 2)):
                await ex(a.front_register, sess, uid, n, n, 1, 1)
                await ex(a.front_arm, sess, 1)
            socks.append(await ex(_claim, d, "Ast", front, "s-flood"))
            socks.append(await ex(_claim, d, "Bst", front, "s-other"))
            for i in range(32):
                s = await ex(_client, d, f"A{i}", front)
                s.sendall(_q(_tok(i)))
                socks.append(s)
            for _ in range(100):
                if fake.peak >= 4:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)
            t0 = time.monotonic()
            b = await ex(_client, d, "B0", front)
            socks.append(b)
            b.sendall(_q(_tok(99)))
            got = await ex(_rest, b)
            took = time.monotonic() - t0
            return {"got": got, "took": took, "peak": fake.peak, "inflight": fake.inflight}
        finally:
            fake.release.set()
            for s in socks:
                s.close()
            a.close()
            await asyncio.wait_for(k.shutdown(), 10)
            srv.close()

    try:
        out = asyncio.run(run())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("R6", {k: v for k, v in out.items() if k != "got"})
    assert out["got"] == gwwire.encode(b"F", {"refused": "token-invalid"})
    assert out["took"] < 2.0                     # well inside RESOLVE_S: no wait behind the flooding session
    assert out["peak"] == K.RESOLVE_PER_SESSION  # the flooding session holds its own share, never more


def test_resolve_fenced_across_preemption_then_shutdown_order(monkeypatch):
    # R21 (design 1.5): a TOKEN-RESOLVE held at CONTROL across a preemption and then answered leaves the token cache
    # EMPTY of it (the old epoch's resolve is cancelled and its exchange detached, no answer); DROP-SESSION answers
    # `{dropped}` (CONTRACT 4.9); then shutdown's order with the credential seam: `begin_closing` at the latch,
    # `cancel_detached` before the wait, and an adopted admin connection aborted by step (2b), not held to the bound.
    _by_path(monkeypatch)
    d = tempfile.mkdtemp(prefix="lk", dir="/tmp")
    from levain.broker.norm import digest_of_token
    held = digest_of_token(_tok(7))
    fake = _FakeControl({held: {"ok": True, "session": "s1",
                                "claims": {"front": {"uid": 70000, "pid": 1, "start": 1}, "incarnation": 1}}},
                        {held})

    async def run() -> dict:
        loop = asyncio.get_running_loop()
        ex = lambda f, *a: loop.run_in_executor(None, f, *a)   # noqa: E731
        k, srv, front, admin = await _keys_with_control(d, fake)
        a1, a2 = C.KeysAdmin(admin, answer_s=2.0), C.KeysAdmin(admin, answer_s=2.0)
        socks = []
        try:
            await ex(a1.front_register, "s1", 70000, 1, 1, 1, 1)
            await ex(a1.front_arm, "s1", 1)
            socks.append(await ex(_claim, d, "Ast", front, "s1"))
            q = await ex(_client, d, "A1", front)
            socks.append(q)
            q.sendall(_q(_tok(7)))
            for _ in range(100):
                if fake.inflight:
                    break
                await asyncio.sleep(0.02)
            await ex(a2.front_register, "s2", 70001, 2, 2, 1, 1)    # preempts a1
            resolving_after_preempt = len(k.resolving)
            fake.release.set()
            q_got = await ex(_rest, q)
            await asyncio.sleep(0.2)
            cache = dict(k.cache)
            dropped = (await ex(lambda: a2.call("DROP-SESSION", connect=False, session="s2")),
                       await ex(lambda: a2.call("DROP-SESSION", connect=False, session="s9")))
            t0 = time.monotonic()
            await asyncio.wait_for(k.shutdown(), 10)
            took = time.monotonic() - t0
            return {"q_got": q_got, "cache": cache, "resolving": resolving_after_preempt, "dropped": dropped,
                    "calls": list(k.cred_lifecycle.calls), "took": took, "admin": k.admin}
        finally:
            fake.release.set()
            for s in socks:
                s.close()
            a1.close()
            a2.close()
            srv.close()

    try:
        out = asyncio.run(run())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("R21", out)
    assert out["q_got"] == b""            # detached: no answer, the connection closed
    assert out["cache"] == {}
    assert out["resolving"] == 0          # the old epoch's resolve task was cancelled in the preemption's phase 1
    assert [x.get("dropped") for x in out["dropped"]] == [True, False]
    assert out["calls"] == ["begin_closing", "cancel_detached"]
    assert out["took"] < K.SHUTDOWN_WAIT_S and out["admin"] is None


# --- P4b: THE FRAME, at every suspension (design 1.5 "test_revoke_at_every_suspension") ------------------------------

_SECRET = "sk-frame-test-SECRET"
_FRAME_TOKEN = "F" * 43
_FRAME_CLAIMS = {"routes": ["r"], "front": {"uid": 70000, "pid": 1, "start": 1}, "incarnation": 1}
_FRAME_Q = {"v": 1, "token": _FRAME_TOKEN, "route": "r", "method": "POST", "path": "/v1/messages", "query": "",
            "headers": [["content-type", "application/json"]], "body": "length:5", "upgrade": "none"}


def _frame_lib(d: str) -> None:
    """L: a base map with one brokered route `r` naming the static credential `c1`, and that credential."""
    os.makedirs(os.path.join(d, "routes"))
    os.makedirs(os.path.join(d, "keys", "cred"))
    route = {"route": "r", "type": "brokered",
             "upstream": {"scheme": "https", "host": "api.example.com", "base_path": ""}, "credential": "c1",
             "requests": {"templates": [{"method": "POST", "path": "/v1/messages"}],
                          "headers": {"content-type": "application/json"}, "body_cap": 1024, "body_codec": "none"},
             "max_call_microdollars": 1000, "max_session_microdollars": 100000}
    with open(os.path.join(d, "routes", "base.json"), "wb") as f:
        f.write(canon.canonical_bytes({"format": K.BASE_MAP_FORMAT, "routes": [route]}))
    with open(os.path.join(d, "keys", "cred", "c1.json"), "w") as f:
        f.write('{"format": "levain-keys-credential/1", "name": "c1", "rev": "%s", "hosts": ["api.example.com"], '
                '"inject": {"header": "x-api-key", "scheme": ""}, "kind": "static", "secret": "%s"}'
                % ("0" * 32, _SECRET))


class _Tr:
    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        pass


class _RawTLS:
    """The raw TLS writer under an exchange's `Upstream`: every `write` is recorded as before or after the detach."""

    def __init__(self, st: dict) -> None:
        self.st, self.transport = st, _Tr()

    def write(self, data: bytes) -> None:
        self.st["raw"].append((self.st["after"], bytes(data)))

    async def drain(self) -> None:
        await asyncio.sleep(0)   # flow control: one suspension between the head and the body

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


class _Plain:
    def __init__(self) -> None:
        self.transport = _Tr()


class _AdminW:
    def __init__(self) -> None:
        self.transport = _Tr()

    def close(self) -> None:
        pass


class _TaskStub:
    """The exchange's `task` as phase 1 sees it: `cancel()` is recorded, never delivered (the stepper delivers it)."""

    def __init__(self) -> None:
        self.cancels = 0

    def cancel(self) -> None:
        self.cancels += 1

    def done(self) -> bool:
        return True   # shutdown's wait never waits on the paused coroutine the stepper itself holds


class _CountingCache(dict):
    def __init__(self, st: dict) -> None:
        super().__init__()
        self.st = st

    def __setitem__(self, k, v) -> None:
        if self.st["after"]:
            self.st["inserts_after"] += 1
        super().__setitem__(k, v)


async def _step(coro, pause_at: int, at_pause) -> tuple:
    """Drive `coro` by `send`/`throw` alone. Every suspension (each value it yields) is TWO pause slots: with the
    awaited future still pending, then with it done and the coroutine not yet resumed. At slot `pause_at` the driver
    runs `at_pause()`; when it returns True a `CancelledError` is thrown in at that suspension. Returns (outcome,
    paused, slots)."""
    slot, exc, paused = 0, None, False
    while True:
        try:
            y = coro.throw(exc) if exc is not None else coro.send(None)
        except StopIteration:
            return "done", paused, slot
        except asyncio.CancelledError:
            return "cancelled", paused, slot
        exc = None
        for late in (False, True):
            slot += 1
            if late:
                if y is None:
                    await asyncio.sleep(0)
                else:
                    y._asyncio_future_blocking = False
                    await asyncio.wait({y})
            if slot == pause_at:
                paused = True
                if await at_pause():
                    exc = asyncio.CancelledError()
                    break


def _transition(name: str):
    async def run(k) -> None:
        if name == "drop":
            await k.drop_session(k.admin_epoch, "s1")
        elif name == "preempt":
            await k._adopt(None, _AdminW())
        elif name == "replace":
            await k.front_register(k.admin_epoch, "s1", 70000, 1, 1, 1, 2)
        elif name == "eof":
            await k._admin_eof(k.admin)
        else:
            await k.shutdown()
    return run


@pytest.mark.parametrize("cached", [True, False], ids=["cached", "uncached"])
@pytest.mark.parametrize("delivery", ["cancel-delivered", "cancel-lost"])
@pytest.mark.parametrize("transition", ["drop", "preempt", "replace", "eof", "shutdown"])
def test_revoke_at_every_suspension(monkeypatch, transition, delivery, cached):
    # Design 1.5, the ONE test of the frame (r7 R36, r8 R43, r9 R46-R48, r10 R50): at every suspension of the
    # exchange coroutine, enumerated by the runtime, a detaching transition's phase 1 + phase 2 run, then the
    # exchange resumes (cancel delivered by `throw`, or lost). ORACLE per run: zero raw TLS writes and zero token-cache
    # inserts after the detach, the shared detach ran and `exchange.detached`, the exchange ended, no transport left
    # open, the exchange out of its set. Reproduced first: P4a's mutation run found NO test that fails when only the
    # cache-insert fence is deleted (`3b-3b-p4/p4_mutations.txt`); this test's mutation run must.
    from levain.broker import upstream as U
    from levain.broker.creds import CredStore
    from levain.broker.norm import digest_of_token
    d = tempfile.mkdtemp(prefix="lf", dir="/tmp")
    _frame_lib(d)
    st: dict = {}
    orig_write, orig_detach = U.Upstream.write, K.Keys._detach

    def counting_write(self, data):
        try:
            return orig_write(self, data)
        except U.StaleExchange:
            st["refused"] += 1
            raise

    def spy_detach(self, reg, revoke=False):
        st["detached"].append(reg)
        return orig_detach(self, reg, revoke)

    async def handshake(addr, port, host):
        await asyncio.sleep(0)   # the connect, held open across one suspension
        raw, plain = _RawTLS(st), _Plain()
        st["transports"] += [raw.transport, plain.transport]
        return object(), raw, plain

    async def read_response(up):
        await asyncio.sleep(0)   # the response wait
        raise NotImplementedError

    monkeypatch.setattr(U.Upstream, "write", counting_write)
    monkeypatch.setattr(K.Keys, "_detach", spy_detach)
    monkeypatch.setattr(U, "_handshake", handshake)
    monkeypatch.setattr(U, "_read_response", read_response)

    async def one(pause_at: int) -> dict:
        st.clear()
        st.update(after=False, raw=[], inserts_after=0, refused=0, detached=[], transports=[])
        cred_fd = os.open(os.path.join(d, "keys", "cred"), os.O_RDONLY | os.O_DIRECTORY)
        a, b = socket.socketpair()
        try:
            k = K.Keys(unmetered=True, lib_root=d, creds=CredStore(cred_fd))
            k._in_loop()
            k.cache = _CountingCache(st)
            k.admin = K.AdminConn(None, _AdminW(), k.admin_epoch)
            k.admin.task = None
            await k.front_register(k.admin_epoch, "s1", 70000, 1, 1, 1, 1)
            await k.front_arm(k.admin_epoch, "s1", 1)
            reg = k.regs[70000]
            reg.state = "claimed"
            answer = {"session": "s1", "claims": dict(_FRAME_CLAIMS)}
            if cached:
                dict.__setitem__(k.cache, digest_of_token(_FRAME_TOKEN), answer)
            else:
                async def resolve(digest):
                    await asyncio.sleep(0)
                    return ("answer", answer)
                k._token_resolve = resolve
            _r, w = await asyncio.open_unix_connection(sock=a)
            fr = asyncio.StreamReader()
            fr.feed_data(gwwire.encode(b"B", b"hello") + gwwire.encode(b"E"))
            fr.feed_eof()
            side = gwwire.KeysSide()
            side.got(b"Q")
            conn = K.Conn(w, reg, "pending")
            conn.task = stub = _TaskStub()
            reg.pending.add(conn)
            k.pool.add(conn)

            async def at_pause() -> bool:
                before = stub.cancels
                await _transition(transition)(k)
                st["after"] = True
                st["ran_detach"] = any(r is reg for r in st["detached"])
                st["detached_flag"] = conn.detached
                return delivery == "cancel-delivered" and stub.cancels > before

            outcome, paused, slots = await _step(k._exchange(conn, gwwire.FrameReader(fr), side, dict(_FRAME_Q)),
                                                 pause_at, at_pause)
            return dict(st, outcome=outcome, paused=paused, slots=slots, in_set=conn in reg.exchanges,
                        held=conn.upstream)
        finally:
            a.close()
            b.close()
            os.close(cred_fd)

    async def all_runs() -> list:
        runs, n = [], 1
        while True:
            r = await one(n)
            runs.append(r)
            if not r["paused"] or n > 200:
                return runs
            n += 1

    try:
        runs = asyncio.run(all_runs())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    last, paused = runs[-1], runs[:-1]
    print(transition, delivery, "cached" if cached else "uncached", "slots", last["slots"],
          "refused", sum(r["refused"] for r in paused))
    assert not last["paused"] and last["outcome"] == "done"
    assert any(_SECRET.encode() in data for _a, data in last["raw"])   # unrevoked, the credential does go out
    assert len(paused) >= 8                                             # the enumeration is not vacuous
    for i, r in enumerate(paused, 1):
        where = f"slot {i}"
        assert r["ran_detach"] and r["detached_flag"], where
        assert [x for x in r["raw"] if x[0]] == [], where                # no raw TLS write after the detach
        assert r["inserts_after"] == 0, where                            # no token-cache insert after the detach
        assert r["outcome"] in ("done", "cancelled"), where
        assert all(t.aborted for t in r["transports"]), where            # no open transport left
        assert not r["in_set"] and r["held"] is None, where
    if cached and delivery == "cancel-lost":
        assert sum(r["refused"] for r in paused) >= 1                   # the gate itself refused, not only the cancel


# --- P5: the request path's upstream failures and the base map, L3 r2's findings ----------------------------------

class _Ledger:
    """A budget seam that counts: +1 per reservation, -1 per release."""

    def __init__(self) -> None:
        self.held = 0
        self.sent: list = []

    def reserve(self, session, route, max_call) -> None:
        self.held += 1

    def release(self, session, route, max_call, sent) -> None:
        self.held -= 1
        self.sent.append(sent)


async def _one_q(d: str, ledger: _Ledger) -> bytes:
    """One cached-token `Q` with a 5-byte body through the real `_exchange`; returns what the front read."""
    from levain.broker.creds import CredStore
    from levain.broker.norm import digest_of_token
    cred_fd = os.open(os.path.join(d, "keys", "cred"), os.O_RDONLY | os.O_DIRECTORY)
    a, b = socket.socketpair()
    try:
        k = K.Keys(unmetered=True, lib_root=d, creds=CredStore(cred_fd), budget=ledger)
        k._in_loop()
        k.admin = K.AdminConn(None, _AdminW(), k.admin_epoch)
        await k.front_register(k.admin_epoch, "s1", 70000, 1, 1, 1, 1)
        await k.front_arm(k.admin_epoch, "s1", 1)
        reg = k.regs[70000]
        reg.state = "claimed"
        k.cache[digest_of_token(_FRAME_TOKEN)] = {"session": "s1", "claims": dict(_FRAME_CLAIMS)}
        _r, w = await asyncio.open_unix_connection(sock=a)
        fr = asyncio.StreamReader()
        fr.feed_data(gwwire.encode(b"B", b"hello") + gwwire.encode(b"E"))
        fr.feed_eof()
        side = gwwire.KeysSide()
        side.got(b"Q")
        conn = K.Conn(w, reg, "pending")
        reg.pending.add(conn)
        k.pool.add(conn)
        await k._exchange(conn, gwwire.FrameReader(fr), side, dict(_FRAME_Q))
        w.close()
        await asyncio.wait_for(w.wait_closed(), 2)
        b.settimeout(2)
        got = b""
        while True:
            x = b.recv(65536)
            if not x:
                return got
            got += x
    finally:
        a.close()
        b.close()
        os.close(cred_fd)


@pytest.mark.parametrize("fault", ["unbuilt", "reset", "tls", "timeout", "head-write", "head-stale", "head-drain",
                                   "body-drain"])
def test_upstream_failure_answers_f_and_releases_the_budget(monkeypatch, fault):
    # L3 r2 (codex MED, complement LOW x2), reproduced first (`3b-3b-p5/repro_before.txt`): a failure in
    # `open_exchange` (the shipped seam's NotImplementedError included), the head's write or a drain escaped every
    # translation, so the front read EOF; and the reservation made before `open_exchange` was never released.
    # Now each is answered `upstream-error` (`upstream-timeout` for a deadline) and every reservation released once.
    # P5 L3 r1 (codex MED, complement LOW), reproduced first (`3b-3b-p5/repro_p5b_head_write_before.txt`,
    # `p5b_mutation_sent.txt`): a head write that raised before taking a byte, or that the gate refused, was released
    # `sent=True`; `sent` now follows the write's return. A refused (detached) exchange is answered nothing.
    import ssl

    from levain.broker import upstream as U
    d = tempfile.mkdtemp(prefix="lu", dir="/tmp")
    _frame_lib(d)
    st = {"after": False, "raw": []}

    class _Failing(_RawTLS):
        drains = 0

        def write(self, data: bytes) -> None:
            if fault == "head-write":
                raise ConnectionResetError("upstream reset before the head")
            super().write(data)

        async def drain(self) -> None:
            self.drains += 1
            if self.drains >= (1 if fault == "head-drain" else 2):
                raise ConnectionResetError("upstream reset")

    raises = {"reset": ConnectionResetError("rst"), "tls": ssl.SSLError("bad cert"), "timeout": asyncio.TimeoutError()}

    async def handshake(addr, port, host):
        if fault in raises:
            raise raises[fault]
        return object(), _Failing(st), _Plain()

    if fault != "unbuilt":
        monkeypatch.setattr(U, "_handshake", handshake)
    if fault == "head-stale":
        opened = K.open_exchange

        async def then_detached(addr, port, host, exchange):
            up = await opened(addr, port, host, exchange)
            exchange.detached = True   # a phase 1 landing between the store and the head's write
            return up

        monkeypatch.setattr(K, "open_exchange", then_detached)
    ledger = _Ledger()
    try:
        got = asyncio.run(_one_q(d, ledger))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    name = "upstream-timeout" if fault == "timeout" else "upstream-error"
    want = (gwwire.encode(b"A") if fault == "body-drain" else b"") + gwwire.encode(b"F", {"refused": name})
    assert got == (b"" if fault == "head-stale" else want)
    assert st["raw"] == [] or fault not in ("head-write", "head-stale")   # the gate refused: nothing written
    assert ledger.held == 0 and len(ledger.sent) == 1
    assert ledger.sent == [fault in ("head-drain", "body-drain")]   # `sent` once the head was handed upstream


@pytest.mark.parametrize("routes", ["non-object", "duplicate", "malformed", "overflow"])
def test_base_map_unusable_anywhere_refuses_every_route(routes):
    # L3 r2 (codex LOW) against ruling S-24's fail-closed rule, reproduced first (`3b-3b-p5/repro_before.txt`): the
    # map was filtered to the asked route before validation, so an unusable or duplicated UNRELATED entry left `r`
    # served. Now every entry is parsed and every id checked once across the map: `keys-unavailable`, nothing
    # reserved.
    d = tempfile.mkdtemp(prefix="lm", dir="/tmp")
    _frame_lib(d)
    path = os.path.join(d, "routes", "base.json")
    with open(path, "rb") as f:
        route = canon.parse(f.read())["routes"][0]
    extra = {"non-object": [42], "duplicate": [dict(route, route="z"), dict(route, route="z")],
             "malformed": [{"route": "z", "type": "brokered"}],
             # P5 L3 r1 (complement MED), reproduced first (`3b-3b-p5/p5b_parse_route_escapes_r2.txt`): a pattern
             # whose `re.compile` raises OverflowError escaped the refusal, so the front read EOF
             "overflow": [dict(route, route="z", requests=dict(route["requests"],
                                                                headers={"content-type": "a{99999999999}"}))]}[routes]
    with open(path, "wb") as f:
        f.write(canon.canonical_bytes({"format": K.BASE_MAP_FORMAT, "routes": [route] + extra}))
    ledger = _Ledger()
    try:
        got = asyncio.run(_one_q(d, ledger))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert got == gwwire.encode(b"F", {"refused": "keys-unavailable"})
    assert ledger.sent == []


def test_preemption_fences_old_exchanges_before_its_bounded_await(monkeypatch):
    # group L3 r1 (codex HIGH 3), reproduced first (`1022-8/g1fix/item2_repro_before.txt`): preemption bumped the
    # epoch, then awaited the old connection's request tasks (bounded) BEFORE its fail-closed clear detached anything,
    # and `Upstream.write` gates on `detached` alone, so a `B` frame read in that window still reached the upstream,
    # and a `Q` arriving in it was admitted on the old registration. Now the bump fences the old epoch's exchanges and
    # admission refuses a registration made under an older epoch.
    from levain.broker import upstream as U
    from levain.broker.creds import CredStore
    from levain.broker.norm import digest_of_token
    d = tempfile.mkdtemp(prefix="lp", dir="/tmp")
    _frame_lib(d)
    st = {"after": False, "raw": []}

    async def handshake(addr, port, host):
        return object(), _RawTLS(st), _Plain()

    async def run() -> dict:
        cred_fd = os.open(os.path.join(d, "keys", "cred"), os.O_RDONLY | os.O_DIRECTORY)
        pairs = [socket.socketpair() for _ in range(2)]
        gate = asyncio.Event()

        async def resisting() -> None:   # an old request task that outlives its cancel, as step (2) allows
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    pass

        try:
            k = K.Keys(unmetered=True, lib_root=d, creds=CredStore(cred_fd))
            k._in_loop()
            k.admin = K.AdminConn(None, _AdminW(), k.admin_epoch)
            k.admin.task = None
            await k.front_register(k.admin_epoch, "s1", 70000, 1, 1, 1, 1)
            await k.front_arm(k.admin_epoch, "s1", 1)
            reg = k.regs[70000]
            reg.state = "claimed"
            k.cache[digest_of_token(_FRAME_TOKEN)] = {"session": "s1", "claims": dict(_FRAME_CLAIMS)}
            old_task = asyncio.ensure_future(resisting())
            k.admin.tasks.add(old_task)

            async def exchange(sock, feed_now: bool):
                _r, w = await asyncio.open_unix_connection(sock=sock)
                fr = asyncio.StreamReader()
                if feed_now:
                    fr.feed_data(gwwire.encode(b"B", b"hello") + gwwire.encode(b"E"))
                    fr.feed_eof()
                side = gwwire.KeysSide()
                side.got(b"Q")
                conn = K.Conn(w, reg, "pending")
                reg.pending.add(conn)
                k.pool.add(conn)
                task = asyncio.ensure_future(k._exchange(conn, gwwire.FrameReader(fr), side, dict(_FRAME_Q)))
                return conn, fr, w, task

            conn1, fr1, w1, t1 = await exchange(pairs[0][0], False)
            for _ in range(50):
                await asyncio.sleep(0)
            head_written = len(st["raw"])
            epoch0 = k.admin_epoch
            pre = asyncio.ensure_future(k._adopt(None, _AdminW()))
            while k.admin_epoch == epoch0:
                await asyncio.sleep(0)
            st["after"] = True                     # the epoch is bumped: preemption is in its bounded await
            fr1.feed_data(gwwire.encode(b"B", b"hello") + gwwire.encode(b"E"))
            fr1.feed_eof()
            conn2, _fr2, w2, t2 = await exchange(pairs[1][0], True)   # a `Q` arriving inside the window
            await asyncio.sleep(0.2)
            in_window = {"pre_done": pre.done(), "raw_after": [x for x in st["raw"] if x[0]],
                         "q2_admitted": conn2.kind == "exchange"}
            gate.set()
            await pre
            for t in (t1, t2):
                await asyncio.wait_for(t, 5)
            for w in (w1, w2):
                w.close()
            return dict(in_window, head_written=head_written)
        finally:
            for a, b in pairs:
                a.close()
                b.close()
            os.close(cred_fd)

    monkeypatch.setattr(U, "_handshake", handshake)
    try:
        out = asyncio.run(run())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("preempt-window", out)
    assert out["head_written"] == 1                 # the exchange was live before the preemption
    assert out["pre_done"] is False                 # the window is real: the clear has not run yet
    assert out["raw_after"] == []                   # nothing reached the upstream after the bump
    assert out["q2_admitted"] is False              # and no new exchange was admitted on the old registration


def test_base_map_parsed_once_per_file_and_refused_when_it_changes_badly(monkeypatch):
    # group L3 r1 (complement MED 2), measured first (`1022-8/g1fix/item5_measure.txt`: ~38 ms per `Q` on keys' one
    # loop for a map near the cap) and reproduced (`item5_repro_before.txt`: every `Q` re-parsed the map). Now the
    # parse is reused while the opened file's (dev, ino, mtime_ns, size) is unchanged; a replaced map is parsed
    # again, and one that does not parse is still refused, whatever was cached.
    from levain.broker import norm
    d = tempfile.mkdtemp(prefix="lc", dir="/tmp")
    _frame_lib(d)
    parses: list = []
    real = K.parse_route

    def counting(entry, source):
        parses.append(entry.get("route"))
        return real(entry, source=source)

    monkeypatch.setattr(K, "parse_route", counting)
    path = os.path.join(d, "routes", "base.json")
    try:
        k = K.Keys(unmetered=True, lib_root=d)
        assert k._base_route("r").route == "r" and k._base_route("r").route == "r"
        assert parses == ["r"]                                   # one parse for two `Q`s
        with open(path, "rb") as f:
            doc = canon.parse(f.read())
        good = dict(doc, routes=doc["routes"] + [dict(doc["routes"][0], route="z")])
        tmp = path + ".new"
        with open(tmp, "wb") as f:
            f.write(canon.canonical_bytes(good))
        os.replace(tmp, path)
        assert k._base_route("z").route == "z" and parses == ["r", "r", "z"]   # a replaced map is read again
        with open(tmp, "wb") as f:
            f.write(canon.canonical_bytes(dict(doc, routes=doc["routes"] + [{"route": "y", "type": "brokered"}])))
        os.replace(tmp, path)
        for _ in range(2):
            with pytest.raises(norm.Refused) as e:
                k._base_route("r")
            assert e.value.name == "keys-unavailable"            # never the cached parse of the earlier map
    finally:
        shutil.rmtree(d, ignore_errors=True)
