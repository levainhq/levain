"""The rig for the real-process keys tests of 3c-1 session B2 (T12, T13): keys as a child process in the launcher form
(`tests/b2_keys_driver.py`), a stand-in CONTROL that answers TOKEN-RESOLVE, a fake plain-TCP upstream that can hold
every response, and this test process acting as the one front (it registers and arms itself on keys' admin socket,
claims the status connection, and sends `Q` frames on `front.sock`)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from levain.broker import creds as CR
from levain.broker import gwwire
from levain.broker import keys as K
from levain.broker import peer
from levain.contracts import canon
from tests.test_broker_keys_upstream import BODY, PRICE

ROOT = Path(__file__).resolve().parents[1]
DRIVER = str(ROOT / "tests" / "b2_keys_driver.py")
TOKEN = "T" * 43
SECRET = "sk-upstream-test-SECRET-0123456789"
READY_S = 40.0


def lib(d: str, cap: int) -> None:
    """`test_broker_keys_upstream._lib` with the route's per-session cap as the argument (base `/2`)."""
    os.makedirs(os.path.join(d, "routes"), exist_ok=True)
    os.makedirs(os.path.join(d, "keys", "cred"), exist_ok=True)
    os.chmod(os.path.join(d, "keys"), 0o700)
    route = {"route": "r", "type": "brokered",
             "upstream": {"scheme": "https", "host": "api.example.com", "base_path": ""}, "credential": "c1",
             "requests": {"templates": [{"method": "POST", "path": "/v1/messages"}],
                          "headers": {"content-type": "application/json"}, "body_cap": 4096,
                          "body_codec": "anthropic-messages"},
             "max_session_microdollars": cap, "prices": {"m1": PRICE}}
    with open(os.path.join(d, "routes", "base.json"), "wb") as f:
        f.write(canon.canonical_bytes({"format": "levain-routes-base/2", "routes": [route]}))
    with open(os.path.join(d, "keys", "cred", "c1.json"), "w") as f:
        f.write(json.dumps({"format": CR.FORMAT, "name": "c1", "rev": "0" * 32, "hosts": ["api.example.com"],
                            "inject": {"header": "x-api-key", "scheme": ""}, "kind": "static", "secret": SECRET}))


def short_dir() -> str:
    return tempfile.mkdtemp(prefix="b2r", dir="/tmp")     # a unix socket path is capped near 104 bytes


class FakeControl:
    """CONTROL's socket: one request, one answer, `TOKEN-RESOLVE` only. Every digest resolves to session s1 and claims
    that name this process as the front."""

    def __init__(self, path: str) -> None:
        self.path, self.server, self.resolves = path, None, 0

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._on, self.path)

    async def _on(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            n = int.from_bytes(await r.readexactly(4), "big")
            req = json.loads(await r.readexactly(n))
            self.resolves += 1
            claims = {"routes": ["r"], "incarnation": 1,
                      "front": {"uid": os.geteuid(), "pid": os.getpid(), "start": peer.start_of(os.getpid())}}
            w.write(K.encode_admin({"id": req["id"], "ok": True, "session": "s1", "claims": claims}))
            await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


class FakeUpstream:
    """Plain TCP on 127.0.0.1. `conns` counts accepted connections, `heads` request heads fully received. Responses
    wait on `release` (cleared = hold every response open); then a 200 with a 2-byte body."""

    def __init__(self) -> None:
        self.conns = self.heads = 0
        self.server: Any = None
        self.port = 0
        self.release: Optional[asyncio.Event] = None
        self.writers: List[asyncio.StreamWriter] = []

    async def start(self, hold: bool = True) -> "FakeUpstream":
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.server = await asyncio.start_server(self._on, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def _on(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.conns += 1
        self.writers.append(w)
        try:
            await r.readuntil(b"\r\n\r\n")
            self.heads += 1
            await self.release.wait()
            w.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok")
            await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()

    async def close(self) -> None:
        for w in self.writers:
            w.close()
        self.server.close()
        await self.server.wait_closed()


class KeysProc:
    """keys as a child process. `start()` returns once it printed its `ready` line; `kill()` is SIGKILL, `stop()` is
    SIGTERM and waits for a clean exit."""

    def __init__(self, d: str, py: str, upstream_port: int, measure: Optional[str] = None) -> None:
        self.d, self.py, self.port, self.measure = d, py, upstream_port, measure
        self.p: Optional[subprocess.Popen] = None
        self.err: List[str] = []
        self.ready = threading.Event()
        self.exited = threading.Event()

    @property
    def front_path(self) -> str:
        return os.path.join(self.d, "keys", "front.sock")

    @property
    def admin_path(self) -> str:
        return os.path.join(self.d, "keys", "admin.sock")

    @property
    def ledger_path(self) -> str:
        return os.path.join(self.d, "keys", "ledger.jsonl")

    def _pump(self) -> None:
        assert self.p is not None and self.p.stderr is not None
        for raw in self.p.stderr:
            line = raw.decode(errors="replace").rstrip()
            self.err.append(line)
            if "gw-keys: ready" in line:
                self.ready.set()
        self.exited.set()
        self.ready.set()

    async def start(self) -> "KeysProc":
        env = {**os.environ, "PYTHONPATH": str(ROOT), "B2_UPSTREAM_PORT": str(self.port), "HOME": account_home()}
        if self.measure:
            env["B2_MEASURE"] = self.measure
        self.ready.clear()
        self.exited.clear()
        self.err = []
        self.p = subprocess.Popen(
            [self.py, DRIVER, "--runtime-root", self.d, "--lib-root", self.d, "--control",
             os.path.join(self.d, "control.sock"), "--test-single-uid"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
        threading.Thread(target=self._pump, daemon=True).start()
        ok = await asyncio.get_running_loop().run_in_executor(None, self.ready.wait, READY_S)
        assert ok and not self.exited.is_set() and any("gw-keys: ready" in x for x in self.err), \
            "keys did not become ready: %r" % self.err[-12:]
        return self

    def kill(self) -> None:
        assert self.p is not None
        self.p.send_signal(signal.SIGKILL)
        self.p.wait(15)
        self.exited.wait(5)

    def stop(self) -> int:
        assert self.p is not None
        self.p.send_signal(signal.SIGTERM)
        rc = self.p.wait(30)
        self.exited.wait(5)
        return rc

    def cleanup(self) -> None:
        if self.p is not None and self.p.poll() is None:
            self.p.kill()
            self.p.wait()


# ---- this process as the front ------------------------------------------------------------------------------------

def _frame(obj: dict) -> bytes:
    data = canon.canonical_bytes(obj)
    return len(data).to_bytes(4, "big") + data


class Front:
    """Registers and arms this process on keys' admin socket and claims the status connection; `q()` is one exchange."""

    def __init__(self, keys: KeysProc) -> None:
        self.keys = keys
        self.admin: Any = None
        self.status: Any = None

    async def _admin_call(self, rid: int, op: str, **args: Any) -> dict:
        r, w = self.admin
        w.write(_frame({"v": 1, "op": op, "id": rid, **args}))
        await w.drain()
        n = int.from_bytes(await asyncio.wait_for(r.readexactly(4), 10), "big")
        return json.loads(await r.readexactly(n))

    async def claim(self) -> "Front":
        self.admin = await asyncio.open_unix_connection(self.keys.admin_path)
        me = dict(session="s1", front_uid=os.geteuid(), pid=os.getpid(), start=peer.start_of(os.getpid()),
                  incarnation=1, reg_token=1)
        got = await self._admin_call(1, "FRONT-REGISTER", **me)
        assert got.get("ok") is True, got
        got = await self._admin_call(2, "FRONT-ARM", session="s1", reg_token=1)
        assert got.get("ok") is True, got
        r, w = await asyncio.open_unix_connection(self.keys.front_path)
        w.write(gwwire.encode(b"H", {"v": 1, "session": "s1", "incarnation": 1}))
        await w.drain()
        ft = await asyncio.wait_for(r.readexactly(5), 10)
        assert ft[:1] == b"A", ft
        self.status = (r, w)
        return self

    async def q(self, attempts: int = 1) -> Dict[str, Any]:
        """One exchange. A connection keys closed with no frame (its pending cap, at accept) is retried, up to
        `attempts`; `retries` in the result counts them. The retry is valid ONLY for that accept-time close: had keys
        admitted a hold and then dropped the connection before the first frame, the retry would reserve a second hold
        (T12 would then fail loudly on an extra `reserved` line)."""
        retries = 0
        while True:
            out = await self._q_once()
            if out["frames"] == [] and retries + 1 < attempts:
                retries += 1
                await asyncio.sleep(0.05)
                continue
            out["retries"] = retries
            return out

    async def _q_once(self) -> Dict[str, Any]:
        q = {"v": 1, "token": TOKEN, "route": "r", "method": "POST", "path": "/v1/messages", "query": "",
             "headers": [["content-type", "application/json"]], "body": "length:%d" % len(BODY),
             "upgrade": "none"}
        frames: List[tuple] = []
        r, w = await asyncio.open_unix_connection(self.keys.front_path)
        try:
            w.write(gwwire.encode(b"Q", q) + gwwire.encode(b"B", BODY) + gwwire.encode(b"E"))
            await w.drain()
            while True:
                try:
                    head = await r.readexactly(5)
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                n = int.from_bytes(head[1:], "big")
                body = await r.readexactly(n) if n else b""
                frames.append((head[:1], body))
                if head[:1] in (b"e", b"F"):
                    break
        finally:
            w.close()
        out: Dict[str, Any] = {"frames": frames}
        for t, b in frames:
            if t == b"F":
                out["refused"] = json.loads(b)["refused"]
            elif t == b"R":
                out["status"] = json.loads(b)["status"]
        return out

    async def close(self) -> None:
        for pair in (self.status, self.admin):
            if pair is not None:
                pair[1].close()
        self.status = self.admin = None


def ledger_lines(path: str) -> List[dict]:
    """The ledger's complete lines. The writer may be mid-batch while a test polls, so a trailing line without its
    `\n` is dropped (replay's torn-tail rule), never parsed."""
    with open(path, "rb") as f:
        raw = f.read()
    raw = raw[: raw.rfind(b"\n") + 1]
    return [json.loads(x) for x in raw.split(b"\n") if x]


def account_home() -> str:
    """The account's real home (the suite's scratch $HOME hides a root interpreter's user site, where h11 lives)."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return os.path.expanduser("~")


def h11_available(py: str) -> bool:
    cp = subprocess.run([py, "-c", "import h11"], capture_output=True, env={**os.environ, "HOME": account_home()})
    return cp.returncode == 0
