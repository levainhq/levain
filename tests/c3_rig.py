"""The rig for S1 3c-3's attachment tests (CONTRACT §4.5; 3c design r3 §2.1 (e); R4 §1.4): CONTROL as its program
starts it, through `control.serve`, on a thread of this process; keys as a child process in the launcher form
(`tests/c3_keys_driver.py`); and this process as root and the operator (in-process `Control.handle`, root by uid 0),
and as the one front (its STATUS connection and its `Q` exchanges on keys' `front.sock`).

Every connection to CONTROL's socket is read as keys' (the `peer_uid` seam): keys is the only process that connects
to it here. keys runs `--test-single-uid`, so its admin socket admits this uid, which is CONTROL's here too. With one
uid there is one `front_uid`, so one registered session at a time (a second registration replaces the first)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from levain.broker import gwwire, peer
from levain.slot import control as C
from levain.slot import records as R
from tests import b2_keys_rig as B2
from tests.test_broker_keys_upstream import BODY
from tests.test_slot_s2a_control import OP_UID, attach_claims, mint_claims

ROOT = Path(__file__).resolve().parents[1]
DRIVER = str(ROOT / "tests" / "c3_keys_driver.py")
#: the uid the `peer_uid` seam gives every connection to CONTROL's socket (keys' peer kind)
KEYS_UID = OP_UID + 3000


class ControlThread:
    """CONTROL through `control.serve(config)` on a daemon thread; `start()` returns once the start pass is done and
    the socket listens, with `control` the running `Control`. `stop()` sets the config's stop event (a stop is a
    crash, as far as any session can tell: no revoke)."""

    def __init__(self, d: str) -> None:
        self.d = d
        self.socket_path = os.path.join(d, "control.sock")
        os.makedirs(os.path.join(d, "sessions"), mode=0o700, exist_ok=True)
        self.stop_event = threading.Event()
        self.server: Optional[C.ControlServer] = None
        self._ready = threading.Event()
        self._rc: List[int] = []
        self.thread: Optional[threading.Thread] = None

    @property
    def control(self) -> C.Control:
        assert self.server is not None
        return self.server.control

    def _ready_cb(self, server: C.ControlServer) -> None:
        self.server = server
        self._ready.set()

    def start(self) -> "ControlThread":
        cfg = C.ServeConfig(store_dir=Path(self.d) / "sessions", socket_path=Path(self.socket_path),
                            operator_uid=OP_UID, keys_admin_path=Path(self.d) / "keys" / "admin.sock",
                            keys_uid=KEYS_UID, peer_uid=lambda sock: KEYS_UID, ready=self._ready_cb,
                            stop=self.stop_event)

        def run() -> None:
            self._rc.append(C.serve(cfg))

        self.thread = threading.Thread(target=run, daemon=True, name="c3-control")
        self.thread.start()
        assert self._ready.wait(10), "CONTROL did not start"
        return self

    def stop(self) -> int:
        self.stop_event.set()
        assert self.thread is not None
        self.thread.join(10)
        assert not self.thread.is_alive()
        return self._rc[0]


def until(cond, timeout: float = 15.0, what: str = "") -> None:
    """A blocking poll for the thread-side checks (CONTROL's state); `aio_until` is the loop's."""
    import time
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "condition not met within %s s: %s" % (timeout, what)
        time.sleep(0.02)


async def aio_until(cond, timeout: float = 15.0, what: str = "") -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "condition not met within %s s: %s" % (timeout, what)
        await asyncio.sleep(0.02)


def _ok(ans: Dict[str, Any]) -> Dict[str, Any]:
    assert ans.get("ok") is True, ans
    return ans


class Session:
    """One brokered interactive egress session made through CONTROL's own verbs: LEASE-OPEN (the operator's lease,
    an in-process `Conn` that stays live until `lease.eof()`), ATTACH, CAS -> RENDERING, MINT (this process the front:
    CONTROL sends FRONT-REGISTER to keys), FRONT-ARM (CONTROL relays it to keys), CAS -> RENDERED, SECRETS."""

    def __init__(self, control: C.Control, session: str, routes: Tuple[str, ...] = ("r",)) -> None:
        self.control, self.session = control, session
        root = C.Conn(0)
        self.op = C.Conn(OP_UID)
        nonce = _ok(control.handle(self.op, {"v": 1, "op": "LEASE-OPEN", "id": 1}))["lease_nonce"]
        _ok(control.handle(root, {"v": 1, "op": "ATTACH", "id": 2, "lease_nonce": nonce, "sudo_uid": OP_UID,
                                  "session": session, "claims": attach_claims()}))
        gen = _ok(control.handle(root, {"v": 1, "op": "CAS", "id": 3, "session": session, "generation": 0,
                                        "from": R.ALLOCATED, "to": R.RENDERING, "fields": {}}))["generation"]
        pid = os.getpid()
        self.front = {"uid": os.geteuid(), "pid": pid, "start": peer.start_of(pid)}
        claims = mint_claims(routes=list(routes), queue_grant=False, front=self.front)
        gen = _ok(control.handle(root, {"v": 1, "op": "MINT", "id": 4, "session": session, "generation": gen,
                                        "claims": claims}))["generation"]
        self.gen = gen

    def arm(self) -> "Session":
        root = C.Conn(0)
        _ok(self.control.handle(root, {"v": 1, "op": "FRONT-ARM", "id": 5, "session": self.session,
                                       "generation": self.gen}))
        self.gen = _ok(self.control.handle(root, {"v": 1, "op": "CAS", "id": 6, "session": self.session,
                                                  "generation": self.gen, "from": R.RENDERING, "to": R.RENDERED,
                                                  "fields": {}}))["generation"]
        self.token = _ok(self.control.handle(root, {"v": 1, "op": "SECRETS", "id": 7,
                                                    "session": self.session}))["token"]
        return self

    def state(self) -> str:
        rec = self.control.store.get(self.session)
        assert rec is not None
        return rec.state

    def lease_eof(self) -> None:
        assert self.op.lease is not None
        self.control.lease_eof(self.op.lease)


class Front:
    """This process as the session's front on keys' `front.sock`: `claim()` opens the STATUS connection (`H`, then keys'
    `A`); `q()` is one exchange with the session's token; `status_k()` reads the STATUS connection until keys' `K` or
    its EOF."""

    def __init__(self, keys: B2.KeysProc, session: Session) -> None:
        self.keys, self.session = keys, session
        self.status: Optional[Tuple[asyncio.StreamReader, asyncio.StreamWriter]] = None

    async def claim(self) -> "Front":
        r, w = await asyncio.open_unix_connection(self.keys.front_path)
        w.write(gwwire.encode(b"H", {"v": 1, "session": self.session.session, "incarnation": 1}))
        await w.drain()
        ft = await asyncio.wait_for(r.readexactly(5), 10)
        assert ft[:1] == b"A", ft
        await r.readexactly(int.from_bytes(ft[1:], "big"))   # `A` carries `{}`
        self.status = (r, w)
        return self

    async def status_k(self, timeout: float = 15.0) -> Optional[bytes]:
        """The next frame type on the STATUS connection (b"K"), or None at its EOF."""
        assert self.status is not None
        try:
            head = await asyncio.wait_for(self.status[0].readexactly(5), timeout)
        except asyncio.IncompleteReadError:
            return None
        return head[:1]

    async def drop_status(self) -> None:
        """The front goes away on its own: its STATUS connection closes without keys' `K`."""
        assert self.status is not None
        self.status[1].close()
        self.status = None

    async def q(self, body: bytes = BODY, token: Optional[str] = None) -> Dict[str, Any]:
        q = {"v": 1, "token": token or self.session.token, "route": "r", "method": "POST", "path": "/v1/messages",
             "query": "", "headers": [["content-type", "application/json"]], "body": "length:%d" % len(body),
             "upgrade": "none"}
        frames: List[tuple] = []
        r, w = await asyncio.open_unix_connection(self.keys.front_path)
        try:
            try:
                w.write(gwwire.encode(b"Q", q) + gwwire.encode(b"B", body) + gwwire.encode(b"E"))
                await w.drain()
            except ConnectionError:
                return {"frames": []}   # closed at accept before the request was taken (Linux resets the write)
            while True:
                try:
                    head = await asyncio.wait_for(r.readexactly(5), 20)
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                n = int.from_bytes(head[1:], "big")
                data = await r.readexactly(n) if n else b""
                frames.append((head[:1], data))
                if head[:1] in (b"e", b"F"):
                    break
        finally:
            w.close()
        out: Dict[str, Any] = {"frames": [t for t, _ in frames]}
        for t, b in frames:
            if t == b"F":
                out["refused"] = json.loads(b)["refused"]
            elif t == b"R":
                out["status"] = json.loads(b)["status"]
        return out

    async def close(self) -> None:
        if self.status is not None:
            self.status[1].close()
            self.status = None


class World:
    """lib + CONTROL (serve) + a fake upstream + keys (child). `env` scales keys' attachment clocks (the driver's
    $C3_* names); `response` is the fake upstream's answer (default: usage at exactly the hold)."""

    def __init__(self, py: str, cap: int, env: Optional[Dict[str, str]] = None,
                 response: Optional[bytes] = None) -> None:
        self.py, self.cap, self.env, self.response = py, cap, dict(env or {}), response

    async def __aenter__(self) -> "World":
        self.d = B2.short_dir()
        B2.lib(self.d, self.cap)
        self.ctl = ControlThread(self.d).start()
        self.up = await B2.FakeUpstream(self.response).start(hold=False)
        self.keys = B2.KeysProc(self.d, self.py, self.up.port, driver=DRIVER, env=self.env)
        self.fronts: List[Front] = []
        await self.keys.start()
        await aio_until(lambda: self.ctl.control.keys_attached() is not None, what="CONTROL sees keys attached")
        return self

    @property
    def control(self) -> C.Control:
        return self.ctl.control

    async def session(self, name: str) -> Tuple[Session, Front]:
        s = await asyncio.get_running_loop().run_in_executor(None, lambda: Session(self.control, name).arm())
        f = await Front(self.keys, s).claim()
        self.fronts.append(f)
        return s, f

    async def __aexit__(self, *exc) -> None:
        for f in self.fronts:
            await f.close()
        self.keys.cleanup()
        await self.up.close()
        if self.ctl.thread is not None and self.ctl.thread.is_alive():
            self.ctl.stop()
        shutil.rmtree(self.d, ignore_errors=True)
