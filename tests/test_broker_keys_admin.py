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
            # a peer that is not `_levain_control` is closed unread
            k.control_uid = os.getuid() + 1
            r, w = await asyncio.open_unix_connection(admin)
            await _send(w, 1, "DROP-SESSION", session="s-x")
            out["foreign"] = await asyncio.wait_for(r.read(), 5)
            w.close()
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
