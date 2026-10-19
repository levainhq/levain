"""The gateway STUB (S1 contract §4.8, plan §2d 12): front-shaped, so sessions 1-2 can test a slot's path to its gateway
before the real front (3a) and keys (3b/3c) exist. It enforces nothing and holds no credential.

Linux shape: a unix socket (the front's `R/gw/slot<n>/gw.sock`); the peer uid comes from the kernel. macOS shape: TCP on
127.0.0.1:<port>; the slot is the port's index (`port - 47100`) and the peer uid is unknown (None).

It answers two requests and refuses everything else:
- `CONNECT host:port` -> `200 Connection Established`, then echoes every byte back until the client closes.
- `POST /r/<route>/...` with a `Content-Length` body -> 200 with FIXED_BODY.
Each answered request appends one JSON line to `log_path`: {peer_uid, slot, route, method, path}.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import struct
import sys
from pathlib import Path

GWPORT_BASE = 47100
FIXED_BODY = b'{"stub":"levain-gateway-stub/1"}'
_HEAD_CAP = 64 * 1024
_BODY_CAP = 1024 * 1024
_ROUTE = re.compile(rb"^/r/([a-z0-9-]{1,48})(/[^?\s]*)?(\?\S*)?$")
_XUCRED = struct.Struct("=IIh16I")  # macOS struct xucred: cr_version, cr_uid, cr_ngroups, cr_groups[16]


def peer_uid(sock: socket.socket) -> int | None:
    """The connecting process's uid on a unix socket, from the kernel; None for TCP."""
    if sock.family != socket.AF_UNIX:
        return None
    if sys.platform.startswith("linux"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)[1]
    if sys.platform == "darwin":
        raw = sock.getsockopt(0, socket.LOCAL_PEERCRED, _XUCRED.size)  # SOL_LOCAL == 0
        return _XUCRED.unpack(raw[: _XUCRED.size])[1]
    return None


class Stub:
    def __init__(self, log_path: Path | str, slot: int | None = None) -> None:
        self.log_path = Path(log_path)
        self.slot = slot  # the unix-socket shape names its slot; the TCP shape derives it from the port

    def _log(self, rec: dict) -> None:
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # the client went away mid-request or mid-tunnel: nothing to answer
        finally:
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        uid = peer_uid(sock)
        slot = self.slot
        if sock.family != socket.AF_UNIX:
            slot = writer.get_extra_info("sockname")[1] - GWPORT_BASE
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        lines = head.split(b"\r\n")
        parts = lines[0].split(b" ")
        if len(parts) != 3 or not parts[2].startswith(b"HTTP/1."):
            await self._refuse(writer, 400)
            return
        method, target = parts[0], parts[1]
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower()] = v.strip()

        if method == b"CONNECT":
            self._log({"peer_uid": uid, "slot": slot, "route": None, "method": "CONNECT",
                       "path": target.decode("ascii", "replace")})
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
            writer.close()
            return

        m = _ROUTE.match(target)
        if method != b"POST" or not m:
            await self._refuse(writer, 404)
            return
        try:
            length = int(headers.get(b"content-length", b""))
        except ValueError:
            await self._refuse(writer, 400)
            return
        if not 0 <= length <= _BODY_CAP:
            await self._refuse(writer, 413)
            return
        await reader.readexactly(length)
        self._log({"peer_uid": uid, "slot": slot, "route": m.group(1).decode(), "method": "POST",
                   "path": target.decode("ascii", "replace")})
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n" % len(FIXED_BODY) + FIXED_BODY)
        await writer.drain()
        writer.close()

    async def _refuse(self, writer: asyncio.StreamWriter, status: int) -> None:
        writer.write(b"HTTP/1.1 %d Refused\r\nContent-Length: 0\r\nConnection: close\r\n\r\n" % status)
        await writer.drain()
        writer.close()

    async def serve_unix(self, path: Path | str) -> asyncio.base_events.Server:
        return await asyncio.start_unix_server(self._handle, path=str(path), limit=_HEAD_CAP)

    async def serve_tcp(self, port: int, host: str = "127.0.0.1") -> asyncio.base_events.Server:
        return await asyncio.start_server(self._handle, host=host, port=port, limit=_HEAD_CAP)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m levain.broker.stub")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--unix", help="socket path (the Linux front shape)")
    g.add_argument("--port", type=int, help="TCP port on 127.0.0.1 (the macOS front shape, 47100 + slot)")
    ap.add_argument("--slot", type=int, help="slot index for the unix shape")
    ap.add_argument("--log", required=True)
    a = ap.parse_args(argv)
    stub = Stub(a.log, slot=a.slot)

    async def run() -> None:
        server = await (stub.serve_unix(a.unix) if a.unix else stub.serve_tcp(a.port))
        async with server:
            await server.serve_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        if a.unix:
            try:
                os.unlink(a.unix)
            except FileNotFoundError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
