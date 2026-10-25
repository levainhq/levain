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


class RecordingOrigin:
    """127.0.0.1 plain-HTTP origin on a raw socket: records each request's head and body as received, answers 200.
    `reflect(Seen)` returns extra response header lines (bytes, each `Name: value`); `content_type(Seen)` the
    Content-Type value (default text/plain)."""

    def __init__(self, reflect=None, content_type=None) -> None:
        self.reqs: list[Seen] = []
        self.conns = 0
        self.reflect = reflect
        self.content_type = content_type
        self._lock = threading.Lock()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(64)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        self._t = threading.Thread(target=self._accept, daemon=True)
        self._t.start()

    def _accept(self) -> None:
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            with self._lock:
                self.conns += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c: socket.socket) -> None:
        c.settimeout(10)
        buf = b""
        try:
            while True:
                while b"\r\n\r\n" not in buf:
                    d = c.recv(65536)
                    if not d:
                        return
                    buf += d
                head, _, buf = buf.partition(b"\r\n\r\n")
                lines = head.split(b"\r\n")
                method, target, _ = lines[0].decode("latin-1").split(" ", 2)
                hdrs = []
                for ln in lines[1:]:
                    k, _, v = ln.decode("latin-1").partition(":")
                    hdrs.append((k, v.strip()))
                low = {k.lower(): v for k, v in hdrs}
                body = b""
                if low.get("transfer-encoding", "").lower() == "chunked":
                    while True:
                        while b"\r\n" not in buf:
                            buf += c.recv(65536)
                        size_line, _, buf = buf.partition(b"\r\n")
                        n = int(size_line.split(b";")[0], 16)
                        if n == 0:
                            # the trailer section: header lines, ended by an empty line
                            while not (buf.startswith(b"\r\n") or b"\r\n\r\n" in buf):
                                buf += c.recv(65536)
                            buf = buf[2:] if buf.startswith(b"\r\n") else buf.split(b"\r\n\r\n", 1)[1]
                            break
                        while len(buf) < n + 2:
                            buf += c.recv(65536)
                        body += buf[:n]
                        buf = buf[n + 2:]
                elif "content-length" in low:
                    n = int(low["content-length"])
                    while len(buf) < n:
                        buf += c.recv(65536)
                    body, buf = buf[:n], buf[n:]
                seen = Seen(method, target, hdrs, body, head)
                with self._lock:
                    self.reqs.append(seen)
                extra = self.reflect(seen) if self.reflect else []
                ct = self.content_type(seen) if self.content_type else "text/plain"
                out = b"HTTP/1.1 200 OK\r\nContent-Type: " + ct.encode("latin-1") + b"\r\nContent-Length: 2\r\n"
                out += b"".join(x + b"\r\n" for x in extra) + b"\r\nok"
                c.sendall(out)
        except (OSError, ValueError):
            return
        finally:
            c.close()

    def close(self) -> None:
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass


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
