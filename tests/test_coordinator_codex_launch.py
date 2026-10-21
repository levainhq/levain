"""Starting and watching a Codex coordinator launch (C3c). A fake app-server daemon replays what C3c's runs measured
on codex-cli 0.162.1 (1021+18 lane c3c): WebSocket over the unix control socket after an HTTP Upgrade, JSON-RPC with
no "jsonrpc" field, ``initialize`` naming ``codexHome``, and ``-32600 "thread not loaded: <id>"`` for a thread the
daemon does not have. No test here reaches a real Codex."""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import socket
import sqlite3
import struct
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

from levain.coordinator import codex_launch as cx
from levain.coordinator import launch as cl
from levain.coordinator.store import CoordStore

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeDaemon:
    """A daemon on ``<home>/app-server-control/app-server-control.sock``: one thread per connection."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.threads: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        path = cx.control_socket(home)
        path.parent.mkdir(parents=True)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(path))
        self.sock.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self.sock.close()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv(conn: socket.socket, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = conn.recv(n - len(out))
            if not chunk:
                raise EOFError
            out += chunk
        return out

    def _serve(self, conn: socket.socket) -> None:
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(1)
            key = next(ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n")
                       if ln.lower().startswith(b"sec-websocket-key:"))
            accept = base64.b64encode(hashlib.sha1(key + _GUID).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nconnection: Upgrade\r\nupgrade: websocket\r\n"
                         b"sec-websocket-accept: " + accept + b"\r\n\r\n")
            while True:
                h = self._recv(conn, 2)
                op, n = h[0] & 0x0F, h[1] & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._recv(conn, 2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._recv(conn, 8))[0]
                mask = self._recv(conn, 4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._recv(conn, n)))
                if op == 8:
                    return
                msg = json.loads(payload)
                assert "jsonrpc" not in msg
                if "id" not in msg:
                    continue
                reply = self._answer(msg["method"], msg.get("params") or {})
                body = json.dumps({"id": msg["id"], **reply}).encode()
                ln = len(body)
                frame = bytes([0x81]) + (bytes([ln]) if ln < 126 else bytes([126]) + struct.pack(">H", ln))
                conn.sendall(frame + body)
        except (EOFError, OSError):
            return
        finally:
            conn.close()

    def _answer(self, method: str, p: dict) -> dict:
        self.calls.append((method, p))
        tid = p.get("threadId")
        if method == "initialize":
            return {"result": {"codexHome": str(self.home)}}
        if method == "thread/start":
            tid = str(uuid.uuid4())
            self.threads[tid] = {"id": tid, "sessionId": tid, "cwd": p["cwd"], "name": None,
                                 "status": {"type": "idle"}, "turns": []}
            return {"result": {"thread": self.threads[tid]}}
        if tid not in self.threads:
            return {"error": {"code": -32600, "message": f"thread not loaded: {tid}"}}
        th = self.threads[tid]
        if method == "thread/name/set":
            th["name"] = p["name"]
            return {"result": {}}
        if method == "turn/start":
            turn = {"id": str(uuid.uuid4()), "status": "inProgress"}
            th["turns"].insert(0, turn)
            th["status"] = {"type": "active", "activeFlags": []}
            return {"result": {"turn": turn}}
        if method == "thread/read":
            return {"result": {"thread": {k: v for k, v in th.items() if k != "turns"}}}
        if method == "thread/turns/list":
            return {"result": {"data": th["turns"]}}
        if method == "turn/interrupt":
            for t in th["turns"]:
                if t["id"] == p["turnId"]:
                    t["status"] = "interrupted"
            th["status"] = {"type": "idle"}
            return {"result": {}}
        return {"error": {"code": -32601, "message": f"unknown method {method}"}}


@pytest.fixture
def home(monkeypatch):
    # a short path: a unix socket path is capped near 104 bytes on macOS, which pytest's tmp_path can pass
    d = Path(tempfile.mkdtemp(prefix="cx", dir="/tmp"))
    monkeypatch.setenv("CODEX_HOME", str(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(tmp_path, monkeypatch):
    q = tmp_path / "consent"
    q.mkdir(mode=0o700)
    work = tmp_path / "codex-install"
    work.mkdir()
    monkeypatch.setattr(cx, "hook_live", lambda d: (True, "wired to this install"))
    monkeypatch.setattr(cx, "UNLISTED_RECHECK", 0.0)
    s = CoordStore(q)
    # main refused this outright ("only claude is built"): C3c's reproduced failure (repro_main.txt)
    s.coord_set_agent("codex", cwd=str(work.resolve()))
    return s


def _woken(store: CoordStore) -> str:
    d, _ = store.accept_drop("fix the build", surface="cli")
    return store.woken_launch(d.drop_id)


def test_a_codex_coordinator_starts_on_the_daemon_and_ends_when_its_thread_is_gone(home, store):
    daemon = FakeDaemon(home)
    try:
        launch_id = _woken(store)
        got = cl.launch_session(store, launch_id)
        assert got.started, got.refusal
        ln = store.launch(launch_id)
        tid = ln.harness_session_id
        assert ln.state == "active" and tid in daemon.threads
        th = daemon.threads[tid]
        assert th["name"] == cl.session_name(ln) and th["cwd"] == str(Path(ln.cwd).resolve())
        assert got.attach == f"codex resume --remote unix://{cx.control_socket(home)} {tid}"
        start = next(p for m, p in daemon.calls if m == "thread/start")
        assert set(start) == {"cwd"}               # no approval or sandbox override: the user's config decides
        # the run's failure (run_done_pass1.txt): the daemon's answer for a thread it lacks is "thread not loaded",
        # which the first build read as "cannot read" and so never ended the launch
        del daemon.threads[tid]
        lines = cl.watch_once(store)
        assert any("ended: 0 turns" in line for line in lines), lines
        assert store.launch(launch_id).state == "ended"
    finally:
        daemon.close()


def test_no_daemon_refuses_before_the_spawn_claim_with_the_remedy(home, store, tmp_path):
    launch_id = _woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and got.remedy == "codex app-server daemon start"
    assert "not answering" in got.refusal and "left pending" in got.refusal
    assert store.launch(launch_id).state == "pending"           # no spawn claimed: it expires, which is no death
    c = sqlite3.connect(store.path)
    try:
        assert c.execute("SELECT COUNT(*) FROM launch_spawns").fetchone()[0] == 0
    finally:
        c.close()
