"""Clients for the probes: curl through the proxy, and raw sockets for the cases curl will not send."""

from __future__ import annotations

import socket
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Curl:
    rc: int
    out: str
    err: str
    status: int       # curl's %{http_code}; 0 when no response reached curl (a failed CONNECT)
    elapsed: float

    @property
    def deny_reason(self) -> str | None:
        for ln in self.out.splitlines():
            if ln.lower().startswith("x-deny-reason:"):
                return ln.split(":", 1)[1].strip()
        return None

    @property
    def first_line(self) -> str:
        return self.out.splitlines()[0] if self.out else ""


def curl(proxy_port: int, ca: Path | str, url: str, *extra: str, max_time: float = 15, proxy: bool = True,
         include: bool = True) -> Curl:
    argv = ["curl", "-sS", "--max-time", str(max_time)]
    if proxy:
        argv += ["--proxy", f"http://127.0.0.1:{proxy_port}"]
    argv += ["--cacert", str(ca)]
    if include:
        argv.append("-i")
    argv += ["-o", "-", "-w", "\nSTATUS=%{http_code}\n", *extra, url]
    t0 = time.time()
    r = subprocess.run(argv, capture_output=True, timeout=max_time + 30)
    out = r.stdout.decode("latin1")
    status = 0
    for ln in reversed(out.splitlines()):
        if ln.startswith("STATUS="):
            status = int(ln[7:] or 0)
            break
    return Curl(rc=r.returncode, out=out, err=r.stderr.decode("latin1"), status=status, elapsed=time.time() - t0)


# --- raw sockets --------------------------------------------------------------------------------------------------------
def recv_all(s: socket.socket, limit: int = 6000) -> bytes:
    out = b""
    try:
        while len(out) < limit:
            b = s.recv(65536)
            if not b:
                break
            out += b
    except (socket.timeout, ssl.SSLError, ConnectionResetError, OSError) as e:
        out += f"<<{type(e).__name__}: {e}>>".encode()
    return out


class Raw:
    """Raw access to the proxy port: plain connections, CONNECT tunnels, TLS inside them."""

    def __init__(self, proxy_port: int, ca: Path | str, timeout: float = 30) -> None:
        self.port, self.ca, self.timeout = proxy_port, str(ca), timeout

    def conn(self) -> socket.socket:
        return socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)

    def connect(self, host: str, port: int) -> tuple[socket.socket, bytes]:
        s = self.conn()
        s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            b = s.recv(1)
            if not b:
                break
            head += b
        return s, head

    def tls(self, s: socket.socket, sni: str | None, *, check_hostname: bool = False) -> ssl.SSLSocket:
        ctx = ssl.create_default_context(cafile=self.ca)
        ctx.check_hostname = check_hostname
        return ctx.wrap_socket(s, server_hostname=sni)

    def tunnel(self, host: str, port: int, sni: str | None | bool = True) -> tuple[ssl.SSLSocket, bytes]:
        """CONNECT then TLS (sni=True -> the host, None -> no server_name)."""
        s, head = self.connect(host, port)
        return self.tls(s, host if sni is True else sni, check_hostname=sni is True), head

    @staticmethod
    def exchange(s: socket.socket, req: bytes, limit: int = 6000) -> bytes:
        s.sendall(req)
        return recv_all(s, limit)

    def one_shot(self, req: bytes) -> bytes:
        s = self.conn()
        try:
            return self.exchange(s, req)
        finally:
            s.close()


def status_line(reply: bytes) -> str:
    return reply.split(b"\r\n", 1)[0].decode("latin1") if reply else ""


def has_header(reply: bytes, name: str, value: str | None = None) -> bool:
    for ln in reply.split(b"\r\n\r\n", 1)[0].decode("latin1").split("\r\n")[1:]:
        k, _, v = ln.partition(":")
        if k.strip().lower() == name.lower() and (value is None or value in v):
            return True
    return False


def dns_query(port: int, name: str, qtype: str = "A", timeout: float = 3.0) -> list[str]:
    """One UDP query to the local responder (the decider's 'own resolve' in the rebinding probes: it consumes the
    responder's FIRST answer for the name, so srt's later lookup gets the SECOND one). Returns the answer addresses."""
    import struct
    qt = {"A": 1, "AAAA": 28}[qtype]
    q = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00" + struct.pack(">HH", qt, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, ("127.0.0.1", port))
        data, _ = s.recvfrom(2048)
    finally:
        s.close()
    n_ans = struct.unpack(">H", data[6:8])[0]
    at = 12
    while data[at]:
        at += 1 + data[at]
    at += 5
    out = []
    for _ in range(n_ans):
        at += 2
        rtype, _c, _ttl, rdl = struct.unpack(">HHIH", data[at:at + 10])
        at += 10
        rd = data[at:at + rdl]
        at += rdl
        out.append(socket.inet_ntop(socket.AF_INET if rtype == 1 else socket.AF_INET6, rd))
    return out
