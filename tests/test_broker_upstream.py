"""S1 session 3b-4: `upstream._handshake`'s TLS construction keeps a paused reader's bytes when the connection is lost.

Reproduced (3b-4 session 2, item 3): an 8 MiB response relayed to a slow slot lost its tail under the root interpreter
(3.9.6). The upstream's close_notify landed while keys' reader was paused; 3.9's TLS transport resumes through its
socket transport, which is None once the connection is lost, so `resume_reading` raised AttributeError, and
`StreamReader.read` calls it only AFTER taking the bytes out of its buffer. 3.12's TLS transport does not raise there,
so these cases emulate 3.9's transport and run on every interpreter; the real-TLS reproduction on both is the lane's
evidence (project_memory/1022-8/3b4/repro_paused_eof*)."""
from __future__ import annotations

import asyncio
import socket

import pytest

from levain.broker import upstream as U

BODY = bytes(i % 251 for i in range(300_000))   # well past 2 x LIMIT: the reader pauses


class _SocketTransport:
    def __init__(self) -> None:
        self.reading = True

    def pause_reading(self) -> None:
        self.reading = False

    def resume_reading(self) -> None:
        self.reading = True


class Lost39TLS(asyncio.Transport):
    """A TLS transport as 3.9's sslproto behaves: reading is paused and resumed through the socket transport under it,
    which is None once the connection is lost (`lose()`)."""

    def __init__(self) -> None:
        super().__init__()
        self.sock: _SocketTransport | None = _SocketTransport()
        self.aborted = False

    def pause_reading(self) -> None:
        self.sock.pause_reading()

    def resume_reading(self) -> None:
        self.sock.resume_reading()

    def lose(self) -> None:
        self.sock = None

    def is_closing(self) -> bool:
        return self.aborted

    def get_extra_info(self, name, default=None):
        return default

    def write(self, data) -> None:
        pass

    def abort(self) -> None:
        self.aborted = True


async def _drain_all(read) -> tuple[bytes, BaseException | None]:
    got = b""
    try:
        while True:
            data = await read(65536)
            if not data:
                return got, None
            got += data
    except Exception as e:   # noqa: BLE001 - the negative control's raise is the observation
        return got, e


def _deliver_then_lose(proto: asyncio.StreamReaderProtocol, tls: Lost39TLS) -> None:
    """The body arrives (the reader pauses), then the connection is lost while it is paused."""
    proto.data_received(BODY)
    tls.lose()
    proto.connection_lost(None)


def test_negative_control_unguarded_transport_loses_bytes():
    """Without the guard, the emulated 3.9 transport raises out of `read` with bytes already gone: the hazard."""
    async def run():
        reader = asyncio.StreamReader(limit=U.LIMIT)
        proto = asyncio.StreamReaderProtocol(reader)
        tls = Lost39TLS()
        proto.connection_made(tls)
        _deliver_then_lose(proto, tls)
        assert tls.sock is None
        return await _drain_all(reader.read)

    got, err = asyncio.run(run())
    assert isinstance(err, AttributeError)
    assert len(got) < len(BODY)


def test_handshake_keeps_every_byte_after_a_loss_while_paused(monkeypatch):
    """Through the real `_handshake` (its `start_tls` answering the emulated 3.9 transport, `_connect` a socketpair):
    the reader still holds the TLS transport itself (row 24), pauses it, and after the loss every byte is read and the
    end is a clean EOF."""
    a, b = socket.socketpair()

    async def run():
        loop = asyncio.get_running_loop()
        made: list[Lost39TLS] = []

        async def connect(addr, port):
            return await asyncio.open_connection(sock=a)

        async def start_tls(transport, protocol, ctx, *, server_hostname=None, ssl_handshake_timeout=None):
            made.append(Lost39TLS())
            return made[0]

        monkeypatch.setattr(U, "_connect", connect)
        monkeypatch.setattr(loop, "start_tls", start_tls, raising=False)
        reader, writer, plain = await U._handshake("127.0.0.1", 443, "upstream.example")
        tls = made[0]
        assert reader._transport is writer.transport is tls
        up = U.Upstream.maintenance(reader, writer, _keepalive=plain)
        _deliver_then_lose(writer._protocol, tls)
        assert tls.sock is None and reader._paused
        got, err = await _drain_all(up.read)
        up.abort()
        return got, err

    try:
        got, err = asyncio.run(run())
    finally:
        b.close()
    assert err is None
    assert got == BODY


@pytest.mark.parametrize("raises", [None, AttributeError])
def test_resume_or_lost(raises):
    """A live transport's resume runs; a lost one's AttributeError is absorbed (nothing to resume)."""
    calls = []

    def resume():
        calls.append(1)
        if raises is not None:
            raise raises("'NoneType' object has no attribute 'resume_reading'")

    U._resume_or_lost(resume)()
    assert calls == [1]


def test_resume_or_lost_passes_other_errors():
    def resume():
        raise RuntimeError("not a lost connection")

    with pytest.raises(RuntimeError):
        U._resume_or_lost(resume)()
