"""CH-D2 S9: the composed rig. The REAL glue (`levain/chat_egress/node/glue.mjs`) over srt 0.0.81 and the REAL decider,
spawned as S8's launch spawns them (a closed env, explicit fds), reusing `test_decider_glue.Launch`; plus an origin that
records every request byte-exactly and can reflect request values into its response, and the leak sweep.

A run writes only under one directory (`Launch.dir`): the glue's event file, its stderr, the dispatch store. `sweep`
greps every byte under it, plus whatever the test captured from logging and from the desk, for the secret values.
"""

from __future__ import annotations

import base64
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote


@dataclass
class Seen:
    method: str
    target: str
    headers: list[tuple[str, str]]       # in wire order, duplicates kept, name case as sent
    body: bytes
    raw_head: bytes
    raw_body: bytes = b""           # the body as it was framed on the wire (chunk sizes and trailers included)


class RecordingOrigin:
    """127.0.0.1 plain-HTTP origin on a raw socket: records each request's head and body as received, answers 200.
    `reflect(Seen)` returns extra response header lines (bytes, each `Name: value`); `content_type(Seen)` the
    Content-Type value (default text/plain)."""

    def __init__(self, reflect=None, content_type=None, tls: tuple[str, str] | None = None) -> None:
        self.reqs: list[Seen] = []
        self.tls_ctx = None
        if tls is not None:                # (cert chain pem, key pem): every accepted connection is TLS, one handshake each
            import ssl
            self.tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.tls_ctx.load_cert_chain(*tls)
        self.conns = 0                     # every ACCEPTED connection, counted before any TLS wrap or parse
        self.bytes_in = 0                  # every byte read: a TLS connection's first flight (peeked, before the wrap) + each later read
        self.first_flights: list[bytes] = []
        self.wrap_failures: list[str] = []
        self.open_conns = 0
        self.reflect = reflect
        self.content_type = content_type
        self._lock = threading.Lock()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(64)
        self._srv.settimeout(0.2)          # the accept loop re-reads _stop; closing a listener does not wake accept everywhere
        self.port = self._srv.getsockname()[1]
        self._stop = False
        self.after_wrap = None             # test hook: called with the wrapped socket, before the worker registers it
        self.snapshotted = threading.Event()   # set by close() once it has taken its snapshot of sockets and threads
        self._socks: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._t = threading.Thread(target=self._accept, daemon=True)
        self._t.start()

    def _accept(self) -> None:
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            t = threading.Thread(target=self._serve, args=(c,), daemon=True)
            with self._lock:               # register and start under one lock, serialized against close()'s snapshot
                if self._stop:
                    c.close()
                    return
                self.conns += 1
                self.open_conns += 1
                self._socks.append(c)
                self._threads.append(t)
                t.start()

    def _recv(self, c: socket.socket) -> bytes:
        """One read, counted; the peer's EOF ends the connection (raises) rather than spinning the caller's loop."""
        d = c.recv(65536)
        if not d:
            raise EOFError
        self._count(len(d))
        return d

    def _count(self, n: int) -> None:
        with self._lock:
            self.bytes_in += n

    def touched(self) -> dict:
        """What reached the origin at the connection and byte level; parsed requests are only the subset that survived
        a TLS wrap and the origin's own parser."""
        with self._lock:
            return {"conns": self.conns, "bytes": self.bytes_in, "wrap_failures": list(self.wrap_failures),
                    "reqs": [(r.method, r.target) for r in self.reqs]}

    def assert_untouched(self, settle: float = 0.3) -> None:
        """The "reaches nothing" oracle: zero accepted connections, zero bytes, no wrap failure and no parsed request,
        after a short drain wait. Plaintext into the TLS port, a bare connect and a body the origin never framed all count."""
        time.sleep(settle)
        t = self.touched()
        assert t["conns"] == 0 and t["bytes"] == 0 and not t["wrap_failures"] and not t["reqs"], t

    def _serve(self, c: socket.socket) -> None:       # noqa: C901 - one wire reader, kept in one place
        c.settimeout(10)
        buf = b""
        try:
            if self.tls_ctx is not None:
                first = c.recv(65536, socket.MSG_PEEK)           # the first flight, left in the buffer for the handshake
                self._count(len(first))
                with self._lock:
                    self.first_flights.append(first)
                try:
                    wrapped = self.tls_ctx.wrap_socket(c, server_side=True)
                except (OSError, ValueError) as exc:             # ssl.SSLError is an OSError
                    with self._lock:
                        self.wrap_failures.append(type(exc).__name__)
                    raise
                c = wrapped                                      # the wrap detached the original fd: track the new socket
                if self.after_wrap is not None:
                    self.after_wrap(c)
                with self._lock:
                    if self._stop:                               # close() snapshotted without this wrapper: close it here
                        c.close()
                        return
                    self._socks.append(c)
            while True:
                while b"\r\n\r\n" not in buf:
                    buf += self._recv(c)
                head, _, buf = buf.partition(b"\r\n\r\n")
                lines = head.split(b"\r\n")
                method, target, _ = lines[0].decode("latin-1").split(" ", 2)
                hdrs = []
                for ln in lines[1:]:
                    k, _, v = ln.decode("latin-1").partition(":")
                    hdrs.append((k, v.strip()))
                low = {k.lower(): v for k, v in hdrs}
                body = b""
                raw = bytearray()
                if low.get("transfer-encoding", "").lower() == "chunked":
                    while True:
                        while b"\r\n" not in buf:
                            buf += self._recv(c)
                        size_line, _, buf = buf.partition(b"\r\n")
                        raw += size_line + b"\r\n"
                        n = int(size_line.split(b";")[0], 16)
                        if n == 0:
                            # the trailer section: header lines, ended by an empty line
                            while not (buf.startswith(b"\r\n") or b"\r\n\r\n" in buf):
                                buf += self._recv(c)
                            cut = 2 if buf.startswith(b"\r\n") else buf.index(b"\r\n\r\n") + 4
                            raw += buf[:cut]
                            buf = buf[cut:]
                            break
                        while len(buf) < n + 2:
                            buf += self._recv(c)
                        body += buf[:n]
                        raw += buf[:n + 2]
                        buf = buf[n + 2:]
                elif "content-length" in low:
                    n = int(low["content-length"])
                    while len(buf) < n:
                        buf += self._recv(c)
                    body, buf = buf[:n], buf[n:]
                    raw += body
                seen = Seen(method, target, hdrs, body, head, bytes(raw))
                with self._lock:
                    self.reqs.append(seen)
                extra = self.reflect(seen) if self.reflect else []
                ct = self.content_type(seen) if self.content_type else "text/plain"
                out = b"HTTP/1.1 200 OK\r\nContent-Type: " + ct.encode("latin-1") + b"\r\nContent-Length: 2\r\n"
                out += b"".join(x + b"\r\n" for x in extra) + b"\r\nok"
                c.sendall(out)
        except (OSError, ValueError, EOFError):
            return
        finally:
            c.close()
            with self._lock:
                self.open_conns -= 1

    def close(self) -> None:
        with self._lock:
            self._stop = True
        self._t.join(5)
        try:
            self._srv.close()
        except OSError:
            pass
        with self._lock:
            socks, threads = list(self._socks), list(self._threads)
        self.snapshotted.set()
        for c in socks:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for t in threads:
            t.join(5)


def variants(secret: str) -> list[bytes]:
    """The spellings a leak could take: raw, percent-encoded, base64 of the bare value and of `user:value`."""
    s = secret.encode()
    return [s, quote(secret, safe="").encode(), base64.b64encode(s), base64.b64encode(b"user:" + s)]


def sweep(directory: Path, secrets: dict[str, str], extra: dict[str, bytes] | None = None) -> list[str]:
    """Every file under `directory`, plus the named `extra` byte blobs (captured log text, filed cards), grepped for
    each secret in each spelling. Returns the hits as `"<where>: <secret label>"`; zero hits is the pass."""
    blobs: dict[str, bytes] = {}
    for p in sorted(Path(directory).rglob("*")):
        if p.is_file():
            blobs[str(p.relative_to(directory))] = p.read_bytes()
    blobs.update(extra or {})
    hits = []
    for where, data in blobs.items():
        for label, value in secrets.items():
            for v in variants(value):
                if v in data:
                    hits.append(f"{where}: {label}")
                    break
    return hits


def autoanswer(launch, answer: str = "denied") -> threading.Event:
    """Answers every card the launch's desk files with `answer`, until the returned event is set."""
    stop = threading.Event()

    def run() -> None:
        import time
        done = 0
        while not stop.is_set():
            n = len(launch.desk.filed)
            while done < n:
                done += 1
                launch.desk.answer[f"card-{done}"] = answer
            time.sleep(0.05)

    threading.Thread(target=run, daemon=True).start()
    return stop


class LeakyRelay:
    """TEST-ONLY negative control: a deliberately leaky stand-in for the proxy, so a "reaches nothing" probe can be shown
    RED. It accepts a client, answers the dialect's handshake as an open proxy would (`connect`: a 200 to CONNECT;
    `socks`: SOCKS5 no-auth then success; `raw`: nothing, the bytes simply go through), then relays every byte both ways to
    the origin on `origin_port`."""

    def __init__(self, origin_port: int, dialect: str) -> None:
        assert dialect in ("connect", "socks", "raw"), dialect
        self.origin_port, self.dialect = origin_port, dialect
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self._srv.settimeout(0.2)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        self.after_accept = None           # test hook: called in the accept thread, after accept() and before registration
        self.stopping = threading.Event()  # set by close() as it begins
        self._socks: list[socket.socket] = []
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = [threading.Thread(target=self._accept, daemon=True)]
        self._threads[0].start()

    def _spawn(self, fn, *a) -> bool:
        """Starts a worker unless close() has begun (it sets `_stop` under this lock, then snapshots)."""
        t = threading.Thread(target=fn, args=a, daemon=True)
        with self._lock:
            if self._stop:
                return False
            self._threads.append(t)
            t.start()
        return True

    def _register(self, s: socket.socket) -> bool:
        """Tracks `s` for close(); once close() has begun, closes it instead and says so."""
        with self._lock:
            if not self._stop:
                self._socks.append(s)
                return True
        s.close()
        return False

    def _accept(self) -> None:
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self.after_accept is not None:
                self.after_accept(c)
            if not self._register(c):
                return
            if not self._spawn(self._serve, c):
                c.close()
                return

    @staticmethod
    def _read_until_more(c: socket.socket, out: bytes, marker: bytes) -> bytes:
        while marker not in out:
            d = c.recv(4096)
            if not d:
                raise EOFError
            out += d
        return out

    @staticmethod
    def _read_exact(c: socket.socket, n: int) -> bytes:
        out = b""
        while len(out) < n:
            d = c.recv(n - len(out))
            if not d:
                raise EOFError
            out += d
        return out

    def _serve(self, c: socket.socket) -> None:
        c.settimeout(10)
        o = None
        try:
            head = b""
            if self.dialect == "connect":
                head = c.recv(4096)                               # anything that is not a CONNECT is simply relayed
                if head.startswith(b"CONNECT "):
                    head = self._read_until_more(c, head, b"\r\n\r\n")
                    tail = head.split(b"\r\n\r\n", 1)[1]
                    c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    head = tail
            elif self.dialect == "socks":
                self._read_exact(c, self._read_exact(c, 2)[1])    # greeting: VER NMETHODS METHODS...
                c.sendall(b"\x05\x00")
                req = self._read_exact(c, 4)
                if req[3] == 3:
                    self._read_exact(c, self._read_exact(c, 1)[0] + 2)
                else:
                    self._read_exact(c, (4 if req[3] == 1 else 16) + 2)
                c.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            o = socket.create_connection(("127.0.0.1", self.origin_port), timeout=10)
            if not self._register(o):
                return
            if head:
                o.sendall(head)
            if not self._spawn(self._pipe, o, c):
                return
            self._pipe(c, o)
        except (OSError, EOFError):
            pass

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                d = src.recv(65536)
                if not d:
                    break
                dst.sendall(d)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._stop = True                                    # no registration or spawn succeeds after this
        self.stopping.set()
        self._threads[0].join(5)                                 # the accept thread, before the snapshot
        try:
            self._srv.close()
        except OSError:
            pass
        with self._lock:
            socks, threads = list(self._socks), list(self._threads)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        for t in threads[1:]:
            t.join(5)
