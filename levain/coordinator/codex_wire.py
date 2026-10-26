"""levain.coordinator.codex_wire — one demultiplexing JSON-RPC client for a Codex app-server (CH-D3 rev 2 §3.2; S1).

Codex offers a long-lived session only through ``codex app-server``: JSON-RPC objects WITHOUT the ``"jsonrpc"`` field,
either as WebSocket messages on the user's daemon control socket (:class:`WsFraming`, the framing
:mod:`levain.coordinator.codex_launch` has spoken since C3c, moved here) or one object per line over a pipe pair to a
process Levain spawns (:class:`LineFraming`). :class:`CodexWire` is the one client over either:

- **one reader thread** reads every inbound message and sorts it: a response (an id, no method) goes to the call waiting
  on it; a notification (a method, no id) and a server request (a method AND an id) go to ONE inbound queue, in arrival
  order, as :class:`Inbound` items. Writes go out under one lock. A call made FROM the reader thread raises at once:
  its answer could only be read by the thread that is waiting on it;
- **typed ids**: an id is kept as the exact JSON value (``0`` and ``"0"`` are different requests) and "has an id" is a
  key-presence test, never truthiness. A reply echoes the id as it came;
- **``gen``**: every connection mints a fresh random hex string, stamped on every item it queues. It is an opaque
  connect id: an item from another ``gen`` is never answered on this one. It carries NO order (the r2 review moves
  ordering to a persisted ordinal in rev 3);
- **the thread filter**: a notification or server request naming a ``threadId`` other than the wire's thread is
  queued as a ``foreign`` item and can never be answered (answering could decide another thread's request). A method
  whose pinned schema requires a ``threadId`` and arrives without one is ``unparseable``. While ``thread/start`` is
  outstanding (:meth:`CodexWire.start_thread`) the wire has no thread yet, so thread-scoped frames are BUFFERED (at most
  :data:`BOOTSTRAP_FRAME_CAP`, and only until that call's deadline): the returned id is bound, the matching frames are
  released in arrival order, and the rest are queued as ``foreign``;
- a frame over :data:`_MAX_MESSAGE` and, on the socket, a frame that is not a JSON object are ``unparseable`` items and
  the reader goes on; on a pipe a line that is not JSON is a log line (``codex app-server`` writes ANSI log lines to
  its stdout, measured over stdio in runs72), counted in :attr:`CodexWire.log_lines` and a running digest, never an item;
- :meth:`CodexWire.open` sends ``initialize`` (``experimentalApi: false`` and :data:`OPT_OUT_NOTIFICATIONS`), then the
  ``initialized`` notification; the wire sends no other request before that.

The module also holds, as data, the server-request registry (:data:`SERVER_REQUESTS`: what the session reads of each
request and the schema-valid reply that denies it, rev 2 §6.2 / §6.6) and the method surface pinned for the one tested
Codex version (:data:`PINNED_SURFACE`). The wire decides nothing with the registry: it never answers a request on its
own. Stdlib only.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import socket
import struct
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

__all__ = ["BOOTSTRAP_FRAME_CAP", "CATCH_ALL", "NO_ID", "OPT_OUT_NOTIFICATIONS", "PINNED_SURFACE", "PINNED_VERSION",
           "RELIED_NOTIFICATIONS", "SERVER_REQUESTS", "CodexError", "CodexWire", "Inbound", "LineFraming", "ServerRequestRow", "WsFraming",
           "server_request_row"]

_CONNECT_TIMEOUT = 10.0
_CALL_TIMEOUT = 60.0
_MAX_MESSAGE = 16 * 1024 * 1024 + 4096     # the daemon's own header names 16777216 bytes unfragmented
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
BOOTSTRAP_FRAME_CAP = 256
_LATE_IDS_CAP = 1024


class CodexError(Exception):
    """The daemon could not be reached, or answered with an error (``.error`` holds its error object then)."""

    def __init__(self, message: str, error: Any = None) -> None:
        super().__init__(message)
        self.error = error


class _Oversize(Exception):
    """A message over :data:`_MAX_MESSAGE`, already read past: the stream is still in step."""

    def __init__(self, size: int) -> None:
        super().__init__(size)
        self.size = size


# --- framings ----------------------------------------------------------------------------------------------------
class WsFraming:
    """WebSocket (RFC 6455) client framing over a unix socket after a plain HTTP/1.1 Upgrade (measured on the Codex
    daemon's control socket, codex-cli 0.162.1). :meth:`recv_message` returns one whole message's bytes."""

    kind = "ws"

    def __init__(self, path: Path, timeout: float = _CONNECT_TIMEOUT) -> None:
        self._s = socket.socket(socket.AF_UNIX)
        try:
            self._s.settimeout(timeout)
            self._s.connect(str(path))
            key = base64.b64encode(os.urandom(16))
            self._s.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                            b"Sec-WebSocket-Key: " + key + b"\r\nSec-WebSocket-Version: 13\r\n\r\n")
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self._s.recv(4096)
                if not chunk or len(buf) > 65536:
                    raise CodexError("the daemon closed the connection during the WebSocket handshake")
                buf += chunk
            head, self._buf = buf.split(b"\r\n\r\n", 1)
            lines = head.split(b"\r\n")
            if b" 101 " not in lines[0] + b" ":
                raise CodexError(f"the daemon refused the WebSocket upgrade: {lines[0][:120].decode(errors='replace')}")
            want = base64.b64encode(hashlib.sha1(key + _GUID).digest())
            accept = next((ln.split(b":", 1)[1].strip() for ln in lines[1:]
                           if ln.lower().startswith(b"sec-websocket-accept:")), None)
            if accept != want:
                raise CodexError("the daemon's WebSocket accept key does not match")
        except BaseException:
            self._s.close()
            raise

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._send(8, b"")
        self._s.close()

    def shutdown(self) -> None:
        """Unblock a reader waiting in :meth:`recv_message` (it then sees the connection closed)."""
        with contextlib.suppress(OSError):
            self._send(8, b"")
        with contextlib.suppress(OSError):
            self._s.shutdown(socket.SHUT_RDWR)

    def settimeout(self, timeout: float | None) -> None:
        self._s.settimeout(timeout)

    def send_text(self, data: bytes) -> None:
        self._send(1, data)

    def _send(self, op: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else
                                     bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                                     bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self._s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _need(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._s.recv(65536)
            if not chunk:
                raise CodexError("the daemon closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _skip(self, n: int) -> None:
        while n > 0:
            if not self._buf:
                self._buf = self._s.recv(65536)
                if not self._buf:
                    raise CodexError("the daemon closed the connection")
            take = min(n, len(self._buf))
            self._buf = self._buf[take:]
            n -= take

    def recv_message(self, *, skip_oversize: bool = False) -> bytes:
        """One whole data message (fragments joined; pings answered). A masked frame raises :class:`CodexError`. A
        message over :data:`_MAX_MESSAGE` raises :class:`CodexError` at once, or, with ``skip_oversize``, is read past
        to its last fragment and raises :class:`_Oversize` (the stream stays usable)."""
        msg = b""
        dropped = 0
        while True:
            h = self._need(2)
            fin, op, n = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._need(8))[0]
            if h[1] & 0x80 or (not skip_oversize and len(msg) + n > _MAX_MESSAGE):
                raise CodexError("the daemon sent a frame this client does not take (masked, or too large)")
            if op in (0, 1, 2) and (dropped or len(msg) + n > _MAX_MESSAGE):
                self._skip(n)
                dropped += len(msg) + n
                msg = b""
                if fin:
                    raise _Oversize(dropped)
                continue
            payload = self._need(n)
            if op == 9:
                self._send(10, payload)
            elif op == 8:
                raise CodexError("the daemon closed the connection")
            elif op in (0, 1, 2):
                msg += payload
                if fin:
                    return msg


class LineFraming:
    """One JSON object per line over a pipe pair (``codex app-server`` on stdio): ``rfile`` is the child's stdout,
    ``wfile`` its stdin, both binary. Closing ends only Levain's ends; the reader stops when the peer closes its
    stdout."""

    kind = "line"

    def __init__(self, rfile: BinaryIO, wfile: BinaryIO) -> None:
        self._r = rfile
        self._w = wfile

    def send_text(self, data: bytes) -> None:
        self._w.write(data + b"\n")
        self._w.flush()

    def recv_message(self, *, skip_oversize: bool = True) -> bytes:
        line = self._r.readline(_MAX_MESSAGE + 1)
        if not line:
            raise CodexError("the app-server closed its output")
        if len(line) > _MAX_MESSAGE and not line.endswith(b"\n"):
            size = len(line)
            while True:
                more = self._r.readline(65536)
                size += len(more)
                if not more or more.endswith(b"\n"):
                    break
            if not skip_oversize:
                raise CodexError("the app-server sent a line this client does not take (too large)")
            raise _Oversize(size)
        return line.rstrip(b"\r\n")

    def shutdown(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._w.close()

    def close(self) -> None:
        self.shutdown()
        with contextlib.suppress(OSError, ValueError):
            self._r.close()


# --- the pinned surface and the server-request registry ------------------------------------------------------------
PINNED_VERSION = "0.162.1"
# Each method this module or the CH-D3 session relies on -> its params' REQUIRED fields, as
# `codex app-server generate-json-schema` writes them for the pinned version (test_coordinator_codex_wire regenerates
# the schema with the installed codex and compares).
PINNED_SURFACE: Mapping[str, Mapping[str, Mapping[str, tuple[str, ...]]]] = {
    "0.162.1": {
        "ClientRequest": {
            "initialize": ("clientInfo",),
            "thread/start": (),
            "thread/resume": ("threadId",),
            "thread/unsubscribe": ("threadId",),
            "thread/read": ("threadId",),
            "thread/turns/list": ("threadId",),
            "thread/items/list": ("threadId",),
            "hooks/list": (),
            "turn/start": ("input", "threadId"),
            "turn/steer": ("expectedTurnId", "input", "threadId"),
            "turn/interrupt": ("threadId", "turnId"),
        },
        "ServerRequest": {
            "item/commandExecution/requestApproval": ("itemId", "startedAtMs", "threadId", "turnId"),
            "item/fileChange/requestApproval": ("itemId", "startedAtMs", "threadId", "turnId"),
            "item/tool/requestUserInput": ("isBlocking", "itemId", "questions", "threadId", "turnId"),
            "mcpServer/elicitation/request": ("serverName", "threadId"),
            "item/permissions/requestApproval": ("cwd", "itemId", "permissions", "startedAtMs", "threadId",
                                                 "turnId"),
            "item/tool/call": ("arguments", "callId", "threadId", "tool", "turnId"),
            "account/chatgptAuthTokens/refresh": ("reason",),
            "attestation/generate": (),
            "applyPatchApproval": ("callId", "conversationId", "fileChanges"),
            "execCommandApproval": ("callId", "command", "conversationId", "cwd", "parsedCmd"),
        },
        "ServerNotification": {
            "thread/started": ("thread",),
            "thread/status/changed": ("status", "threadId"),
            "thread/closed": ("threadId",),
            "thread/settings/updated": ("threadId", "threadSettings"),
            "turn/started": ("threadId", "turn"),
            "turn/completed": ("threadId", "turn"),
            "item/started": ("item", "startedAtMs", "threadId", "turnId"),
            "item/completed": ("completedAtMs", "item", "threadId", "turnId"),
            "item/fileChange/patchUpdated": ("changes", "itemId", "threadId", "turnId"),
            "item/commandExecution/terminalInteraction": ("itemId", "processId", "stdin", "threadId", "turnId"),
            "serverRequest/resolved": ("requestId", "threadId"),
            "hook/started": ("run", "threadId"),
            "hook/completed": ("run", "threadId"),
            "item/autoApprovalReview/started": ("action", "review", "reviewId", "startedAtMs", "threadId",
                                                "turnId"),
            "item/autoApprovalReview/completed": ("action", "completedAtMs", "decisionSource", "review", "reviewId",
                                                  "startedAtMs", "threadId", "turnId"),
            "error": ("error", "threadId", "turnId", "willRetry"),
            "item/agentMessage/delta": ("delta", "itemId", "threadId", "turnId"),
            "item/reasoning/textDelta": ("contentIndex", "delta", "itemId", "threadId", "turnId"),
            "item/reasoning/summaryTextDelta": ("delta", "itemId", "summaryIndex", "threadId", "turnId"),
            "item/reasoning/summaryPartAdded": ("itemId", "summaryIndex", "threadId", "turnId"),
            "item/commandExecution/outputDelta": ("delta", "itemId", "threadId", "turnId"),
            "item/fileChange/outputDelta": ("delta", "itemId", "threadId", "turnId"),
            "command/exec/outputDelta": ("capReached", "deltaBase64", "processId", "stream"),
            "thread/tokenUsage/updated": ("threadId", "tokenUsage", "turnId"),
            "account/rateLimits/updated": ("rateLimits",),
            "mcpServer/startupStatus/updated": ("name", "status"),
        },
    },
}

# The notifications the CH-D3 session relies on (rev 3 §4 / §8.2), per pinned version: each is mapped to an event or read
# as state. `thread/settings/updated` is in the table above but NOT here: it is experimental in the Codex source and
# never reaches a client that sent `experimentalApi: false`; one that arrives is an anomaly, not evidence. Adding a
# version means re-reading each method's `#[experimental]` mark in that tag's source, which the schema test cannot see.
RELIED_NOTIFICATIONS: Mapping[str, frozenset[str]] = {
    "0.162.1": frozenset((
        "thread/started", "thread/status/changed", "thread/closed", "turn/started", "turn/completed", "item/started",
        "item/completed", "item/fileChange/patchUpdated", "item/commandExecution/terminalInteraction",
        "serverRequest/resolved", "hook/started", "hook/completed", "item/autoApprovalReview/started",
        "item/autoApprovalReview/completed", "error")),
}

# rev 1 §3.2's list: the streaming deltas and the token / rate-limit / startup-status updates no event is made from
OPT_OUT_NOTIFICATIONS: tuple[str, ...] = (
    "item/agentMessage/delta", "item/reasoning/textDelta", "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded", "item/commandExecution/outputDelta", "item/fileChange/outputDelta",
    "command/exec/outputDelta", "thread/tokenUsage/updated", "account/rateLimits/updated",
    "mcpServer/startupStatus/updated",
)

# server methods whose pinned params REQUIRE a threadId: one that arrives without it is unparseable
_THREAD_REQUIRED: frozenset[str] = frozenset(
    m for table in ("ServerRequest", "ServerNotification")
    for m, req in PINNED_SURFACE[PINNED_VERSION][table].items() if "threadId" in req)


@dataclass(frozen=True)
class ServerRequestRow:
    """One server-request method the session knows (rev 2 §6.2 / §6.6). ``reads``: version -> the closed list of params
    fields the session reads (an approval with a field outside it fails closed, §6.2). The deny is ``reply`` (a
    schema-valid result) or, where the schema has no deny form, ``error`` (a JSON-RPC ``(code, message)``).
    ``response`` names the response definition ``reply`` must satisfy. ``replayable`` is unknown (None) until R-E."""

    method: str
    reads: Mapping[str, frozenset[str]]
    reply: Mapping[str, Any] | None
    error: tuple[int, str] | None
    requires_card: bool
    latches: bool
    response: str | None = None
    replayable: bool | None = None

    def deny(self) -> dict[str, Any]:
        """The deny as a frame body without its id: ``{"result": ...}`` or ``{"error": {...}}``."""
        if self.reply is not None:
            return {"result": json.loads(json.dumps(self.reply))}
        code, message = self.error or (-32601, "levain answers no such request")
        return {"error": {"code": code, "message": message}}


def _row(method: str, reads: tuple[str, ...], *, reply: Mapping[str, Any] | None = None,
         error: tuple[int, str] | None = None, card: bool = False, latches: bool = False,
         response: str | None = None) -> ServerRequestRow:
    return ServerRequestRow(method, {PINNED_VERSION: frozenset(reads)}, reply, error, card, latches, response)


_NO_METHOD = (-32601, "levain answers no such request in chat")
SERVER_REQUESTS: Mapping[str, ServerRequestRow] = {r.method: r for r in (
    _row("item/commandExecution/requestApproval",
         ("kind", "command", "cwd", "networkApprovalContext", "reason", "commandActions",
          "proposedExecpolicyAmendment", "proposedNetworkPolicyAmendments", "availableDecisions",
          "additionalPermissions", "approvalId", "environmentId", "itemId", "startedAtMs", "threadId", "turnId"),
         reply={"decision": "decline"}, card=True, response="CommandExecutionRequestApprovalResponse"),
    _row("item/fileChange/requestApproval", ("grantRoot", "itemId", "reason", "startedAtMs", "threadId", "turnId"),
         reply={"decision": "decline"}, card=True, response="FileChangeRequestApprovalResponse"),
    _row("item/permissions/requestApproval", ("cwd", "itemId", "permissions", "startedAtMs", "threadId", "turnId"),
         reply={"permissions": {}}, response="PermissionsRequestApprovalResponse"),
    _row("mcpServer/elicitation/request", ("serverName", "threadId"), reply={"action": "decline"},
         response="McpServerElicitationRequestResponse"),
    _row("item/tool/requestUserInput", ("isBlocking", "itemId", "questions", "threadId", "turnId"),
         error=(-32603, "levain answers no user-input request in chat")),
    _row("item/tool/call", ("arguments", "callId", "threadId", "tool", "turnId"),
         reply={"contentItems": [], "success": False}, latches=True, response="DynamicToolCallResponse"),
    _row("account/chatgptAuthTokens/refresh", ("reason",), error=_NO_METHOD, latches=True),
    _row("attestation/generate", (), error=_NO_METHOD, latches=True),
    _row("applyPatchApproval", ("callId", "conversationId", "fileChanges"), error=_NO_METHOD, latches=True),
    _row("execCommandApproval", ("callId", "command", "conversationId", "cwd", "parsedCmd"), error=_NO_METHOD,
         latches=True),
)}
CATCH_ALL = ServerRequestRow("*", {}, None, _NO_METHOD, False, True)


def server_request_row(method: str) -> ServerRequestRow:
    """The registry row for ``method``; an unknown method takes :data:`CATCH_ALL`."""
    return SERVER_REQUESTS.get(method, CATCH_ALL)


# --- the wire ----------------------------------------------------------------------------------------------------
class _NoId:
    def __repr__(self) -> str:
        return "NO_ID"


NO_ID: Any = _NoId()


@dataclass(frozen=True)
class Inbound:
    """One queued inbound message. ``kind``: ``notification``, ``request`` (answerable once, on its own ``gen``),
    ``foreign`` (names another thread: never answered), ``unparseable`` (``detail`` says why) or ``closed`` (the
    connection ended; the last item of a ``gen``). ``id`` is the JSON-RPC id exactly as sent, :data:`NO_ID` when
    absent. ``obs`` is the item's arrival ordinal on its ``gen``."""

    kind: str
    gen: str
    obs: int
    method: str | None = None
    id: Any = NO_ID
    params: Any = None
    thread_id: str | None = None
    raw_sha256: str = ""
    detail: str = ""

    @property
    def token(self) -> str:
        return f"{self.gen}:{self.obs}"


@dataclass
class _Waiter:
    method: str
    refuse: bool
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: CodexError | None = None


@dataclass
class _Boot:
    key: Any
    frames: list[tuple[str, str, Any, Any, str, str]] = field(default_factory=list)
    dropped: int = 0


class CodexWire:
    """One connection to a Codex app-server (see the module docstring). Build it with :meth:`open` (or
    :meth:`connect_socket` / :meth:`over_pipes`); read with :meth:`get` / :meth:`drain`; send with :meth:`call`,
    :meth:`start_thread`, :meth:`notify` and :meth:`reply`; end with :meth:`close`. ``on_inbound`` is called on the
    reader thread after each item is queued (a wake-up for the session); it must not :meth:`call` (that raises)."""

    def __init__(self, framing: Any, *, on_inbound: Callable[[Inbound], None] | None = None,
                 bootstrap_cap: int = BOOTSTRAP_FRAME_CAP) -> None:
        self._framing = framing
        self.gen = secrets.token_hex(8)
        self.init: dict[str, Any] = {}
        self.log_lines = 0
        self.callback_errors = 0
        self._log_sha = hashlib.sha256()
        self._on_inbound = on_inbound
        self._cap = bootstrap_cap
        self._wlock = threading.Lock()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._queue: deque[Inbound] = deque()
        self._obs = 0
        self._next_id = 0
        self._waiters: dict[Any, _Waiter] = {}
        self._late: deque[Any] = deque(maxlen=_LATE_IDS_CAP)
        self._pending: dict[str, Inbound] = {}
        self._thread_id: str | None = None
        self._boot: _Boot | None = None
        self._initialized = False
        self._closed: str | None = None
        self._reader = threading.Thread(target=self._run, name=f"codex-wire-{self.gen}", daemon=True)

    # -- construction --
    @classmethod
    def open(cls, framing: Any, *, client_version: str | None = None,
             opt_out: tuple[str, ...] = OPT_OUT_NOTIFICATIONS, timeout: float = _CALL_TIMEOUT,
             on_inbound: Callable[[Inbound], None] | None = None,
             bootstrap_cap: int = BOOTSTRAP_FRAME_CAP) -> CodexWire:
        """Start the reader on ``framing``, then ``initialize`` and ``initialized``. Any fault closes the framing and
        raises :class:`CodexError`."""
        if client_version is None:
            from levain import __version__ as client_version
        wire = cls(framing, on_inbound=on_inbound, bootstrap_cap=bootstrap_cap)
        if hasattr(framing, "settimeout"):
            framing.settimeout(None)        # the reader blocks between messages; deadlines are per call
        wire._reader.start()
        try:
            init = wire._call("initialize", {
                "clientInfo": {"name": "levain-chat", "title": "Levain chat", "version": client_version},
                "capabilities": {"experimentalApi": False, "optOutNotificationMethods": list(opt_out)},
            }, timeout, refuse=False, boot=False)
            wire._send({"method": "initialized", "params": {}})
            wire._initialized = True
        except BaseException:
            wire.close()
            raise
        wire.init = init if isinstance(init, dict) else {}
        return wire

    @classmethod
    def connect_socket(cls, path: Path, **kw: Any) -> CodexWire:
        try:
            framing = WsFraming(path)
        except OSError as exc:
            raise CodexError(f"the Codex daemon is not answering on {path} ({type(exc).__name__}: {exc})") from None
        return cls.open(framing, **kw)

    @classmethod
    def over_pipes(cls, rfile: BinaryIO, wfile: BinaryIO, **kw: Any) -> CodexWire:
        return cls.open(LineFraming(rfile, wfile), **kw)

    # -- state --
    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def closed(self) -> str | None:
        """Why the connection ended, or None while it is open."""
        return self._closed

    @property
    def log_digest(self) -> str:
        return self._log_sha.hexdigest()

    def bind_thread(self, thread_id: str) -> None:
        """Make ``thread_id`` this wire's thread (an attach, before ``thread/resume``). A wire has one thread."""
        with self._lock:
            if self._thread_id is not None and self._thread_id != thread_id:
                raise CodexError(f"this connection is already bound to thread {self._thread_id}")
            if self._boot is not None:
                raise CodexError("a thread/start is outstanding on this connection")
            self._thread_id = thread_id

    # -- reading --
    def get(self, timeout: float | None = None) -> Inbound | None:
        """The next queued item, waiting up to ``timeout`` seconds (None: until one arrives); None on timeout."""
        with self._cond:
            if not self._queue:
                self._cond.wait_for(lambda: bool(self._queue), timeout)
            return self._queue.popleft() if self._queue else None

    def drain(self) -> list[Inbound]:
        """Every queued item, in arrival order."""
        with self._lock:
            out = list(self._queue)
            self._queue.clear()
            return out

    # -- sending --
    def call(self, method: str, params: dict[str, Any], *, timeout: float = _CALL_TIMEOUT,
             on_server_request: str = "queue") -> Any:
        """Send one request and return its ``result``; an error response raises :class:`CodexError` (``.error`` holds
        it). Server traffic in between is queued as always; with ``on_server_request="refuse"`` a server request
        arriving while this call is outstanding (the wire's thread, or no thread) also makes the call raise
        :class:`CodexError` naming that method (the request stays queued, unanswered; the late response is
        dropped). Called from the reader thread, it raises at once."""
        if on_server_request not in ("queue", "refuse"):
            raise ValueError(f"on_server_request must be 'queue' or 'refuse', not {on_server_request!r}")
        if method == "thread/start":
            raise CodexError("thread/start goes through start_thread(), which binds the thread it starts")
        return self._call(method, params, timeout, refuse=on_server_request == "refuse", boot=False)

    def start_thread(self, params: dict[str, Any], *, timeout: float = _CALL_TIMEOUT) -> dict[str, Any]:
        """``thread/start`` with the bootstrap state (see the module docstring); returns its result, the wire then
        bound to the returned ``thread.id``. A result without one raises :class:`CodexError`."""
        with self._lock:
            if self._thread_id is not None:
                raise CodexError(f"this connection is already bound to thread {self._thread_id}")
        got = self._call("thread/start", params, timeout, refuse=False, boot=True)
        if self._thread_id is None:
            raise CodexError("thread/start answered without a thread id")
        return got

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._check_ready(method)
        self._send({"method": method, "params": params})

    def reply(self, item: Inbound, *, result: Any = NO_ID, error: tuple[int, str] | None = None) -> None:
        """Answer server request ``item`` once, with ``result`` or ``error`` ``(code, message)``, echoing its id as it
        came. Refused (:class:`CodexError`): an item that is not a request (a foreign thread's included), one from
        another ``gen``, one already answered, or a closed connection."""
        if (result is NO_ID) == (error is None):
            raise ValueError("reply takes exactly one of result= or error=")
        with self._lock:
            if item.kind == "foreign":
                raise CodexError(f"{item.method}: a request on thread {item.thread_id} is never answered here")
            if item.kind != "request":
                raise CodexError(f"item {item.token} is a {item.kind}, not a request")
            if item.gen != self.gen:
                raise CodexError(f"{item.method}: the request is from a dead connection ({item.gen})")
            if self._closed is not None:
                raise CodexError(f"{item.method}: the connection is closed ({self._closed})")
            if self._pending.pop(item.token, None) is None:
                raise CodexError(f"{item.method}: request {item.token} was already answered")
        body: dict[str, Any] = {"id": item.id}
        if error is None:
            body["result"] = result
        else:
            body["error"] = {"code": error[0], "message": error[1]}
        self._send(body)

    def close(self) -> None:
        """Close the connection; calls still waiting raise :class:`CodexError`."""
        with self._lock:
            if self._closed is None:
                self._closed = "closed by levain"
                self._fail_waiters_locked("the connection was closed by levain")
        held = self._wlock.acquire(timeout=1.0)
        try:
            self._framing.shutdown()
        finally:
            if held:
                self._wlock.release()
        if self._reader.is_alive() and self._reader is not threading.current_thread():
            self._reader.join(2.0)
        if not self._reader.is_alive():
            # a pipe whose peer never closes its output keeps the reader blocked in readline; closing the file
            # under it would block here too, so it is left to the process that owns the peer
            with contextlib.suppress(OSError, ValueError):
                self._framing.close()

    # -- internals: sending --
    def _check_ready(self, method: str) -> None:
        if method != "initialize" and not self._initialized:
            raise CodexError(f"{method}: nothing but initialize goes out before initialized")

    def _send(self, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        try:
            with self._wlock:
                self._framing.send_text(data)
        except (OSError, ValueError) as exc:
            raise CodexError(f"the Codex connection failed ({type(exc).__name__}: {exc})") from None

    def _call(self, method: str, params: dict[str, Any], timeout: float, *, refuse: bool, boot: bool) -> Any:
        if self._on_reader_thread():
            raise CodexError(f"{method}: called from the wire's reader thread, the one thread that could read its "
                             "answer")
        self._check_ready(method)
        with self._lock:
            if self._closed is not None:
                raise CodexError(f"{method}: the connection is closed ({self._closed})")
            rid = self._next_id
            self._next_id += 1
            key = self._id_key(rid)
            waiter = _Waiter(method, refuse)
            self._waiters[key] = waiter
            if boot:
                self._boot = _Boot(key)
        try:
            self._send({"id": rid, "method": method, "params": params})
        except CodexError:
            self._abandon(key, boot)
            raise
        if not waiter.event.wait(timeout):
            if self._abandon(key, boot):
                raise CodexError(f"the daemon did not answer {method} in {timeout:g}s")
            waiter.event.wait()             # answered as the deadline passed: the reader is setting it
        if waiter.error is not None:
            raise waiter.error
        m = waiter.response or {}
        if "error" in m:
            err = m["error"]
            text = err.get("message") if isinstance(err, dict) else err
            raise CodexError(f"{method}: {str(text)[:300]}", err)
        return m.get("result")

    def _abandon(self, key: Any, boot: bool) -> bool:
        """Drop a call nobody answered; True when it was still waiting (False: its answer came in meanwhile)."""
        fresh: list[Inbound] = []
        with self._lock:
            if self._waiters.pop(key, None) is None:
                return False
            self._late.append(key)
            if boot and self._boot is not None and self._boot.key == key:
                fresh = self._end_boot_locked(None)
        self._after(fresh)
        return True

    # -- internals: reading (the reader thread) --
    def _run(self) -> None:
        reason = "the connection ended"
        try:
            while True:
                try:
                    raw = self._framing.recv_message(skip_oversize=True)
                except _Oversize as exc:
                    self._push(self._item("unparseable",
                                          detail=f"a message of {exc.size} bytes, over {_MAX_MESSAGE}"))
                    continue
                self._dispatch(raw)
        except (CodexError, OSError, ValueError) as exc:
            reason = str(exc) or type(exc).__name__
        fresh: list[Inbound] = []
        with self._lock:
            if self._closed is None:
                self._closed = reason
            if self._boot is not None:
                fresh += self._end_boot_locked(None)
            self._fail_waiters_locked(f"the connection ended ({self._closed})")
            fresh.append(self._enqueue_new_locked("closed", detail=self._closed))
        self._after(fresh)

    def _dispatch(self, raw: bytes) -> None:
        sha = hashlib.sha256(raw).hexdigest()
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if self._framing.kind == "line":
                with self._lock:
                    self.log_lines += 1
                    self._log_sha.update(hashlib.sha256(raw).digest())
                return
            self._push(self._item("unparseable", raw_sha256=sha, detail="a message that is not JSON"))
            return
        if not isinstance(msg, dict):
            self._push(self._item("unparseable", raw_sha256=sha, detail="a message that is not a JSON object"))
            return
        has_id = self._has_id(msg)
        rid = msg.get("id", NO_ID)
        if "method" not in msg:
            if has_id and self._usable(rid):
                self._deliver(msg, sha)
            else:
                self._push(self._item("unparseable", raw_sha256=sha,
                                      detail="a message with neither a method nor a usable id"))
            return
        method = msg["method"]
        if not isinstance(method, str):
            self._push(self._item("unparseable", raw_sha256=sha, detail="a method that is not a string"))
            return
        if has_id and not self._usable(rid):
            self._push(self._item("unparseable", method=method, raw_sha256=sha,
                                  detail="a server request with no usable id"))
            return
        kind = "request" if has_id else "notification"
        params = msg.get("params")
        tid, why = self._thread_of(method, params)
        if why:
            self._push(self._item("unparseable", method=method, id=self._wire_id(rid) if has_id else NO_ID,
                                  raw_sha256=sha, detail=why))
            return
        rid = self._wire_id(rid) if has_id else NO_ID
        fresh: list[Inbound] = []
        with self._lock:
            if tid is None or tid == self._thread_id:
                fresh.append(self._enqueue_new_locked(kind, method=method, id=rid, params=params, thread_id=tid,
                                                      raw_sha256=sha))
            elif self._boot is not None and self._thread_id is None:
                if len(self._boot.frames) < self._cap:
                    self._boot.frames.append((kind, method, rid, params, tid, sha))
                else:
                    self._boot.dropped += 1
            else:
                fresh.append(self._enqueue_new_locked("foreign", method=method, id=rid, params=params,
                                                      thread_id=tid, raw_sha256=sha))
        self._after(fresh)

    def _deliver(self, msg: dict[str, Any], sha: str) -> None:
        key = self._id_key(msg["id"])
        fresh: list[Inbound] = []
        with self._lock:
            waiter = self._waiters.pop(key, None)
            if waiter is None:
                if key not in self._late:
                    fresh.append(self._enqueue_new_locked("unparseable", raw_sha256=sha,
                                                          detail="a response no call is waiting on"))
            elif self._boot is not None and self._boot.key == key:
                result = msg.get("result")
                thread = result.get("thread") if isinstance(result, dict) else None
                tid = thread.get("id") if isinstance(thread, dict) else None
                fresh = self._end_boot_locked(tid if isinstance(tid, str) and "error" not in msg else None)
        self._after(fresh)
        if waiter is not None:
            waiter.response = msg
            waiter.event.set()

    def _end_boot_locked(self, thread_id: str | None) -> list[Inbound]:
        """Leave the bootstrap state: bind ``thread_id`` (None: the start failed) and release the buffered frames in
        arrival order, the bound thread's as themselves and every other as ``foreign``."""
        boot, self._boot = self._boot, None
        if thread_id is not None:
            self._thread_id = thread_id
        out: list[Inbound] = []
        for kind, method, rid, params, tid, sha in boot.frames if boot else ():
            out.append(self._enqueue_new_locked(kind if tid == self._thread_id else "foreign", method=method, id=rid,
                                                params=params, thread_id=tid, raw_sha256=sha))
        if boot and boot.dropped:
            out.append(self._enqueue_new_locked(
                "unparseable", detail=f"{boot.dropped} thread-scoped message(s) past the bootstrap cap of {self._cap} "
                                      "were dropped before thread/start answered"))
        return out

    def _thread_of(self, method: str, params: Any) -> tuple[str | None, str]:
        """``(thread id or None, why unparseable or "")``."""
        if method == "thread/started":
            thread = params.get("thread") if isinstance(params, dict) else None
            tid = thread.get("id") if isinstance(thread, dict) else None
            return (tid, "") if isinstance(tid, str) else (None, "thread/started without a thread id")
        tid = params.get("threadId", NO_ID) if isinstance(params, dict) else NO_ID
        if tid is NO_ID:
            return (None, f"{method} without the threadId its schema requires") if method in _THREAD_REQUIRED \
                else (None, "")
        return (tid, "") if isinstance(tid, str) else (None, f"{method} with a threadId that is not a string")

    def _item(self, kind: str, **kw: Any) -> dict[str, Any]:
        return {"kind": kind, **kw}

    def _push(self, spec: dict[str, Any]) -> None:
        with self._lock:
            got = self._enqueue_new_locked(spec.pop("kind"), **spec)
        self._after([got])

    def _enqueue_new_locked(self, kind: str, **kw: Any) -> Inbound:
        self._obs += 1
        item = Inbound(kind, self.gen, self._obs, **kw)
        if kind == "request":
            self._pending[item.token] = item
            for key, w in list(self._waiters.items()):
                if w.refuse:
                    del self._waiters[key]
                    self._late.append(key)
                    w.error = CodexError(f"{w.method}: the daemon sent the server request {item.method} while it "
                                         "was outstanding")
                    w.event.set()
        self._enqueue_locked(item)
        self._cond.notify_all()
        return item

    def _after(self, items: list[Inbound]) -> None:
        if self._on_inbound is None:
            return
        for item in items:
            try:
                self._on_inbound(item)
            except Exception:  # noqa: BLE001 - a session callback fault never stops the reader; it is counted
                with self._lock:
                    self.callback_errors += 1

    def _fail_waiters_locked(self, why: str) -> None:
        for key, w in list(self._waiters.items()):
            w.error = CodexError(f"{w.method}: {why}")
            w.event.set()
            self._late.append(key)
        self._waiters.clear()

    # -- the behaviours the S1 tests break on purpose (overridden only there) --
    def _on_reader_thread(self) -> bool:
        return threading.current_thread() is self._reader

    def _enqueue_locked(self, item: Inbound) -> None:
        self._queue.append(item)

    @staticmethod
    def _has_id(msg: dict[str, Any]) -> bool:
        return "id" in msg

    @staticmethod
    def _usable(rid: Any) -> bool:
        return type(rid) is int or type(rid) is str

    @staticmethod
    def _id_key(rid: Any) -> tuple[str, Any]:
        return (type(rid).__name__, rid)

    @staticmethod
    def _wire_id(rid: Any) -> Any:
        return rid
