"""keys' upstream client: the capability SHELL (S1 session 3b, design 1.2).

Every byte keys sends upstream for an exchange goes through that exchange's `Upstream.write`, which checks the
exchange's `detached` flag SYNCHRONOUSLY and then calls the TLS writer's `write`, with no `await` between them. A phase
1 sets `detached` under `state_lock` on keys' one loop, so no await anywhere in an exchange can carry a revoked
exchange's credential upstream (the fencing token checked at the resource on every write).

The raw writer and the plaintext writer under it are private to `Upstream`: no method, property or return value hands
either out. There are exactly two ways to an `Upstream`:
`open_exchange` (an exchange is mandatory) and `refresh_post` (the maintenance form, local to that function, used for
the one refresh request it was given).

This tip is the shell. `_connect` (plain TCP to a validated address) and `_handshake` (TLS on that connection) refuse
until the real client is built; a test fixture replaces `_handshake` with one returning fake `(reader, writer,
plain_writer)` objects. Reading an upstream response is likewise not built here.

Ownership: whoever holds an `Upstream`, or a transport no `Upstream` holds yet, releases it in a `finally` (or, where
ownership passes on success, an `except BaseException`) with `abort()`, which is idempotent, best-effort and never
raises an `Exception`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, NamedTuple, Tuple

logger = logging.getLogger("levain.broker")

#: the graceful close's bound: on 3.9 `SSLProtocol` has no shutdown timeout of its own, so a peer that never answers
#: `close_notify` would otherwise hold an exchange in its registration's exchange set.
GRACE = 2.0


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


class RefreshRequest(NamedTuple):
    """The one request `refresh_post` sends: the address already validated, the host for SNI and the hostname check,
    and the request's bytes as the credential layer built them."""

    addr: str
    port: int
    host: str
    head: bytes
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


async def _connect(addr: str, port: int) -> Tuple[Any, Any]:
    """The plain-TCP seam: the one place an upstream connection is opened. Not built at this tip."""
    raise NotImplementedError("upstream._connect: the plain-TCP connect is not built yet (session 3b-4)")


async def _handshake(addr: str, port: int, host: str) -> Tuple[Any, Any, Any]:
    """TLS on `_connect`'s connection, returning `(reader, writer, plain_writer)`; called ONLY by `open_exchange` and
    `refresh_post`. Not built at this tip: a test fixture replaces it."""
    raise NotImplementedError("upstream._handshake: the TLS handshake is not built yet (session 3b-4)")


async def _read_response(up: Upstream) -> Any:
    """Read one upstream response through `up`. Not built at this tip (the h11 client)."""
    raise NotImplementedError("upstream._read_response: the response reader is not built yet (session 3b-4)")


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


async def refresh_post(request: RefreshRequest) -> Any:
    """The refresh's entry point: build the maintenance `Upstream` here, send THIS request, read its response, close,
    and return the response only. The `Upstream` never leaves this function; every exit aborts it."""
    reader, writer, plain_writer = await _handshake(request.addr, request.port, request.host)
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

