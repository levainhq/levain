"""A fake Codex app-server for the Codex tests: what C3c's runs measured on codex-cli 0.162.1 (1021+18 lane c3c), plus
the traffic the CH-D3 wire must sort (rev 2 §3.2, §4, §6.2, §6.6).

Two transports over one handler: :class:`FakeDaemon` listens on ``<home>/app-server-control/app-server-control.sock``
(WebSocket after an HTTP Upgrade, JSON-RPC with no "jsonrpc" field, ``initialize`` naming ``codexHome``, ``-32600
"thread not loaded: <id>"`` for a thread it does not have), and :meth:`FakeDaemon.serve_pipes` speaks one JSON object
per line over a pipe pair, as ``codex app-server`` does on stdio. Each connection is a :class:`Conn` a test can push
notifications, server requests and raw frames down. A test scripts the daemon with ``before_reply[method]`` (runs
after the answer is computed and before it is sent, so it can name a thread ``thread/start`` just made) and
``after_reply[method]``, and ``on_client_reply`` (runs when the client answers a server request). Clearing
:attr:`FakeDaemon.reading` stops the daemon reading what the client sends (its buffers then fill, so a client write
blocks); on the socket :meth:`Conn.ping` sends a ping and the pongs that come back are kept in ``pongs``.
:meth:`Conn.send_bytes` writes bytes with no framing at all (a hostile peer's malformed or stalled traffic), and
:class:`TricklePeer` is a peer that takes a write a few bytes at a time without ever blocking.

Not measured, the fake's own choices: the error a request sent before ``initialized`` gets (:data:`NOT_INITIALIZED`),
and the wording of the stale-steer error (:meth:`FakeDaemon._answer`). No test here reaches a real Codex."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import socket
import struct
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from levain.coordinator import codex_launch as cx

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
NOT_INITIALIZED = {"code": -32002, "message": "Not initialized"}


class Conn:
    """One client connection: everything sent down it goes out whole, under one lock. :attr:`hangup` drops it from the
    daemon's side."""

    def __init__(self, write: Callable[[bytes], None], hangup: Callable[[], None],
                 write_frame: Callable[..., None] | None = None,
                 write_bytes: Callable[[bytes], None] | None = None) -> None:
        self._write = write
        self._write_frame = write_frame
        self._write_bytes = write_bytes
        self.hangup = hangup
        self._lock = threading.Lock()
        self.initialized = False
        self.last_id: Any = None

    def send_raw(self, data: bytes) -> None:
        with self._lock:
            self._write(data)

    def send(self, obj: Any) -> None:
        self.send_raw(json.dumps(obj).encode())

    def send_bytes(self, data: bytes) -> None:
        """``data`` exactly as given: no WebSocket frame, no line end."""
        with self._lock:
            self._write_bytes(data)

    def ping(self, payload: bytes) -> None:
        self.frame(9, payload)

    def frame(self, op: int, payload: bytes, *, fin: bool = True, declared: int | None = None) -> None:
        """One raw WebSocket frame; ``declared`` overrides the length the header states (the payload is then short)."""
        with self._lock:
            self._write_frame(op, payload, fin, declared)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any], rid: Any) -> None:
        self.send({"id": rid, "method": method, "params": params})


def _ws_frame(body: bytes, op: int = 1, fin: bool = True, declared: int | None = None) -> bytes:
    n = len(body) if declared is None else declared
    head = bytes([(0x80 if fin else 0) | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else
                            bytes([127]) + struct.pack(">Q", n))
    return head + body


# --- builders for the server traffic the tests send -----------------------------------------------------------
def command_approval(tid: str, turn_id: str, item_id: str, command: str = "ls", **extra: Any) -> dict[str, Any]:
    return {"threadId": tid, "turnId": turn_id, "itemId": item_id, "startedAtMs": 1, "kind": "command",
            "command": command, "cwd": "/tmp", "availableDecisions": ["accept", "decline", "cancel"], **extra}


def file_approval(tid: str, turn_id: str, item_id: str, **extra: Any) -> dict[str, Any]:
    return {"threadId": tid, "turnId": turn_id, "itemId": item_id, "startedAtMs": 1, "reason": None, **extra}


def non_approval_requests(tid: str, turn_id: str) -> dict[str, dict[str, Any]]:
    """One schema-shaped params object per non-approval server request of the registry (rev 2 §6.6)."""
    return {
        "item/permissions/requestApproval": {"threadId": tid, "turnId": turn_id, "itemId": "p1", "startedAtMs": 1,
                                             "cwd": "/tmp", "permissions": {"network": {"enabled": True}}},
        "mcpServer/elicitation/request": {"threadId": tid, "serverName": "srv", "message": "?"},
        "item/tool/requestUserInput": {"threadId": tid, "turnId": turn_id, "itemId": "u1", "isBlocking": True,
                                       "questions": []},
        "item/tool/call": {"threadId": tid, "turnId": turn_id, "callId": "c1", "tool": "t", "arguments": {}},
        "account/chatgptAuthTokens/refresh": {"reason": "unauthorized"},
        "attestation/generate": {},
        "applyPatchApproval": {"callId": "a1", "conversationId": tid, "fileChanges": {}},
        "execCommandApproval": {"callId": "e1", "conversationId": tid, "command": ["ls"], "cwd": "/tmp",
                                "parsedCmd": []},
    }


def settings_updated(tid: str, approval_policy: str = "on-request") -> dict[str, Any]:
    return {"threadId": tid, "threadSettings": {"approvalPolicy": approval_policy, "approvalsReviewer": "user",
                                                "collaborationMode": "default", "cwd": "/tmp", "model": "m",
                                                "modelProvider": "openai",
                                                "sandboxPolicy": {"type": "workspaceWrite"}}}


class TricklePeer:
    """A peer that is always ready to take more and takes it slowly: each :meth:`write` waits ``delay`` seconds and
    accepts at most ``chunk`` bytes (into a socket pair whose far end a thread drains), so no write ever blocks or
    makes zero progress. It is passed as a framing's ``write`` callable: a kernel socket blocks once its buffer
    fills, so the shape is built at the write seam, where the framing meets it."""

    def __init__(self, chunk: int = 256, delay: float = 0.001) -> None:
        self.chunk, self.delay = chunk, delay
        self.near, self._far = socket.socketpair(socket.AF_UNIX)
        self.taken = 0
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        with contextlib.suppress(OSError):
            while self._far.recv(65536):
                pass

    def write(self, view: memoryview) -> int:
        time.sleep(self.delay)
        n = self.near.send(view[:self.chunk])
        self.taken += n
        return n

    def close(self) -> None:
        self.near.close()
        self._far.close()


class FakeDaemon:
    """The fake app-server. ``home`` given: it listens on that CODEX_HOME's control socket; ``FakeDaemon(None)``
    serves pipes only (:meth:`serve_pipes`)."""

    def __init__(self, home: Path | None) -> None:
        self.home = home
        self.threads: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.client_notifications: list[tuple[str, Any]] = []
        self.client_replies: list[dict[str, Any]] = []
        self.refused_before_init: list[str] = []
        self.pongs: list[bytes] = []
        self.bad_frames: list[bytes] = []
        self.reading = threading.Event()
        self.reading.set()
        self.gated = threading.Event()                  # set when a connection stops at the closed ``reading`` gate
        self.conns: list[Conn] = []
        self.hooks: list[dict[str, Any]] = []
        self.before_reply: dict[str, Callable[[Conn, dict, dict], None]] = {}
        self.after_reply: dict[str, Callable[[Conn, dict, dict], None]] = {}
        self.on_client_reply: Callable[[Conn, dict], None] | None = None
        self.sock: socket.socket | None = None
        if home is not None:
            path = cx.control_socket(home)
            path.parent.mkdir(parents=True)
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.bind(str(path))
            self.sock.listen(8)
            threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()

    # -- transports --
    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve_ws, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv(conn: socket.socket, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = conn.recv(n - len(out))
            if not chunk:
                raise EOFError
            out += chunk
        return out

    def _serve_ws(self, conn: socket.socket) -> None:
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                got = conn.recv(1)
                if not got:
                    raise EOFError
                head += got
                if len(head) > 8192:
                    conn.close()
                    return
            key = next(ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n")
                       if ln.lower().startswith(b"sec-websocket-key:"))
            accept = base64.b64encode(hashlib.sha1(key + _GUID).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nconnection: Upgrade\r\nupgrade: websocket\r\n"
                         b"sec-websocket-accept: " + accept + b"\r\n\r\n")
            c = Conn(lambda body: conn.sendall(_ws_frame(body)), lambda: conn.shutdown(socket.SHUT_RDWR),
                     lambda op, body, fin=True, declared=None: conn.sendall(
                         _ws_frame(body, op, fin, declared)), conn.sendall)
            self.conns.append(c)
            while True:
                if not self.reading.is_set():
                    self.gated.set()
                self.reading.wait()
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
                if op == 10:
                    self.pongs.append(payload)
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    self.bad_frames.append(payload[:200])        # a corrupt stream: the connection is dropped
                    return
                self._handle(c, msg)
        except (EOFError, OSError):
            return
        finally:
            conn.close()

    def serve_pipes(self) -> tuple[BinaryIO, BinaryIO]:
        """A line-framed connection over two pipes: returns the client's ``(rfile, wfile)``. The daemon side closes
        its output when the client closes its write end."""
        c_r, d_w = os.pipe()
        d_r, c_w = os.pipe()
        out = os.fdopen(d_w, "wb")
        inp = os.fdopen(d_r, "rb")

        def write(body: bytes) -> None:
            out.write(body + b"\n")
            out.flush()

        def write_bytes(data: bytes) -> None:
            out.write(data)
            out.flush()

        c = Conn(write, out.close, write_bytes=write_bytes)
        self.conns.append(c)

        def serve() -> None:
            try:
                while True:
                    if not self.reading.is_set():
                        self.gated.set()
                    self.reading.wait()
                    line = inp.readline()
                    if not line:
                        break
                    self._handle(c, json.loads(line))
            except (OSError, ValueError):
                pass
            finally:
                for f in (out, inp):
                    try:
                        f.close()
                    except OSError:
                        pass

        threading.Thread(target=serve, daemon=True).start()
        return os.fdopen(c_r, "rb"), os.fdopen(c_w, "wb")

    # -- the handler --
    def _handle(self, c: Conn, msg: dict) -> None:
        assert "jsonrpc" not in msg
        if "method" not in msg:                         # the client answering a server request
            self.client_replies.append(msg)
            if self.on_client_reply is not None:
                self.on_client_reply(c, msg)
            return
        method, params = msg["method"], msg.get("params") or {}
        if "id" not in msg:
            self.client_notifications.append((method, params))
            if method == "initialized":
                c.initialized = True
            return
        c.last_id = msg["id"]
        if method != "initialize" and not c.initialized:
            self.refused_before_init.append(method)
            c.send({"id": msg["id"], "error": NOT_INITIALIZED})
            return
        reply = self._answer(method, params)
        if method in self.before_reply:
            self.before_reply[method](c, params, reply)
        c.send({"id": msg["id"], **reply})
        if method in self.after_reply:
            self.after_reply[method](c, params, reply)

    def _answer(self, method: str, p: dict) -> dict:
        self.calls.append((method, p))
        tid = p.get("threadId")
        if method == "initialize":
            return {"result": {"codexHome": str(self.home)}}
        if method == "hooks/list":
            return {"result": {"data": self.hooks}}
        if method == "thread/start":
            tid = str(uuid.uuid4())
            self.threads[tid] = {"id": tid, "sessionId": tid, "cwd": p.get("cwd"), "name": None,
                                 "status": {"type": "idle"}, "turns": [], "items": []}
            return {"result": {"thread": {k: v for k, v in self.threads[tid].items() if k != "items"}}}
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
        if method == "turn/steer":
            live = next((t for t in th["turns"] if t["status"] == "inProgress"), None)
            if live is None or live["id"] != p.get("expectedTurnId"):
                live_id = live["id"] if live else None
                return {"error": {"code": -32600, "message": f"expected turn {p.get('expectedTurnId')} is not the "
                                                            f"active turn ({live_id})"}}
            return {"result": {"turnId": live["id"]}}
        if method == "thread/read":
            return {"result": {"thread": {k: v for k, v in th.items() if k not in ("turns", "items")}}}
        if method == "thread/resume":
            return {"result": {"thread": {k: v for k, v in th.items() if k not in ("turns", "items")},
                               "approvalPolicy": "on-request", "approvalsReviewer": "user", "cwd": th["cwd"]}}
        if method == "thread/unsubscribe":
            return {"result": {}}
        if method == "thread/turns/list":
            turns = th["turns"] if p.get("sortDirection") != "asc" else list(reversed(th["turns"]))
            return {"result": {"data": turns}}
        if method == "thread/items/list":
            items = [i for i in th["items"] if p.get("turnId") in (None, i.get("turnId"))]
            return {"result": {"data": items}}
        if method == "turn/interrupt":
            for t in th["turns"]:
                if t["id"] == p["turnId"]:
                    t["status"] = "interrupted"
            th["status"] = {"type": "idle"}
            return {"result": {}}
        return {"error": {"code": -32601, "message": f"unknown method {method}"}}
