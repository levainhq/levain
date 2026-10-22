"""keys' upstream client (S1 session 3b, design 1.2 and 1.6).

Every byte keys sends upstream for an exchange goes through that exchange's `Upstream.write`, which checks the
exchange's `detached` flag SYNCHRONOUSLY and then calls the TLS writer's `write`, with no `await` between them. A phase
1 sets `detached` under `state_lock` on keys' one loop, so no await anywhere in an exchange can carry a revoked
exchange's credential upstream (the fencing token checked at the resource on every write).

The raw writer and the plaintext writer under it are private to `Upstream`: no method, property or return value hands
either out. There are exactly two ways to an `Upstream`:
`open_exchange` (an exchange is mandatory) and `refresh_post` (the maintenance form, local to that function, used for
the one refresh request it was given).

The path to a vendor: `pick_address` resolves the route's host ONCE and passes every answer through the front's
`refused_address` (all or nothing); `_connect` opens plain TCP to that address literal; `_handshake` runs TLS on that
connection with `loop.start_tls` (the one construction the root interpreter supports: 3.9 has no
`StreamWriter.start_tls`), the route's host as SNI and for the hostname check. The response is read with an h11
client: `read_head` consumes every informational response but 101 (bounded), `read_body` yields the body.

Ownership: whoever holds an `Upstream`, or a transport no `Upstream` holds yet, releases it in a `finally` (or, where
ownership passes on success, an `except BaseException`) with `abort()`, which is idempotent, best-effort and never
raises an `Exception`.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import ssl
from typing import Any, List, NamedTuple, Optional, Tuple

import h11

from levain.broker.norm import Refused

logger = logging.getLogger("levain.broker")

#: the graceful close's bound: on 3.9 `SSLProtocol` has no shutdown timeout of its own, so a peer that never answers
#: `close_notify` would otherwise hold an exchange in its registration's exchange set.
GRACE = 2.0
#: the connect and the TLS handshake together (DECISION, judged, design 1.6)
CONNECT_TIMEOUT_S = 10.0
#: from the request's last byte to the FINAL response head, one absolute deadline (DECISION, judged: a long first token)
HEAD_TIMEOUT_S = 300.0
#: after the final head, the longest wait for the next response bytes (DECISION, judged, 3a's IDLE_S rule)
IDLE_S = 600.0
#: the TLS reader's buffer limit: past it the reader pauses its transport, so a slow front bounds keys' memory
LIMIT = 64 * 1024
#: one read from the upstream
READ_CHUNK = 64 * 1024
#: informational responses consumed before the final one; one more is `upstream-error` (DECISION, judged, r2 W10)
MAX_INFORMATIONAL = 16
#: the refresh response's body, read whole (a token endpoint's JSON is far below it)
REFRESH_BODY_CAP = 64 * 1024


def _note(level: int, msg: str, *a: object) -> None:
    """The ONE failure-note helper. It never raises an `Exception` into its caller: a full disk, a closed fd, or a
    handler whose own `emit` raises (`Handler.handle()` does not guard `emit`) is swallowed here. A `BaseException`
    still propagates."""
    try:
        logger.log(level, msg, *a)
    except Exception:
        pass


class StaleExchange(Exception):
    """The exchange was detached (dropped, preempted, replaced, closed): nothing more goes upstream for it. An ordinary
    exception, so the exchange's own `finally` (its release) still runs."""


#: every `BadResponse.detail`, each a fixed token: the detail is logged (gw.jsonl, stderr), and a text built from the
#: response (h11's refusal quotes the offending bytes) could carry an echoed credential into the log unscanned.
BAD_RESPONSE_DETAILS = frozenset({"protocol", "unsolicited-upgrade", "informational", "unexpected-event",
                                  "refresh-body-cap"})


class BadResponse(EOFError):
    """The upstream's response is one keys does not relay (h11 refused it, an unsolicited 101, a run of informational
    responses past MAX_INFORMATIONAL, a refresh body past its cap). `detail` names which, for the note only, and is
    always one of BAD_RESPONSE_DETAILS (anything else becomes "protocol"), never text derived from the response. An
    EOFError, so keys answers it `upstream-error` like any other upstream failure."""

    def __init__(self, detail: str) -> None:
        detail = detail if detail in BAD_RESPONSE_DETAILS else "protocol"
        super().__init__(detail)
        self.detail = detail


class RefreshRequest(NamedTuple):
    """The one request `refresh_post` sends: the address already validated, the host for SNI and the hostname check,
    and the request's bytes as the credential layer built them."""

    addr: str
    port: int
    host: str
    head: bytes
    body: bytes


class RefreshResponse(NamedTuple):
    status: int
    headers: List[Tuple[bytes, bytes]]
    body: bytes


class Upstream:
    """One upstream connection: the TLS reader and writer, and the plaintext writer under them held only so it is not
    collected (on 3.12 a dropped `StreamWriter` closes its transport, which the TLS layer rides on). Never written
    through, never read, never handed out.

    The exchange form gates every write on its exchange. The maintenance form (`Upstream.maintenance`) has no exchange
    and carries only the refresh request; its one constructor is `refresh_post`."""

    __slots__ = ("_reader", "_writer", "_keepalive", "_exchange")

    def __init__(self, reader: Any, writer: Any, exchange: Any, *, _keepalive: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._keepalive = _keepalive
        self._exchange = exchange
        if exchange is None:
            # The constructor owns what it was handed: both transports are aborted before the one named raise.
            self.abort()
            raise ValueError("an exchange Upstream is built for an exchange; the refresh uses Upstream.maintenance")

    @classmethod
    def maintenance(cls, reader: Any, writer: Any, *, _keepalive: Any) -> "Upstream":
        """The ungated form, for the refresh request alone. It holds no exchange and no session credential."""
        up = cls.__new__(cls)
        up._reader = reader
        up._writer = writer
        up._keepalive = _keepalive
        up._exchange = None
        return up

    def write(self, data: bytes) -> None:
        """THE gate. A detached exchange's write raises `StaleExchange` and sends nothing; the check and the transport
        write are one synchronous step, so no phase 1 can fall between them."""
        ex = self._exchange
        if ex is not None and ex.detached:
            raise StaleExchange()
        self._writer.write(data)

    async def drain(self) -> None:
        """Flow control after a `write`; it sends no new bytes, so it is not gated."""
        await self._writer.drain()

    async def read(self, n: int) -> bytes:
        """Up to `n` response bytes (b"" at EOF). Reading sends nothing, so it is not gated."""
        return await self._reader.read(n)

    def abort(self) -> bool:
        """Abort the TLS transport (which also closes the plaintext one under it), then the plaintext transport itself,
        each attempted whatever the other did. Idempotent; never raises an `Exception`. Returns True iff both aborts
        completed without one on THIS call."""
        ok = True
        try:
            self._writer.transport.abort()
        except Exception as e:
            ok = False
            _note(logging.WARNING, "levain gw-keys: upstream abort failed (tls: %s)", type(e).__name__)
        try:
            self._keepalive.transport.abort()
        except Exception as e:
            ok = False
            _note(logging.WARNING, "levain gw-keys: upstream abort failed (plain: %s)", type(e).__name__)
        return ok

    def _graceful_close(self) -> None:
        """Private: reached only from an exchange's release and from `refresh_post`. A detached exchange is never
        closed gracefully, since a close would flush what `abort()` discards."""
        self._writer.close()

    async def wait_closed(self) -> None:
        await self._writer.wait_closed()


# --- the path to the vendor -------------------------------------------------------------------------------------

async def _resolve(host: str, port: int) -> List[str]:
    """The system resolver, as the front uses it (an absolute name, bounded threads)."""
    from levain.broker.front import system_resolve
    return await system_resolve(host, port)


async def pick_address(host: str, port: int) -> str:
    """Resolve `host` ONCE and return the address to connect to. Every answer passes the front's `refused_address`
    or the whole lookup is `address-refused` (403): one refused answer refuses them all, as the front's CONNECT."""
    from levain.broker.front import refused_address
    addrs = list(dict.fromkeys(await _resolve(host, port)))
    if not addrs:
        raise Refused("address-refused", "no answer")
    bad = [(a, why) for a, why in ((a, refused_address(a)) for a in addrs) if why]
    if bad:
        _note(logging.WARNING, "levain gw-keys: address-refused host=%s %s", host,
              "; ".join(f"{a} {w}" for a, w in bad))
        raise Refused("address-refused", "refused answer")
    return addrs[0]


_CTX: Optional[ssl.SSLContext] = None


def _tls_context() -> ssl.SSLContext:
    """THE client context, made once: the platform default (system CAs, hostname check, CERT_REQUIRED), TLS >= 1.2."""
    global _CTX
    if _CTX is None:
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        _CTX = ctx
    return _CTX


async def _connect(addr: str, port: int) -> Tuple[Any, Any]:
    """The plain-TCP seam: the one place an upstream connection is opened, to an address LITERAL (a name here would be
    a second, unchecked resolution, so one is refused before any socket exists)."""
    ipaddress.ip_address(addr)
    return await asyncio.open_connection(addr, port, limit=LIMIT)


async def _handshake(addr: str, port: int, host: str) -> Tuple[Any, Any, Any]:
    """TLS on `_connect`'s connection, returning `(reader, writer, plain_writer)`; called ONLY by `open_exchange` and
    `refresh_post`. One `try` spans the whole ownership interval, from the transport `_connect` returns to the return
    the constructor consumes, and aborts whatever transport it holds on ANY exit by exception (3.9's `start_tls` closes
    the transport only around its own handshake wait, and owns nothing after it returns)."""
    plain_reader, plain_writer = await _connect(addr, port)
    tls = None
    try:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=LIMIT)   # made inside the running loop (3.9 binds it)
        proto = asyncio.StreamReaderProtocol(reader)
        tls = await loop.start_tls(plain_writer.transport, proto, _tls_context(), server_hostname=host,
                                   ssl_handshake_timeout=CONNECT_TIMEOUT_S)
        proto.connection_made(tls)   # 3.9's start_tls never calls it: without it the reader has no transport to pause
        writer = asyncio.StreamWriter(tls, proto, reader, loop)
    except BaseException:
        if tls is not None:
            tls.abort()
        plain_writer.transport.abort()
        raise
    del plain_reader   # nothing reads the plain stream; the TLS protocol owns the socket now
    return reader, writer, plain_writer


async def _await_handshake(task: "asyncio.Future", timeout: float) -> Tuple[Any, Any, Any]:
    """A `_handshake` task's result, bounded by `timeout` (asyncio.TimeoutError past it), for the refresh, which has
    no exchange to store a late result on. Unlike 3.9's `wait_for`, a handshake that completes as the wait ends (the
    deadline, or a cancel of the caller) is not dropped: its transports are aborted here before the exit."""
    try:
        await asyncio.wait({task}, timeout=timeout)
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _abort_handshake(task)
        raise
    if not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if _abort_handshake(task) or task.cancelled():
            raise asyncio.TimeoutError()
    return task.result()


def _abort_handshake(task: "asyncio.Future") -> bool:
    """Abort the transports of a `_handshake` that completed; True iff there was one. Never raises an `Exception`."""
    if not task.done() or task.cancelled() or task.exception() is not None:
        return False
    _reader, writer, plain_writer = task.result()
    for tr in (writer.transport, plain_writer.transport):
        try:
            tr.abort()
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: upstream abort failed (%s)", type(e).__name__)
    return True


async def open_exchange(addr: str, port: int, host: str, exchange: Any) -> Upstream:
    """The only way to an exchange's `Upstream`. The exchange is mandatory and holds ONE: a second call for it is
    refused (the new `Upstream` aborted, the one it holds kept, so no transport is left in no registry). The `Upstream`
    is stored on the exchange with no await between its construction and the store, so a phase 1 either finds it (and
    phase 2 aborts it) or ran before it existed (and this function sees `detached`, aborts it and clears the store)."""
    reader, writer, plain_writer = await _handshake(addr, port, host)
    up = Upstream(reader, writer, exchange, _keepalive=plain_writer)
    try:
        if exchange.upstream is not None:
            raise ValueError("this exchange holds an Upstream already")
        exchange.upstream = up
        if exchange.detached:
            raise StaleExchange()
    except BaseException:
        up.abort()   # never raises, so the propagating exception is the original
        if exchange.upstream is up:
            exchange.upstream = None
        raise
    return up


# --- the response (h11 client) ----------------------------------------------------------------------------------

async def _next_event(up: Upstream, conn: "h11.Connection", wait: Optional[float]) -> Any:
    """The next h11 event, reading from `up` while h11 needs data; each read waits at most `wait` (a timeout raises
    asyncio.TimeoutError; None waits as long as the caller does). h11's refusal of the bytes is BadResponse, its text
    dropped (it quotes the bytes)."""
    loop = asyncio.get_running_loop()
    deadline = None if wait is None else loop.time() + wait
    while True:
        try:
            ev = conn.next_event()
        except h11.RemoteProtocolError:
            raise BadResponse("protocol") from None
        if ev is not h11.NEED_DATA:
            return ev
        left = None if deadline is None else max(0.0, deadline - loop.time())
        data = await asyncio.wait_for(up.read(READ_CHUNK), left)
        try:
            conn.receive_data(data)
        except h11.RemoteProtocolError:
            raise BadResponse("protocol") from None


async def read_head(up: Upstream, conn: "h11.Connection", deadline: Optional[float]) -> "h11.Response":
    """The FINAL response head, by the absolute `deadline` (loop time) fixed when the request's last byte was sent: an
    informational response never extends it. None: no deadline here, the caller bounds the wait (keys reads the head
    while the body is still going up, and fixes the deadline when its last byte is sent). Every 1xx but 101 is
    consumed; more than MAX_INFORMATIONAL of them, or a 101 (keys asks for no upgrade at this tip), is BadResponse."""
    loop = asyncio.get_running_loop()
    seen = 0
    while True:
        ev = await _next_event(up, conn, None if deadline is None else max(0.0, deadline - loop.time()))
        if isinstance(ev, h11.InformationalResponse):
            if ev.status_code == 101:
                raise BadResponse("unsolicited-upgrade")
            seen += 1
            if seen > MAX_INFORMATIONAL:
                raise BadResponse("informational")
            continue
        if isinstance(ev, h11.Response):
            return ev
        raise BadResponse("unexpected-event")


async def read_body(up: Upstream, conn: "h11.Connection", idle_s: float) -> Optional[bytes]:
    """The next body bytes, or None at the end of the response (its trailers, if any, are dropped). Each wait for
    bytes is bounded by `idle_s`."""
    while True:
        ev = await _next_event(up, conn, idle_s)
        if isinstance(ev, h11.Data):
            if ev.data:
                return bytes(ev.data)
            continue
        if isinstance(ev, h11.EndOfMessage):
            return None
        raise BadResponse("unexpected-event")


def h11_bytes(conn: "h11.Connection", event: Any) -> bytes:
    """The bytes h11 writes for `event` on `conn`. h11's serializer does no I/O (the caller sends what it returns
    through `Upstream.write`); every serialization in keys goes through this one function, so the networking
    inventory names its `send` once."""
    return conn.send(event) or b""


def client_after_request(method: bytes) -> "h11.Connection":
    """An h11 client connection placed after a request of `method` with no body, so it parses that request's
    response (a HEAD's response has no body). For the refresh, whose request bytes the credential layer built."""
    conn = h11.Connection(our_role=h11.CLIENT)
    for ev in (h11.Request(method=method, target=b"/", headers=[(b"host", b"refresh")]), h11.EndOfMessage()):
        h11_bytes(conn, ev)
    return conn


async def _read_response(up: Upstream) -> RefreshResponse:
    """Read the refresh's one response through `up`: its head by HEAD_TIMEOUT_S, its body by IDLE_S per read, the body
    at most REFRESH_BODY_CAP."""
    conn = client_after_request(b"POST")
    loop = asyncio.get_running_loop()
    head = await read_head(up, conn, loop.time() + HEAD_TIMEOUT_S)
    body = b""
    while True:
        part = await read_body(up, conn, IDLE_S)
        if part is None:
            break
        body += part
        if len(body) > REFRESH_BODY_CAP:
            raise BadResponse("refresh-body-cap")
    return RefreshResponse(head.status_code, [(bytes(n), bytes(v)) for n, v in head.headers], body)


async def refresh_post(request: RefreshRequest) -> Any:
    """The refresh's entry point: build the maintenance `Upstream` here, send THIS request, read its response, close,
    and return the response only. The `Upstream` never leaves this function; every exit aborts it. The connect and the
    TLS handshake share one CONNECT_TIMEOUT_S deadline (a cancel inside `_handshake` aborts what it holds)."""
    reader, writer, plain_writer = await _await_handshake(
        asyncio.ensure_future(_handshake(request.addr, request.port, request.host)), CONNECT_TIMEOUT_S)
    up = Upstream.maintenance(reader, writer, _keepalive=plain_writer)
    try:
        up.write(request.head)
        if request.body:
            up.write(request.body)
        await up.drain()
        response = await _read_response(up)
        up._graceful_close()
        try:
            await asyncio.wait_for(up.wait_closed(), GRACE)
        except Exception:
            _note(logging.DEBUG, "levain gw-keys: refresh graceful close ended early")
    finally:
        if not up.abort():
            _note(logging.WARNING, "levain gw-keys: dropped an unclosed transport")
    return response
