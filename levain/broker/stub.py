"""The gateway STUB (S1 contract §4.8, plan §2d 12): front-shaped, so sessions 1-2 can test a slot's path to its gateway
before the real front (3a) and keys (3b/3c) exist. It enforces nothing and holds no credential.

Linux shape: a unix socket (the front's `R/gw/slot<n>/gw.sock`); the peer uid comes from the kernel. macOS shape: TCP on
127.0.0.1:<port>; the slot is the port's index (`port - 47100`) and the peer uid is unknown (None).

It answers two requests and refuses everything else:
- `CONNECT host:port` -> `200 Connection Established`, then echoes every byte back until the client closes.
- `POST /r/<route>/...` with a `Content-Length` body -> 200 with FIXED_BODY.
Each answered request appends one JSON line to `log_path`: {peer_uid, slot, route, method, path}.

What a green run means, and does not (L2 review, 10-19): a log record = the slot REACHED its gateway as the logged uid/slot.
Never "the CLI worked": a real client's TLS handshake through the echo always fails, and a vendor may send several POSTs per
prompt (retries, non-streaming fallback) plus non-essential CONNECTs, so assert "at least one record with the right slot and
uid", never an exact count; give the slot a dummy API key so the vendor sends at all. Point only HTTPS_PROXY at the stub:
an HTTP_PROXY makes plain-http base-URL calls absolute-form, which 404s here. Bodies: Content-Length only (chunked = 400),
no Expect: 100-continue, no keep-alive, no streaming response; the real front (3a) owes all of those.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import stat
import struct
import sys
from pathlib import Path

GWPORT_BASE = 47100
SLOTS = 64  # the egress index space, contract §1.1
FIXED_BODY = b'{"stub":"levain-gateway-stub/1"}'
_HEAD_CAP = 64 * 1024
_BODY_CAP = 1024 * 1024
_ROUTE = re.compile(rb"^/r/([a-z0-9-]{1,48})(/[^?\s]*)?(\?\S*)?\Z")
_XUCRED = struct.Struct("@IIh16I")  # macOS struct xucred, native alignment: cr_version, cr_uid, cr_ngroups, cr_groups[16]


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
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.slot = slot  # the unix-socket shape names its slot; the TCP shape derives it from the port
        self.bound: tuple[str, int] | None = None

    def _log(self, rec: dict) -> None:
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError):
            pass  # the client went away mid-request or mid-tunnel (incl. a peer-cred read on a dead socket)
        finally:
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        uid = peer_uid(sock)
        slot = self.slot
        if sock.family != socket.AF_UNIX:
            slot = writer.get_extra_info("sockname")[1] - GWPORT_BASE  # serve_tcp admits only the gateway range
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return
        lines = head.split(b"\r\n")
        parts = lines[0].split(b" ")
        if len(parts) != 3 or not parts[2].startswith(b"HTTP/1."):
            await self._refuse(writer, 400)
            return
        method, target = parts[0], parts[1]
        headers: dict[bytes, list[bytes]] = {}
        for line in lines[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers.setdefault(k.strip().lower(), []).append(v.strip())

        if method == b"CONNECT":
            self._log({"peer_uid": uid, "slot": slot, "route": None, "method": "CONNECT",
                       "path": target.decode("ascii", "replace")})
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
            return

        # Framing first, so every answer below is sent after the body is read (an unread body makes the
        # close an RST that can destroy the status). Chunked, or any framing but one plain Content-Length: 400.
        lengths = headers.get(b"content-length", [b"0"] if method != b"POST" else [])
        if b"transfer-encoding" in headers or len(lengths) != 1 or not lengths[0].isdigit():
            await self._refuse(writer, 400)
            return
        length = int(lengths[0])
        if length > _BODY_CAP:
            await self._refuse(writer, 413)
            return
        await reader.readexactly(length)
        m = _ROUTE.match(target)
        if method != b"POST" or not m:
            await self._refuse(writer, 404)
            return
        self._log({"peer_uid": uid, "slot": slot, "route": m.group(1).decode(), "method": "POST",
                   "path": target.decode("ascii", "replace")})
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n" % len(FIXED_BODY) + FIXED_BODY)
        await writer.drain()

    async def _refuse(self, writer: asyncio.StreamWriter, status: int) -> None:
        writer.write(b"HTTP/1.1 %d Refused\r\nContent-Length: 0\r\nConnection: close\r\n\r\n" % status)
        await writer.drain()

    async def serve_unix(self, path: Path | str) -> asyncio.base_events.Server:
        """Bind `path` mode 0660 (the slot reaches it through the socket's GROUP, contract §2.2), never over an
        existing file. A stale SOCKET at the path is replaced; any other file refuses (FileExistsError)."""
        if self.slot is None or not 0 <= self.slot < SLOTS:
            raise ValueError(f"the unix shape needs a slot index in 0..{SLOTS - 1}")
        path = str(path)
        try:
            if stat.S_ISSOCK(os.lstat(path).st_mode):
                os.unlink(path)
            else:
                raise FileExistsError(path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(path)
            self.bound = (path, os.lstat(path).st_ino)
            os.chmod(path, 0o660)  # the socket's mode is the umask's until set; group must be able to connect
            return await asyncio.start_unix_server(self._handle, sock=s, limit=_HEAD_CAP)
        except BaseException:
            s.close()
            self.unbind()
            raise

    def unbind(self) -> None:
        """Remove the socket this process bound, only while the path still names that inode."""
        if self.bound is None:
            return
        path, ino = self.bound
        self.bound = None
        try:
            if os.lstat(path).st_ino == ino:
                os.unlink(path)
        except FileNotFoundError:
            pass

    async def serve_tcp(self, port: int, host: str = "127.0.0.1") -> asyncio.base_events.Server:
        if not GWPORT_BASE <= port < GWPORT_BASE + SLOTS:
            raise ValueError(f"the TCP shape serves only the gateway range {GWPORT_BASE}..{GWPORT_BASE + SLOTS - 1}")
        return await asyncio.start_server(self._handle, host=host, port=port, limit=_HEAD_CAP)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m levain.broker.stub")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--unix", help="socket path (the Linux front shape); needs --slot")
    g.add_argument("--port", type=int, help=f"TCP port on 127.0.0.1 (the macOS front shape, {GWPORT_BASE} + slot)")
    ap.add_argument("--slot", type=int, help="slot index for the unix shape")
    ap.add_argument("--log", required=True)
    a = ap.parse_args(argv)
    if a.unix and a.slot is None:
        ap.error("--unix needs --slot")
    if a.port is not None and a.slot is not None:
        ap.error("--slot is the port's index in the TCP shape; do not pass it")
    stub = Stub(a.log, slot=a.slot)

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):  # a plain `kill` must clean up too, not only Ctrl-C
            loop.add_signal_handler(sig, stop.set)
        server = await (stub.serve_unix(a.unix) if a.unix else stub.serve_tcp(a.port))
        async with server:
            await stop.wait()

    try:
        asyncio.run(run())
    finally:
        stub.unbind()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
