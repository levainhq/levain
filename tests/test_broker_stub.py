"""The S1 gateway stub (contract §4.8): it answers a slot-shaped request on both front shapes, over real sockets."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
from pathlib import Path

import pytest

from levain.broker.stub import FIXED_BODY, GWPORT_BASE, Stub


async def _post(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str) -> bytes:
    body = b'{"model":"x"}'
    writer.write(b"POST %s HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: %d\r\n\r\n" % (path.encode(), len(body)) + body)
    await writer.drain()
    return await reader.read()


async def _connect_echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[bytes, bytes]:
    writer.write(b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n")
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    writer.write(b"tunnel-bytes")
    await writer.drain()
    echoed = await reader.readexactly(len(b"tunnel-bytes"))
    writer.close()
    return head, echoed


def _records(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def _free_gwport() -> int:
    for port in range(GWPORT_BASE, GWPORT_BASE + 64):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # a prior run's TIME_WAIT must not skip the port
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in the gateway range 47100-47163")


def test_stub_answers_slot_request():
    async def run(tmp: Path) -> None:
        log = tmp / "stub.jsonl"

        # Linux shape: a unix socket; the peer uid is the kernel's answer, the slot is configured.
        sock_path = tmp / "gw.sock"
        unix = await Stub(log, slot=3).serve_unix(sock_path)
        r, w = await asyncio.open_unix_connection(str(sock_path))
        resp = await _post(r, w, "/r/anthropic/v1/messages")
        assert resp.startswith(b"HTTP/1.1 200 ") and resp.endswith(FIXED_BODY)
        r, w = await asyncio.open_unix_connection(str(sock_path))
        head, echoed = await _connect_echo(r, w)
        assert head.startswith(b"HTTP/1.1 200 ") and echoed == b"tunnel-bytes"
        r, w = await asyncio.open_unix_connection(str(sock_path))
        assert (await _post(r, w, "/v1/messages")).startswith(b"HTTP/1.1 404 ")  # not a /r/<route> path: refused
        unix.close()
        await unix.wait_closed()

        # macOS shape: TCP on 127.0.0.1:47100+n; the slot comes from the port, the uid is unknown.
        port = _free_gwport()
        tcp = await Stub(log).serve_tcp(port)
        r, w = await asyncio.open_connection("127.0.0.1", port)
        resp = await _post(r, w, "/r/openrouter-glm/api/v1/chat/completions")
        assert resp.endswith(FIXED_BODY)
        r, w = await asyncio.open_connection("127.0.0.1", port)
        head, echoed = await _connect_echo(r, w)
        assert head.startswith(b"HTTP/1.1 200 ") and echoed == b"tunnel-bytes"
        tcp.close()
        await tcp.wait_closed()

        recs = _records(log)
        assert recs[0] == {"peer_uid": os.getuid(), "slot": 3, "route": "anthropic", "method": "POST",
                           "path": "/r/anthropic/v1/messages"}
        assert recs[1] == {"peer_uid": os.getuid(), "slot": 3, "route": None, "method": "CONNECT",
                           "path": "github.com:443"}
        assert recs[2] == {"peer_uid": None, "slot": port - GWPORT_BASE, "route": "openrouter-glm", "method": "POST",
                           "path": "/r/openrouter-glm/api/v1/chat/completions"}
        assert recs[3] == {"peer_uid": None, "slot": port - GWPORT_BASE, "route": None, "method": "CONNECT",
                           "path": "github.com:443"}
        assert len(recs) == 4  # the refused request logged nothing

    # /tmp, not pytest's tmp_path: macOS caps a unix socket path at 104 bytes.
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        asyncio.run(run(Path(d)))
