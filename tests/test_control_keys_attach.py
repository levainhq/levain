"""S1 3c-3: keys' attachment to CONTROL (CONTRACT §4.5, §4.9; 3c design r3 §2.1 (e) and §2.4's rows; R4 §0, §1.4, §2,
§10): `KEYS-ATTACH` (with the generation keys serves), `FRONT-LOST`, the unclaimed registration's V23 clock,
`SPEND-OVERRUN` (recorded, never a revoke), the `SESSIONS` reconcile, both EOF rules, and `control.serve`.

The process tests run CONTROL through `control.serve` on a thread and keys as a child process (`tests/c3_rig.py`).
Undischarged here: two real uids (`_levain_keys`, `_levain_control`) and the real unit's restart; the VM run is 3c-10's.
keys refuses uid 0, so the process tests skip as root."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from levain.broker import keys as K
from levain.slot import control as C
from levain.slot import control_client
from levain.slot import records as R
from tests import b2_keys_rig as B2
from tests import c3_rig as R3
from tests.test_broker_keys_process import EST
from tests.test_control_keys_admin import FakeKeys
from tests.test_slot_s2a_control import OP_UID, attach_claims, mint_claims

pytestmark = [pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets")]
process = pytest.mark.skipif(os.geteuid() == 0, reason="keys refuses to run as root (it runs as _levain_keys)")

SYS_PY = "/usr/bin/python3"
PYS = [sys.executable] + ([SYS_PY] if os.path.exists(SYS_PY) and sys.executable != SYS_PY else [])
S1, S2, S3 = "s-00000000000c3a01", "s-00000000000c3a02", "s-00000000000c3a03"
CAP = 100 * EST
#: usage far above the rig body's hold: one call overruns
OVER = B2.usage_response({"input_tokens": 1, "output_tokens": 50000})


@pytest.fixture(params=PYS, ids=lambda p: "usr-bin-python3" if p == SYS_PY else "venv")
def py(request):
    B2.require_h11(request.param)
    return request.param


def run(coro, timeout: float = 90.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


async def in_thread(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)


async def no_k(front: R3.Front, s: float = 0.5) -> bool:
    """Nothing arrives on the STATUS connection within `s` (keys sent no `K`, and did not close it)."""
    try:
        await front.status_k(timeout=s)
    except asyncio.TimeoutError:
        return True
    return False


def root(control: C.Control, op: str, **args) -> dict:
    return control.handle(C.Conn(0), {"v": 1, "op": op, "id": 90, **args})


def attach_only(control: C.Control, session: str) -> C.Conn:
    """An egress session ATTACHed (ALLOCATED, no MINT: nothing registered with keys); returns its operator lease."""
    op = C.Conn(OP_UID)
    nonce = control.handle(op, {"v": 1, "op": "LEASE-OPEN", "id": 1})["lease_nonce"]
    assert root(control, "ATTACH", lease_nonce=nonce, sudo_uid=OP_UID, session=session,
                claims=attach_claims())["ok"]
    return op


def test_keys_v23_is_the_contract_value():
    # keys restates V23 (it imports no levain.slot module): the two must be one value
    assert K.V23_S == control_client.V23_SECONDS
    assert K.RECONCILE_MISSES == 3 and K.RECONCILE_S == 60.0   # R4 §0; r3 §2.1 (e)
    assert K.SESSIONS_CHUNK <= C.SESSIONS_DIGESTS_MAX


# ---- the fail-closed rules ------------------------------------------------------------------------------------------

@process
def test_keys_fail_closed_on_control_eof(py):
    # §4.5: keys' KEYS-ATTACH EOF = keys refuses every token, sends `K` to every front AND closes the other connection
    # (the admin wire). Case 1: the attachment alone ends (CONTROL's side of it closed). Case 2: CONTROL dies (stopped
    # as a crash looks: both its connections end). Each time the front reads `K` and the next `Q` is closed with no
    # frame (the real front answers that 503 keys-unavailable); keys attaches again on its own.
    async def go() -> dict:
        out = {}
        async with R3.World(py, CAP) as w:
            s1, f1 = await w.session(S1)
            out["q1"] = await f1.q()
            admin = w.control.keys_admin
            assert admin is not None and admin.connected()
            att = w.control._attach
            att.sock.shutdown(socket.SHUT_RDWR)                       # case 1: only the attachment ends
            out["k1"] = await f1.status_k()
            out["q1_after"] = await f1.q()
            await R3.aio_until(lambda: any("detached n=1" in x for x in w.keys.err), what="keys detached")
            await R3.aio_until(lambda: not admin.connected(), what="keys closed the admin connection")
            await R3.aio_until(lambda: w.control.keys_attached() == 2, what="keys attached again")
            await R3.aio_until(lambda: s1.state() == R.REVOKED, what="CONTROL's side of the same EOF")
            out["s1_state"] = s1.state()
            s2, f2 = await w.session(S2)
            out["q2"] = await f2.q()
            ups = w.up.conns
            await in_thread(w.ctl.stop)                               # case 2: CONTROL dies
            out["k2"] = await f2.status_k()
            out["q2_after"] = await f2.q()
            await R3.aio_until(lambda: any("detached n=2" in x for x in w.keys.err), what="keys detached again")
            out["up_after"] = w.up.conns - ups
        return out

    out = run(go())
    assert out["q1"].get("status") == 200 and out["q2"].get("status") == 200, out
    assert out["k1"] == b"K" and out["k2"] == b"K", out
    assert out["q1_after"]["frames"] == [] and out["q2_after"]["frames"] == [], out
    assert out["s1_state"] == R.REVOKED, out
    assert out["up_after"] == 0, out


@process
def test_keys_restart_revokes_sessions(py):
    # §4.5 (d): keys restarting = every session revoked. SIGKILL keys: CONTROL reads the attachment's EOF and REVOKES
    # every egress-class session, the registered one (s1, also its admin EOF's victim) AND one not yet registered with
    # keys (s2, ALLOCATED). s1's token then resolves `token-revoked`, and after keys restarts and attaches again the old
    # token's front is unknown to it.
    async def go() -> dict:
        async with R3.World(py, CAP) as w:
            s1, f1 = await w.session(S1)
            await in_thread(attach_only, w.control, S2)
            q1 = await f1.q()
            digest = C.digest_of(s1.token)
            w.keys.kill()
            await R3.aio_until(lambda: s1.state() == R.REVOKED, what="s1 revoked")
            await R3.aio_until(lambda: w.control.store.get(S2).state == R.REVOKED, what="s2 revoked")
            resolved = w.control.handle(C.Conn(R3.KEYS_UID), {"v": 1, "op": "TOKEN-RESOLVE", "id": 3,
                                                               "token_digest": digest})
            await w.keys.start()
            await R3.aio_until(lambda: w.control.keys_attached() == 2, what="keys attached after its restart")
            q_after = await f1.q()
            return {"q1": q1, "resolved": resolved, "q_after": q_after}

    out = run(go())
    assert out["q1"].get("status") == 200, out
    assert out["resolved"] == {"id": 3, "ok": False, "refused": "token-revoked"}, out
    assert out["q_after"]["frames"] == [], out


@process
def test_front_lost_revokes_session(py):
    # §4.5: a front that dies on its own (keys sees its STATUS connection EOF without having sent `K`) = FRONT-LOST,
    # and CONTROL REVOKES that session. Not after a DROP-SESSION for it: s2 revoked by CONTROL gets keys' `K`, and keys
    # sends no FRONT-LOST for it.
    async def go() -> dict:
        async with R3.World(py, CAP) as w:
            s1, f1 = await w.session(S1)
            q1 = await f1.q()
            await f1.drop_status()
            await R3.aio_until(lambda: s1.state() == R.REVOKED, what="s1 revoked on FRONT-LOST")
            q1_after = await f1.q()
            s2, f2 = await w.session(S2)
            ans =await in_thread(lambda: root(w.control, "REVOKE", session=S2))
            k2 = await f2.status_k()
            await asyncio.sleep(0.5)                       # a FRONT-LOST for s2, wrongly sent, would be in by now
            return {"q1": q1, "q1_after": q1_after, "revoke": ans, "k2": k2, "lost": list(w.control.front_lost),
                    "err": w.keys.err}

    out = run(go())
    assert out["q1"].get("status") == 200, out
    assert out["q1_after"]["frames"] == [], out
    assert out["revoke"]["ok"] and out["k2"] == b"K", out
    assert out["lost"] == [S1], out


@process
def test_unclaimed_registration_revoked_after_v23(py):
    # §4.5: a registration unclaimed (no `H`) for V23 after keys acks FRONT-ARM is a lost front: keys detaches it and
    # sends FRONT-LOST, CONTROL revokes. V23 is scaled to 1 s (the driver's $C3_UNCLAIMED_S). s2, claimed in time,
    # stays live past its own V23 and is served.
    v23 = 1.0

    async def go() -> dict:
        async with R3.World(py, CAP, env={"C3_UNCLAIMED_S": str(v23)}) as w:
            t0 = time.monotonic()
            s1 = await in_thread(lambda: R3.Session(w.control, S1).arm())   # armed, never claimed
            await R3.aio_until(lambda: s1.state() == R.REVOKED, timeout=10, what="s1 revoked unclaimed")
            took = time.monotonic() - t0
            s2, f2 = await w.session(S2)
            await asyncio.sleep(v23 + 0.5)
            return {"took": took, "s2_state": s2.state(), "q2": await f2.q(), "lost": list(w.control.front_lost)}

    out = run(go())
    assert out["took"] >= v23, out                          # never before the clock ran out
    assert out["lost"] == [S1], out
    assert out["s2_state"] == R.RENDERED and out["q2"].get("status") == 200, out


# ---- the reconcile ---------------------------------------------------------------------------------------------------

@process
def test_reconcile_drops_unrevoked_session(py):
    # 3c design r3 §2.1 (e): a session REVOKED at CONTROL whose DROP-SESSION never reaches keys (suppressed here) keeps
    # its token cached at keys, and is served, until the next SESSIONS tick: CONTROL answers its digest in `drop`, keys
    # treats it exactly as a DROP-SESSION (the front gets `K`), and the next `Q` never reaches upstream. The tick is
    # scaled to 0.5 s ($C3_RECONCILE_S). Built as DROP-SESSION, so the front is told `K` and the `Q` is closed with no
    # frame where r3's row read 401 (RETURN question 1).
    tick = 0.5

    async def go() -> dict:
        async with R3.World(py, CAP, env={"C3_RECONCILE_S": str(tick)}) as w:
            s1, f1 = await w.session(S1)
            q1 = await f1.q()                                       # the token is cached at keys now
            w.control._drop_session = lambda session: None          # the DROP-SESSION never reaches keys
            ans = await in_thread(lambda: root(w.control, "REVOKE", session=S1))
            k = await f1.status_k(timeout=5 * tick + 2)
            ups = w.up.conns
            q_after = await f1.q()
            return {"q1": q1, "revoke": ans, "k": k, "q_after": q_after, "up": w.up.conns - ups,
                    "state": s1.state()}

    out = run(go())
    assert out["q1"].get("status") == 200 and out["revoke"]["ok"] and out["state"] == R.REVOKED, out
    assert out["k"] == b"K", out
    assert out["q_after"]["frames"] == [] and out["up"] == 0, out


class AttachServer:
    """CONTROL's socket for the in-process attachment cases: answers KEYS-ATTACH `ok`; by default never answers
    SESSIONS or SPEND-OVERRUN. Per connection it records when it was accepted (the loop's clock), the SESSIONS it read,
    each SPEND-OVERRUN's arguments, and how many SESSIONS it had read at its EOF. The modes, settable between
    connections: `close_after_attach` (close right after the ok), `answer_sessions` (`ok` with an empty `drop`, then
    close 0.1 s later), `overrun_ignore` (that many SPEND-OVERRUN sends go unanswered, then each is acked; -1: none
    is), `overrun_refuse` (answered `ok: false`)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.conns: list = []      # per connection: {"at", "sessions", "overruns", "eof_after"}
        self.close_after_attach = False
        self.answer_sessions = False
        self.overrun_ignore = -1
        self.overrun_refuse = False

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._on, self.path)

    async def _on(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        me = {"at": asyncio.get_running_loop().time(), "sessions": 0, "overruns": [], "eof_after": None}
        self.conns.append(me)
        try:
            while True:
                n = int.from_bytes(await r.readexactly(4), "big")
                req = json.loads(await r.readexactly(n))
                if req["op"] == "KEYS-ATTACH":
                    w.write(K.encode_admin({"id": req["id"], "ok": True, "attach": len(self.conns)}))
                    await w.drain()
                    if self.close_after_attach:
                        break
                elif req["op"] == "SESSIONS":
                    me["sessions"] += 1
                    if self.answer_sessions:
                        w.write(K.encode_admin({"id": req["id"], "ok": True, "drop": []}))
                        await w.drain()
                        await asyncio.sleep(0.1)
                        break
                elif req["op"] == "SPEND-OVERRUN":
                    me["overruns"].append({k: v for k, v in req.items() if k != "id"})
                    if self.overrun_refuse:
                        w.write(K.encode_admin({"id": req["id"], "ok": False, "refused": "bad-arguments"}))
                        await w.drain()
                    elif 0 <= self.overrun_ignore < len(me["overruns"]):
                        w.write(K.encode_admin({"id": req["id"], "ok": True}))
                        await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            me["eof_after"] = me["sessions"]
            w.close()

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()


def test_reconcile_misses_end_the_attachment_on_the_third():
    # R4 §0: a SESSIONS tick falls under the attachment EOF rule only after 3 CONSECUTIVE unanswered ticks: with two
    # unanswered the attachment stands; the third ends it (keys closes it and fails closed), and keys attaches again.
    async def go() -> dict:
        d = tempfile.mkdtemp(prefix="c3m", dir="/tmp")
        try:
            srv = AttachServer(os.path.join(d, "c.sock"))
            await srv.start()
            k = K.Keys(control_path=srv.path, lib_root=d, attach=True)
            k.reconcile_s, k.attach_answer_s = 0.3, 0.15
            k.start_attach()
            await R3.aio_until(lambda: k.attached is not None, what="attached")
            first = k.attached
            await R3.aio_until(lambda: srv.conns[0]["sessions"] >= 2, what="two ticks")
            await asyncio.sleep(0.2)                         # the second tick's answer window has passed: 2 misses
            standing_after_two = k.attached is first and not first.ended
            await R3.aio_until(lambda: srv.conns[0]["eof_after"] is not None, what="the attachment ended")
            await R3.aio_until(lambda: len(srv.conns) >= 2 and k.attached is not None and k.attached is not first,
                               what="attached again")
            out = {"standing_after_two": standing_after_two, "eof_after": srv.conns[0]["eof_after"]}
            await k.shutdown()
            await srv.close()
            return out
        finally:
            shutil.rmtree(d, ignore_errors=True)

    out = run(go(), 30)
    assert out["standing_after_two"] is True, out
    assert out["eof_after"] == 3, out


async def _attached_keys(d: str, srv: AttachServer) -> K.Keys:
    """In-process keys attached to `srv` (its clocks short, the reconcile out of the way unless a test sets it)."""
    k = K.Keys(control_path=srv.path, lib_root=d, attach=True)
    k.reconcile_s, k.attach_answer_s = 30.0, 0.5
    k.start_attach()
    await R3.aio_until(lambda: k.attached is not None, what="attached")
    return k


def _with_keys(body, timeout: float = 30):
    """Run `body(k, srv)` against an AttachServer and an attached in-process keys, then shut both down."""
    async def go():
        d = tempfile.mkdtemp(prefix="c3x", dir="/tmp")
        try:
            srv = AttachServer(os.path.join(d, "c.sock"))
            await srv.start()
            k = await _attached_keys(d, srv)
            try:
                return await body(k, srv)
            finally:
                await k.shutdown()
                await srv.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)
    return run(go(), timeout)


def test_attach_serve_exception_is_the_attachments_eof(monkeypatch):
    # L3 r1 (complement MED 1): an exception `_attach_serve` does not list (a RecursionError out of a deep frame's parse,
    # say) is that attachment's EOF: the fail-closed clear runs, and keys attaches again; the attach client never dies.
    async def body(k, srv):
        first = k.attached
        real = k._attach_serve
        calls = []

        async def boom(att):
            calls.append(att.no)
            if len(calls) == 1:
                raise RuntimeError("an exception the read loop does not list")
            return await real(att)

        k._attach_serve = boom
        k._attach_close(first)   # the first attachment's serve already runs: the next one is the one that raises
        await R3.aio_until(lambda: len(calls) >= 2, timeout=10, what="a third attachment after the raise")
        return {"first_ended": first.ended, "calls": calls, "task_alive": not k._attach_task.done(),
                "conns": len(srv.conns)}

    out = _with_keys(body)
    assert out["first_ended"] and out["task_alive"] and out["conns"] >= 3, out


def test_reconcile_exception_ends_the_attachment(monkeypatch):
    # L3 r1 (complement MED 1): the reconcile ticker raising ends the attachment (fail closed), never leaves it up with
    # the reconcile dead; keys then attaches again.
    real = K.Keys._reconcile_tick
    raised = []

    async def boom(self, att):
        if not raised:           # the first attachment's tick at its open
            raised.append(att.no)
            raise RuntimeError("the reconcile tick failed")
        return await real(self, att)

    monkeypatch.setattr(K.Keys, "_reconcile_tick", boom)

    async def body(k, srv):
        await R3.aio_until(lambda: k.attached is not None and k.attached.no >= 2, timeout=10, what="attached again")
        return {"raised": list(raised), "eof": srv.conns[0]["eof_after"] is not None}

    out = _with_keys(body)
    assert out["raised"] == [1] and out["eof"], out


def test_short_lived_attachments_back_off(monkeypatch):
    # L3 r1 (codex MED 4, complement LOW 2): a CONTROL that answers KEYS-ATTACH ok and closes at once sees GROWING gaps
    # between keys' connects (the backoff applies after a short-lived attachment, not only after a failed open); one
    # healthy attachment (a SESSIONS answered) resets it, so the connect after that one comes at once.
    monkeypatch.setattr(K, "ATTACH_RETRY_FIRST_S", 0.05)
    monkeypatch.setattr(K, "ATTACH_RETRY_MAX_S", 0.8)

    async def body(k, srv):
        srv.close_after_attach = True
        k._attach_close(k.attached)
        await R3.aio_until(lambda: len(srv.conns) >= 7, timeout=15, what="six short-lived attachments")
        at = [c["at"] for c in srv.conns[1:7]]
        gaps = [b - a for a, b in zip(at, at[1:])]
        srv.close_after_attach, srv.answer_sessions = False, True
        n = len(srv.conns)
        await R3.aio_until(lambda: len(srv.conns) >= n + 2, timeout=15, what="a healthy attachment, then the next")
        reset_gap = srv.conns[n + 1]["at"] - srv.conns[n]["at"]
        return {"gaps": gaps, "reset_gap": reset_gap}

    out = _with_keys(body)
    gaps = out["gaps"]
    assert all(g >= 0.04 for g in gaps), out
    assert all(b > a * 1.5 for a, b in zip(gaps, gaps[1:4])), out   # doubling from 0.05 s
    # the healthy attachment lasted ~0.1 s past its SESSIONS answer; the connect after it does not wait the backoff
    assert out["reset_gap"] < 0.4, out


OVERRUN = {"session": S1, "route": "r", "model": "m1", "ref": ["a" * 32, "b" * 32, 7], "overrun": 5,
           "unproven": False}


def test_spend_overrun_retried_until_acked():
    # L3 r1 (codex MED 1): an unacked SPEND-OVERRUN is kept and sent again, the same payload (CONTROL records it once
    # per (session, ref)), until acked; the attachment stays up.
    async def body(k, srv):
        srv.overrun_ignore = 2
        k.attach_attempt_s, k.attach_answer_s = 0.1, 2.0
        first = k.attached
        await k._spend_overrun(dict(OVERRUN))
        return {"overruns": srv.conns[0]["overruns"], "standing": k.attached is first and not first.ended}

    out = _with_keys(body)
    want = {"v": 1, "op": "SPEND-OVERRUN", **OVERRUN}
    assert out["overruns"] == [want] * 3 and out["standing"], out


@pytest.mark.parametrize("refuse", [False, True], ids=["never-acked", "refused"])
def test_spend_overrun_unacked_ends_the_attachment(refuse):
    # L3 r1 (codex MED 1): a SPEND-OVERRUN never acked within V23 (scaled to 0.6 s), or refused, ends the attachment
    # (fail closed: CONTROL's EOF rule revokes every egress session), never dropped silently; keys attaches again.
    async def body(k, srv):
        srv.overrun_refuse = refuse
        k.attach_attempt_s, k.attach_answer_s = 0.1, 0.6
        first = k.attached
        await k._spend_overrun(dict(OVERRUN))
        await R3.aio_until(lambda: first.ended, timeout=5, what="the attachment ended")
        await R3.aio_until(lambda: k.attached is not None and k.attached is not first, timeout=10,
                           what="attached again")
        return {"sent": len(srv.conns[0]["overruns"]), "eof": srv.conns[0]["eof_after"] is not None}

    out = _with_keys(body)
    assert out["eof"], out
    assert out["sent"] == 1 if refuse else out["sent"] >= 3, out


async def _admin_pair():
    a, b = socket.socketpair()
    r, w = await asyncio.open_unix_connection(sock=a)
    return r, w, b


def test_adopt_installs_nothing_after_an_attachment_loss():
    # L3 r1 (codex MED 3): a new admin connection's adoption that yields in its preemption wait while the attachment
    # ends (`_attach_lost` bumps the admin epoch) does not install a connection stamped with the epoch it captured
    # before: every request on that one would be stale and unanswered until CONTROL's V23. It installs nothing.
    async def body(k, srv):
        r, w, _b = await _admin_pair()
        old = K.AdminConn(r, w, k.admin_epoch)
        old.task = None
        release = asyncio.Event()

        async def stall():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await release.wait()   # the preemption's bounded wait yields here
                raise

        t = asyncio.ensure_future(stall())
        old.tasks.add(t)
        k.admin = old
        r2, w2, _b2 = await _admin_pair()
        adopt = asyncio.ensure_future(k._adopt(r2, w2))
        await R3.aio_until(lambda: old.preempted, what="the preemption began")
        first = k.attached
        k._attach_close(first)
        await R3.aio_until(lambda: first.ended, what="the attachment ended")
        release.set()
        conn = await asyncio.wait_for(adopt, 5)
        return {"conn": conn, "admin": k.admin, "epoch": k.admin_epoch}

    out = _with_keys(body)
    assert out["conn"] is None and out["admin"] is None, (out, getattr(out["conn"], "epoch", None))


def test_attach_lost_cancels_the_admin_connections_tasks():
    # L3 r1 (complement LOW 3): the attachment's EOF lets go of the admin connection AND cancels its request tasks, as
    # the admin EOF does (`_admin_eof` returns early for a connection no longer adopted, so nothing else would).
    async def body(k, srv):
        r, w, _b = await _admin_pair()
        adm = K.AdminConn(r, w, k.admin_epoch)
        adm.task = None
        t = asyncio.ensure_future(asyncio.sleep(30))
        adm.tasks.add(t)
        k.admin = adm
        first = k.attached
        k._attach_close(first)
        await R3.aio_until(lambda: first.ended, what="the attachment ended")
        await asyncio.wait({t}, timeout=0.5)
        cancelled = t.cancelled()
        t.cancel()
        return {"cancelled": cancelled, "admin": k.admin}

    out = _with_keys(body)
    assert out["cancelled"] and out["admin"] is None, out


def test_keys_unattached_refuses_registration():
    # §4.5 fail closed: with no attachment keys refuses every token, so it takes no registration a later EOF could not
    # clear (FRONT-REGISTER `keys-unavailable`, which CONTROL's MINT answers as keys-unavailable).
    async def go() -> dict:
        k = K.Keys(attach=True)
        k._in_loop()
        return await k.front_register(0, S1, 5000, 1, 1, 1, 1)

    assert run(go(), 10) == {"refused": "keys-unavailable"}


# ---- SPEND-OVERRUN ---------------------------------------------------------------------------------------------------

@process
def test_spend_overrun_recorded_session_not_revoked(py):
    # R4 §1.4 step 2: an overrun's SPEND-OVERRUN is RECORDED by CONTROL (the card's data, R4 §4) and the session is NOT
    # revoked: its record stays RENDERED, its handles stay, its front gets no `K`; keys' own stop refuses its next
    # `Q` 402 `budget-overrun`. The record's overrun is the ledger's.
    async def go() -> dict:
        async with R3.World(py, CAP, response=OVER) as w:
            s1, f1 = await w.session(S1)
            q1 = await f1.q()
            await R3.aio_until(lambda: w.control.spend_overruns(S1), what="SPEND-OVERRUN recorded")
            settled = [o for o in B2.ledger_lines(w.keys.ledger_path) if o["kind"] == "settled"]
            return {"q1": q1, "recs": w.control.spend_overruns(S1), "settled": settled, "state": s1.state(),
                    "handles": w.control.handles(S1) is not None, "no_k": await no_k(f1), "q2": await f1.q(),
                    "drops_owed": w.control.drops_owed()}

    out = run(go())
    assert out["q1"].get("status") == 200, out
    (rec,), (line,) = out["recs"], out["settled"]
    assert line["overrun"] > 0, line
    assert {k: rec[k] for k in ("session", "route", "model", "overrun", "unproven")} == \
        {"session": S1, "route": "r", "model": "m1", "overrun": line["overrun"], "unproven": False}, rec
    assert rec["ref"][2] == line["ref"], (rec, line)
    assert out["state"] == R.RENDERED and out["handles"] and out["no_k"], out
    assert out["q2"].get("refused") == "budget-overrun", out


# ---- idempotency and the slow ack -----------------------------------------------------------------------------------

@process
def test_admin_verbs_idempotent(py):
    # §4.9 "Every verb is idempotent and acked": each of keys' admin verbs sent twice by CONTROL's own client answers the
    # same way and changes nothing more (a repeated FRONT-REGISTER keeps the registration: the front gets no `K` and is
    # served; DROP-SESSION answers dropped true, then false); and each of the attachment's verbs sent twice to CONTROL
    # (FRONT-LOST, SPEND-OVERRUN, SESSIONS) answers the same, with one revoke and one record.
    async def go() -> dict:
        async with R3.World(py, CAP) as w:
            s1, f1 = await w.session(S1)
            admin = w.control.keys_admin
            tok = w.control.store.get(S1).claims[R.REG_TOKEN]
            reg = dict(session=S1, front_uid=s1.front["uid"], pid=s1.front["pid"], start=s1.front["start"],
                       incarnation=1, reg_token=tok)
            out = {"register": [await in_thread(lambda: admin.call("FRONT-REGISTER", **reg)) for _ in range(2)],
                   "arm": [await in_thread(lambda: admin.call("FRONT-ARM", session=S1, reg_token=tok))
                           for _ in range(2)]}
            reloads = []
            for _ in range(2):
                try:
                    reloads.append(await in_thread(lambda: admin.call("RELOAD", generation=1, item_sha256="0" * 64,
                                                                      keep=[1])))
                except C.AdminRefused as e:
                    reloads.append(e.answer)
            out["reload"] = reloads
            out["no_k"] = await no_k(f1)
            out["q"] = await f1.q()
            out["drop"] = [await in_thread(lambda: admin.call("DROP-SESSION", session=S1)) for _ in range(2)]
            # the attachment's verbs, on the attachment CONTROL holds
            s2, f2 = await w.session(S2)
            att = w.control._attach
            ask = lambda op, **a: w.control.handle(att, {"v": 1, "op": op, "id": 7, **a})   # noqa: E731
            over = dict(session=S2, route="r", model="m1", ref=["a" * 32, "b" * 32, 9], overrun=5, unproven=True)
            out["overrun"] = [ask("SPEND-OVERRUN", **over) for _ in range(2)]
            out["overrun_recs"] = w.control.spend_overruns(S2)
            digest = C.digest_of(s2.token)
            out["sessions"] = [ask("SESSIONS", digests=[digest, "sha256:" + "0" * 64]) for _ in range(2)]
            out["lost"] = [ask("FRONT-LOST", session=S2) for _ in range(2)]
            await R3.aio_until(lambda: s2.state() == R.REVOKED, what="s2 revoked")
            await asyncio.sleep(0.3)
            out["gen"] = w.control.store.get(S2).generation
            out["lost_again"] = ask("FRONT-LOST", session=S2)
            await asyncio.sleep(0.3)
            out["gen_again"] = w.control.store.get(S2).generation
            out["not_attached"] = w.control.handle(C.Conn(R3.KEYS_UID), {"v": 1, "op": "SESSIONS", "id": 8,
                                                                          "digests": []})
            return out

    out = run(go())
    assert out["register"] == [{}, {}] and out["arm"] == [{}, {}], out
    assert out["reload"] == [{"id": out["reload"][0]["id"], "ok": False, "refused": "routemap-invalid:current"},
                             {"id": out["reload"][1]["id"], "ok": False, "refused": "routemap-invalid:current"}], out
    assert out["no_k"] and out["q"].get("status") == 200, out
    assert out["drop"] == [{"dropped": True}, {"dropped": False}], out
    assert out["overrun"] == [{"id": 7, "ok": True}] * 2 and len(out["overrun_recs"]) == 1, out
    zero = "sha256:" + "0" * 64
    assert out["sessions"] == [{"id": 7, "ok": True, "drop": [zero]}] * 2, out
    assert out["lost"] == [{"id": 7, "ok": True}] * 2 and out["lost_again"] == {"id": 7, "ok": True}, out
    assert out["gen_again"] == out["gen"], out
    assert out["not_attached"] == {"id": 8, "ok": False, "refused": "not-attached"}, out


class SlowKeys(FakeKeys):
    """FakeKeys whose FIRST DROP-SESSION answer is held `delay` seconds (the frames behind it wait with it)."""

    def __init__(self, path: Path, delay: float) -> None:
        self.delay, self._held = delay, False
        super().__init__(path)

    def _serve(self, s: socket.socket, i: int) -> None:
        try:
            while (req := C.read_frame(s)) is not None:
                self.log.append((i, req))
                if req["op"] == "DROP-SESSION" and not self._held:
                    self._held = True
                    time.sleep(self.delay)
                s.sendall(C.encode_frame({"id": req["id"], "ok": True}))
            self.ended.add(i)
        except (OSError, C.FrameMalformed, C.FrameTooLarge):
            pass


@pytest.mark.parametrize("delay,closes", [(0.6, False), (2.5, True)], ids=["within-v23", "past-v23"])
def test_slow_admin_ack_does_not_revoke_others(monkeypatch, delay, closes):
    # §4.5 / §4.9: an admin verb not acked within its 2 s attempt deadline (scaled to 0.2 s) is RETRIED until V23 (scaled
    # to 1.5 s); an ack inside V23 closes nothing, so the OTHER session (s2) stays live with its handles. Only past V23
    # does CONTROL close the wire, and that EOF's rule revokes s2 too (the boundary this test pins from both sides).
    monkeypatch.setattr(control_client, "V23_SECONDS", 1.5)
    d = Path(tempfile.mkdtemp(prefix="c3s", dir="/tmp"))
    try:
        (d / "s").mkdir(mode=0o700)
        keys = SlowKeys(d / "a.sock", delay)
        admin = C.KeysAdmin(d / "a.sock", answer_s=0.2, window_s=1.5)
        control = C.Control(C.StateStore(d / "s"), OP_UID, keys_admin=admin, sleep=lambda s: time.sleep(0.01))
        control.start()
        eofs: list = []
        seen = admin.on_eof
        admin.on_eof = lambda no: (eofs.append(no), seen(no))
        try:
            leases = {}
            for s in (S1, S2):
                op = C.Conn(OP_UID)
                nonce = control.handle(op, {"v": 1, "op": "LEASE-OPEN", "id": 1})["lease_nonce"]
                assert root(control, "ATTACH", lease_nonce=nonce, sudo_uid=OP_UID, session=s,
                            claims=attach_claims())["ok"]
                gen = root(control, "CAS", session=s, generation=0, **{"from": R.ALLOCATED}, to=R.RENDERING,
                           fields={})["generation"]
                assert root(control, "MINT", session=s, generation=gen, claims=mint_claims(queue_grant=False))["ok"]
                leases[s] = op
            t0 = time.monotonic()
            assert root(control, "REVOKE", session=S1)["ok"]
            took = time.monotonic() - t0
            control.join_eof(5)
            if closes:
                R3.until(lambda: control.store.get(S2).state == R.REVOKED, timeout=10, what="s2 revoked at V23")
            else:
                # keys reads the resent drop only once its held answer is out, so a slow runner can read the log before
                # the resend lands (CI run on 5982d9d3): wait for it, bounded, then hold a moment for a stray close
                R3.until(lambda: [r["op"] for _, r in keys.log].count("DROP-SESSION") >= 2, timeout=5,
                         what="the drop resent after its attempt deadline")
                time.sleep(0.3)
            drops = [r["op"] for _, r in keys.log].count("DROP-SESSION")
            s2 = control.store.get(S2)
            out = {"took": took, "drops": drops, "eofs": eofs, "connected": admin.connected(), "s2": s2.state,
                   "s2_handles": control.handles(S2) is not None}
        finally:
            control.stop()
            keys.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if closes:
        assert out["eofs"] and out["s2"] == R.REVOKED and not out["s2_handles"], out
    else:
        # the attempt deadline passed and the drop was sent again (keys reads the resends once its held answer is out)
        assert out["drops"] >= 2, out
        assert out["took"] >= delay - 0.05 and out["eofs"] == [] and out["connected"], out
        assert out["s2"] == R.RENDERING and out["s2_handles"], out


# ---- the attach-time RELOAD, and serve ------------------------------------------------------------------------------

class HoldReload(FakeKeys):
    """FakeKeys that never answers RELOAD and stamps when each request arrived (this process's monotonic clock)."""

    def __init__(self, path: Path) -> None:
        self.arrived: list = []
        super().__init__(path)

    def _serve(self, s: socket.socket, i: int) -> None:
        try:
            while (req := C.read_frame(s)) is not None:
                self.arrived.append((req["op"], time.monotonic()))
                self.log.append((i, req))
                if req["op"] != "RELOAD":
                    s.sendall(C.encode_frame({"id": req["id"], "ok": True}))
        except (OSError, C.FrameMalformed, C.FrameTooLarge):
            pass


def _attach_client(path: Path, generation, item_sha256) -> tuple:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(str(path))
    s.sendall(C.encode_frame({"v": 1, "op": "KEYS-ATTACH", "id": 1, "generation": generation,
                              "item_sha256": item_sha256}))
    ans = C.read_frame(s)
    return s, ans, time.monotonic()


def test_attach_reload_goes_after_the_attach_answer():
    # R4 §2 / §10: KEYS-ATTACH states keys' generation; when it differs from CONTROL's head CONTROL sends RELOAD of its
    # head on keys' admin wire AFTER the KEYS-ATTACH answer is written, never inside the handler (keys waits on the
    # answer, and a RELOAD keys holds would stall it until V23). Here keys never answers the RELOAD: the attach answer
    # still comes at once, and the RELOAD arrives after it. A keys that states CONTROL's head gets no RELOAD.
    head = {"generation": 3, "item_sha256": "a" * 64}
    d = Path(tempfile.mkdtemp(prefix="c3r", dir="/tmp"))
    try:
        (d / "s").mkdir(mode=0o700)
        keys = HoldReload(d / "a.sock")
        admin = C.KeysAdmin(d / "a.sock", answer_s=0.5, window_s=3.0)
        control = C.Control(C.StateStore(d / "s"), OP_UID, keys_uid=R3.KEYS_UID, keys_admin=admin,
                            routemap_head=lambda: head)
        # the server-side order, in one list (list.append is atomic): the attach answer handed to the socket, the
        # RELOAD's thread spawned, the RELOAD begun
        events: list = []
        spawn, reload = control._spawn, control._attach_reload
        control._spawn = lambda *a, **kw: (events.append("spawn"), spawn(*a, **kw))[1]
        control._attach_reload = lambda *a, **kw: (events.append("reload"), reload(*a, **kw))[1]

        class Ordered(C.ControlServer):
            def _send(self, sock, answer):
                ok = super()._send(sock, answer)
                if "attach" in answer:
                    events.append("answer-sent")
                return ok

        srv = Ordered(control, d / "c.sock", peer_uid=lambda sock: R3.KEYS_UID)
        srv.start()
        try:
            t0 = time.monotonic()
            s1, ans1, t_ans = _attach_client(d / "c.sock", None, None)
            stated_none = control.keys_serves()
            R3.until(lambda: any(op == "RELOAD" for op, _ in keys.arrived), what="RELOAD sent")
            reload_frame = next(r for _, r in keys.log if r["op"] == "RELOAD")
            order = list(events)
            s1.close()
            R3.until(lambda: control.keys_attached() is None, what="attachment 1 ended")
            n = len(keys.arrived)
            s2, ans2, _ = _attach_client(d / "c.sock", 3, "a" * 64)
            time.sleep(0.5)
            later = [op for op, _ in keys.arrived[n:]]
            serves = control.keys_serves()
            s2.close()
        finally:
            srv.stop()
            keys.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert ans1 == {"id": 1, "ok": True, "attach": 1} and t_ans - t0 < 1.0, (ans1, t_ans - t0)
    assert stated_none is None
    # keys never answers this RELOAD, so a RELOAD inside the handler would hold the answer above for the admin window
    # (3 s); the answer came at once, and the RELOAD was still sent
    assert reload_frame["generation"] == 3, reload_frame
    # the order itself, as CONTROL's side recorded it: the answer was on the socket before the RELOAD's thread was
    # spawned, and before the RELOAD began (a spawn or a call inside the handler would put it ahead of the answer)
    assert order[:3] == ["answer-sent", "spawn", "reload"], order
    assert ans2 == {"id": 1, "ok": True, "attach": 2} and "RELOAD" not in later, (ans2, later)
    assert serves == head


def test_serve_is_the_program_entry(tmp_path):
    # `control.serve(config)`: the start pass runs before the socket listens (a record left live by the previous run is
    # REVOKED), the socket is 0660, the server answers, and setting `stop` returns 0 with no revoke on the way down.
    d = Path(tempfile.mkdtemp(prefix="c3v", dir="/tmp"))
    try:
        (d / "s").mkdir(mode=0o700)
        store = C.StateStore(d / "s")
        rec = R.SessionState(session=S3, generation=0, state=R.ALLOCATED, claims=attach_claims(), lease_live=True,
                             event_kind="state", updated_at=R.timestamp(time.time()))
        store.put(rec)
        stop, ready, rc = threading.Event(), threading.Event(), []
        seen = {}

        def on_ready(server):
            seen["state"] = server.control.store.get(S3).state
            seen["mode"] = stat.S_IMODE(os.stat(d / "c.sock").st_mode)
            ready.set()

        cfg = C.ServeConfig(store_dir=d / "s", socket_path=d / "c.sock", operator_uid=OP_UID, keys_uid=R3.KEYS_UID,
                            peer_uid=lambda sock: R3.KEYS_UID, ready=on_ready, stop=stop)
        t = threading.Thread(target=lambda: rc.append(C.serve(cfg)), daemon=True)
        t.start()
        assert ready.wait(10)
        s, ans, _ = _attach_client(d / "c.sock", None, None)
        s.close()
        stop.set()
        t.join(10)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert seen == {"state": R.REVOKED, "mode": 0o660}, seen
    assert ans == {"id": 1, "ok": True, "attach": 1}, ans
    assert rc == [0] and not t.is_alive()
