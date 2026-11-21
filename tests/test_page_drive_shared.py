"""The shared session Chrome and the driver's use of it: the same viewport as the driver's own Chrome, a setup failure falls
back to a Chrome of its own (never a hang, never a failure), and a killed pytest leaves neither the Chrome nor its profile."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests import _page_drive_run, _shared_chrome
from tests.test_cockpit_flip_parity import CHROME, DRIVE, NODE, needs_browser

ROOT = Path(__file__).resolve().parent.parent
GEOMETRY = "JSON.stringify([innerWidth, innerHeight, screen.width, screen.height, outerWidth, outerHeight])"
FALLBACK_BOUND_SECONDS = 40   # setup bounds (a few seconds each) plus the spawned Chrome's own start


def _drive(tmp_path: Path, steps: list, env: dict[str, str], timeout: float = 90):
    f = tmp_path / "steps.json"
    f.write_text(json.dumps(steps))
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return _page_drive_run.run([NODE, str(DRIVE), CHROME, "about:blank", str(f)], timeout=timeout)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _geometry(tmp_path: Path, env: dict[str, str]) -> list:
    r = _drive(tmp_path, [{"eval": GEOMETRY}], env)
    assert r.returncode == 0, r.stderr
    return json.loads(json.loads(r.stdout)["values"][0])


@pytest.fixture
def shared_port():
    port = _shared_chrome.port()
    if port is None:
        pytest.skip("the shared Chrome could not start here")
    yield port
    _shared_chrome.shutdown()


@needs_browser
def test_a_shared_drive_sees_the_same_viewport_as_a_spawned_one(tmp_path: Path, shared_port: int) -> None:
    spawned = _geometry(tmp_path, {"LEVAIN_DRIVE_CDP_PORT": ""})
    shared = _geometry(tmp_path, {"LEVAIN_DRIVE_CDP_PORT": str(shared_port)})
    assert shared == spawned, {"spawned": spawned, "shared": shared}
    assert shared[0] == 1440, shared


@needs_browser
def test_a_page_that_stops_answering_on_the_shared_chrome_is_still_listed_by_the_browser(tmp_path: Path, shared_port: int) -> None:
    # the timeout block asks the SHARED Chrome (Target.getTargets) whether this drive's own page target is alive, by its id: the url
    # carries a marker no other drive's page in the same Chrome has, so the line names this drive's target and not a neighbour's
    steps = [{"eval": "location.hash = '#shared-hang-marker'; setTimeout(() => { for (;;) {} }, 50); 1"}, {"until": "false", "ms": 1500}]
    r = _drive(tmp_path, steps, {"LEVAIN_DRIVE_CDP_PORT": str(shared_port), "LEVAIN_TEST_WAIT_SCALE": "1"}, timeout=90)
    assert r.returncode == 1 and "until never came true within 1500ms" in r.stderr, (r.returncode, r.stderr)
    _, _, block = r.stderr.partition("--- page state at the timeout ---")
    assert "PAGE URL: ?" in block, r.stderr                                           # the page did not answer: the premise of the test
    assert re.search(r'^TARGET: listed, id=[0-9A-F]{32} type=page attached=\w+ url="about:blank#shared-hang-marker"', block, re.M), r.stderr


class _ChromeProxy:
    """A TCP proxy in front of the shared Chrome that fails the DRIVE's side of the browser connection when it sees Target.getTargets.

    The Chrome behind it stays up and keeps the drive's context (the upstream half of the connection is only closed by `close()`), so
    what is simulated is a browser connection that drops during a hang while the browser itself, and its /json listing, still answer.
    mode "drop": the drive's socket is closed at once. mode "hold": the request is swallowed and the drive's socket is closed after
    `delay` seconds, with every later browser command swallowed too, so the drive is still alive when the late close arrives.
    `refuse_http`: once tripped, every other connection through the proxy (the /json listing included) is closed on its next bytes."""

    def __init__(self, upstream: int, mode: str, delay: float = 2.5, refuse_http: bool = False) -> None:
        self.upstream, self.mode, self.delay, self.refuse_http = upstream, mode, delay, refuse_http
        self.tripped = False
        self.refused = 0                              # connections/requests the proxy refused after the trip
        self.http_log: list[tuple[bool, bytes]] = []  # every non-browser request forwarded upstream: (tripped at that moment, bytes)
        self._lock = threading.Lock()             # one lock around "check tripped + forward" and around "set tripped"
        self._socks: list[socket.socket] = []
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        for s in [self._srv, *self._socks]:
            try:
                s.shutdown(socket.SHUT_RDWR)   # wakes a thread blocked in recv on it; close alone does not
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def _accept(self) -> None:
        while True:
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            self._socks.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass

    @staticmethod
    def _client_frames(buf: bytearray):
        """The complete client-to-server websocket frames in `buf` as (raw, unmasked payload), consumed from it."""
        while len(buf) >= 2:
            n, off = buf[1] & 0x7F, 2
            if n == 126 and len(buf) >= 4:
                n, off = int.from_bytes(buf[2:4], "big"), 4
            elif n == 127 and len(buf) >= 10:
                n, off = int.from_bytes(buf[2:10], "big"), 10
            elif n >= 126:
                return
            key = bytes(buf[off:off + 4]) if buf[1] & 0x80 else b""
            end = off + len(key) + n
            if len(buf) < end:
                return
            body = bytes(buf[off + len(key):end])
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(body)) if key else body
            raw = bytes(buf[:end])
            del buf[:end]
            yield raw, payload

    def _refusing(self) -> bool:
        return self.tripped and self.refuse_http

    @staticmethod
    def _send(up: socket.socket, payload: bytes, timeout: float = 5) -> None:
        """Send all of `payload` on `up` within `timeout` seconds (OSError past it). `up` stays blocking for the pump thread's reads, so
        the bound is select plus non-blocking sends, not a socket timeout."""
        deadline, view = time.monotonic() + timeout, memoryview(payload)
        while view:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [up], [], left)[1]:
                raise OSError("the upstream stopped reading")
            try:
                view = view[up.send(view, socket.MSG_DONTWAIT):]
            except BlockingIOError:
                pass

    def _forward_http(self, up: socket.socket, payload: bytes) -> bool:
        """Forward a non-browser request unless the proxy is refusing. The decision (check + log entry, with `tripped` as of the
        decision) is made under the lock against the trip, so no request is decided "forward" once `tripped` is set; the send itself
        is outside the lock, so a stalled upstream cannot keep the trip from being recorded."""
        with self._lock:
            if self._refusing():
                self.refused += 1
                return False
            self.http_log.append((self.tripped, payload))
        self._send(up, payload)
        return True

    def _serve(self, c: socket.socket) -> None:
        try:
            up = socket.create_connection(("127.0.0.1", self.upstream), timeout=5)
        except OSError:
            c.close()
            return
        up.settimeout(None)
        self._socks.append(up)
        threading.Thread(target=self._pump, args=(up, c), daemon=True).start()
        head, frames, browser, holding = bytearray(), bytearray(), None, False
        try:
            while data := c.recv(65536):
                if browser is None:
                    head += data
                    end = head.find(b"\r\n\r\n")
                    if end < 0:
                        continue
                    browser = b"/devtools/browser/" in bytes(head[:end])
                    if browser:
                        up.sendall(bytes(head[:end + 4]))
                        data = bytes(head[end + 4:])
                    else:
                        data = bytes(head)
                if not browser:
                    # the refusal is decided BEFORE a byte goes upstream: a request forwarded first is one Chrome answers (the pump
                    # thread relays the answer) while this thread is still on its way to the shutdown
                    if data and not self._forward_http(up, data):
                        # each half is SHUT DOWN, then closed, and each step is guarded (the client's shutdown raises when it already reset).
                        # On Linux a close() from this thread does not release a socket the pump thread is blocked in recv on (the kernel
                        # holds it until that recv returns), so a bare up.close() left the upstream half open: only the shutdown wakes it
                        for s in (c, up):
                            for step in (lambda s=s: s.shutdown(socket.SHUT_RDWR), s.close):
                                try:
                                    step()
                                except OSError:
                                    pass
                        return
                    continue
                frames += data
                for raw, payload in self._client_frames(frames):
                    try:
                        method = json.loads(payload).get("method")
                    except ValueError:
                        method = None
                    if holding:
                        continue
                    if method == "Target.getTargets":
                        with self._lock:
                            self.tripped = True
                        if self.mode == "drop":
                            c.shutdown(socket.SHUT_RDWR)
                            return
                        holding = True
                        threading.Timer(self.delay, lambda: self._cut(c)).start()
                        continue
                    up.sendall(raw)
        except OSError:
            pass

    @staticmethod
    def _cut(c: socket.socket) -> None:
        try:
            c.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _Upstream:
    """A stand-in for Chrome: answers every complete HTTP request head (however the bytes were split) with a 200."""

    def __init__(self) -> None:
        self._stop = False
        self._conns: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self._srv.settimeout(0.1)
        self.port = self._srv.getsockname()[1]
        self._acceptor = threading.Thread(target=self._accept, daemon=True)
        self._acceptor.start()

    def _spawn(self, target, *args) -> None:
        t = threading.Thread(target=target, args=args, daemon=True)
        t.start()                     # started before it is listed: a listed thread can always be joined
        self._threads.append(t)

    def close(self) -> None:
        self._stop = True
        self._acceptor.join(2)        # the acceptor is the only writer of _conns/_threads: once it is gone the snapshot below is final
        for s in [self._srv, *self._conns]:
            try:
                s.shutdown(socket.SHUT_RDWR)   # wakes a thread blocked in recv on it; close alone does not
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(2)

    def _accept(self) -> None:
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._conns.append(c)
            self._spawn(self._serve, c)

    @staticmethod
    def _serve(c: socket.socket) -> None:
        buf = bytearray()
        try:
            while data := c.recv(65536):
                buf += data
                while (end := buf.find(b"\r\n\r\n")) >= 0:
                    del buf[:end + 4]
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n[]")
        except OSError:
            pass


@pytest.fixture
def rig():
    """make(mode, refuse_http) -> (upstream stand-in, proxy in front of it); both are closed when the test ends."""
    made: list = []

    def make(mode: str = "drop", refuse_http: bool = False, proxy_cls=_ChromeProxy):
        up = _Upstream()
        proxy = proxy_cls(up.port, mode, refuse_http=refuse_http)
        made.append((up, proxy))
        return up, proxy

    yield make
    for up, proxy in made:
        proxy.close()
        up.close()


def _http_get(s: socket.socket) -> bytes:
    """One GET /json over `s`: the bytes that came back (whole response, or until the connection ended or went quiet; b"" when refused)."""
    s.settimeout(2)
    got = b""
    try:
        s.sendall(b"GET /json HTTP/1.1\r\nHost: x\r\n\r\n")
        while not got.endswith(b"[]"):
            data = s.recv(65536)
            if not data:
                break
            got += data
    except socket.timeout:
        return got or b"(silence)"
    except OSError:
        pass
    return got


def _http_get_rest(s: socket.socket) -> bytes:
    """The response to a GET already sent on `s` (whole response, or until the connection ended or went quiet)."""
    s.settimeout(2)
    got = b""
    try:
        while not got.endswith(b"[]"):
            data = s.recv(65536)
            if not data:
                break
            got += data
    except OSError:
        pass
    return got


def _trip(proxy: _ChromeProxy) -> socket.socket:
    """Open a browser websocket through the proxy and send Target.getTargets as a masked frame; returns once the proxy has seen it."""
    b = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    b.sendall(b"GET /devtools/browser/x HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n\r\n")
    payload = json.dumps({"id": 1, "method": "Target.getTargets"}).encode()
    key = b"\x01\x02\x03\x04"
    b.sendall(bytes([0x81, 0x80 | len(payload)]) + key + bytes(x ^ key[i % 4] for i, x in enumerate(payload)))
    deadline = time.monotonic() + 5
    while not proxy.tripped and time.monotonic() < deadline:
        time.sleep(0.01)
    assert proxy.tripped, "the proxy never saw Target.getTargets"
    return b


def test_the_rig_refuses_an_http_client_that_connects_after_the_trip_without_asking_upstream(rig) -> None:
    up, proxy = rig(refuse_http=True)
    _trip(proxy)
    for _ in range(20):   # the old order forwarded the head upstream first, so a quick answer could win the race: every attempt must be refused
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as s:
            assert _http_get(s) == b""
    assert proxy.refused == 20, "the refusal path was not what ended the connections"
    assert proxy.http_log == [], "a request went upstream after the trip: the refusal came after the forward"


def test_the_rig_refuses_an_http_client_that_was_connected_before_the_trip_on_its_next_bytes(rig) -> None:
    up, proxy = rig(refuse_http=True)
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as s:
        assert b"200 OK" in _http_get(s)   # before the trip the listing answers, twice: the connection is alive and kept open
        assert b"200 OK" in _http_get(s)
        _trip(proxy)
        assert _http_get(s) == b""
    assert proxy.refused == 1
    assert [t for t, _ in proxy.http_log] == [False, False], "a request went upstream after the trip"


class _StalledForward(_ChromeProxy):
    """The send of a forwarded request stalls (an upstream that stopped reading) until `release` is set."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.entered, self.release = threading.Event(), threading.Event()

    def _send(self, up, payload, timeout: float = 5) -> None:
        self.entered.set()
        self.release.wait(10)
        super()._send(up, payload, timeout)


def test_a_forward_that_is_stalled_in_its_send_does_not_keep_the_trip_from_landing(rig) -> None:
    up, proxy = rig(refuse_http=True, proxy_cls=_StalledForward)
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as s:
        s.sendall(b"GET /json HTTP/1.1\r\nHost: x\r\n\r\n")
        assert proxy.entered.wait(5), "the request never reached the send"
        try:
            _trip(proxy)   # the send is in flight: the trip is recorded anyway (it used to queue on the lock the send held)
        finally:
            proxy.release.set()
        assert b"200 OK" in _http_get_rest(s), "the request decided before the trip was not answered"
    assert [t for t, _ in proxy.http_log] == [False], ("the decision made before the trip must keep its logged state", proxy.http_log)


def test_a_refused_client_that_resets_after_its_request_still_has_both_sockets_closed(rig) -> None:
    up, proxy = rig(refuse_http=True)
    _trip(proxy)
    c = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    c.sendall(b"GET /json HTTP/1.1\r\nHost: x\r\n\r\n")
    c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")   # close() sends a reset
    c.close()
    deadline = time.monotonic() + 5
    while proxy.refused < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert proxy.refused == 1
    # the proxy's upstream half is closed when it ends the refused connection: the stand-in sees the end of that connection and its
    # serving thread exits (the trip's own upstream half stays open by design, so the refused one is the thread that goes away)
    deadline = time.monotonic() + 5
    while sum(t.is_alive() for t in up._threads) > 1 and time.monotonic() < deadline:   # the trip's connection
        time.sleep(0.01)
    assert sum(t.is_alive() for t in up._threads) <= 1, "the refused connection's upstream half was left open"


def test_the_upstream_stand_in_closes_promptly_with_a_client_still_connected() -> None:
    up = _Upstream()
    c = socket.create_connection(("127.0.0.1", up.port), timeout=5)
    deadline = time.monotonic() + 5
    while not up._conns and time.monotonic() < deadline:
        time.sleep(0.01)
    assert up._conns
    t0 = time.monotonic()
    up.close()
    c.close()
    assert time.monotonic() - t0 < 1, "close() waited out a thread blocked in recv"


def test_the_upstream_stand_in_leaves_no_connection_open_or_thread_running_when_clients_connect_during_close() -> None:
    for _ in range(40):
        up = _Upstream()
        stop = threading.Event()
        clients: list[socket.socket] = []

        def flood() -> None:
            while not stop.is_set():
                try:
                    clients.append(socket.create_connection(("127.0.0.1", up.port), timeout=1))
                except OSError:
                    return
        t = threading.Thread(target=flood, daemon=True)
        t.start()
        time.sleep(0.02)
        t0 = time.monotonic()
        up.close()
        stop.set()
        t.join(5)
        for c in clients:
            c.close()
        assert time.monotonic() - t0 < 1.5, "close() waited out a late connection's thread"
        assert all(c.fileno() == -1 for c in up._conns), "a connection accepted during close was left open"
        assert not any(t.is_alive() for t in up._threads)


def test_the_rig_without_refuse_http_still_forwards_after_the_trip(rig) -> None:
    up, proxy = rig()
    _trip(proxy)
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as s:
        assert b"200 OK" in _http_get(s)
    assert proxy.refused == 0 and [t for t, _ in proxy.http_log] == [True]


@needs_browser
@pytest.mark.parametrize("mode", ["drop", "hold"])
def test_a_browser_connection_that_fails_during_a_hang_still_gets_the_target_listed_over_json(tmp_path: Path, shared_port: int, mode: str) -> None:
    # The page is hung and the browser connection to the shared Chrome then fails (closed, or silent and closed after the diagnostic gave
    # up on it). The browser still lists the page at /json, so the TARGET line must come from there; the protocol failure is worth
    # reporting only when /json cannot answer either.
    proxy = _ChromeProxy(shared_port, mode)
    steps = [{"eval": "location.hash = '#shared-hang-marker'; setTimeout(() => { for (;;) {} }, 50); 1"}, {"until": "false", "ms": 1500}]
    try:
        r = _drive(tmp_path, steps, {"LEVAIN_DRIVE_CDP_PORT": str(proxy.port), "LEVAIN_TEST_WAIT_SCALE": "1"}, timeout=90)
    finally:
        proxy.close()
    assert proxy.tripped, "the rig never saw Target.getTargets: the test would pass without exercising the failure"
    assert r.returncode == 1 and "until never came true within 1500ms" in r.stderr, (r.returncode, r.stderr)
    _, _, block = r.stderr.partition("--- page state at the timeout ---")
    assert "PAGE URL: ?" in block, r.stderr                                           # the page did not answer: the premise of the test
    assert re.search(r'^TARGET: listed, id=[0-9A-F]{32} type=page url="about:blank#shared-hang-marker"', block, re.M), r.stderr


# ---- a shared Chrome that fails during setup: the drive still runs, on its own Chrome, within a bound --------------------------

def _ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()


class _Fake(BaseHTTPRequestHandler):
    mode = "drop_upgrade"

    def log_message(self, *a):   # quiet
        pass

    def do_GET(self):
        upgrade = self.headers.get("Upgrade", "").lower() == "websocket"
        if not upgrade:
            body = json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{self.server.server_address[1]}/devtools/browser/x"}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if self.mode == "drop_upgrade":
            self.connection.close()
            return
        self.send_response(101); self.send_header("Upgrade", "websocket"); self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", _ws_accept(self.headers["Sec-WebSocket-Key"])); self.end_headers()
        self.wfile.flush()
        if self.mode == "close_after_handshake":
            self.connection.close()
        else:   # wedge: accepts and never answers
            time.sleep(30)


def _serve(mode: str):
    handler = type("H", (_Fake,), {"mode": mode})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _dead_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def _accept_then_close():
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(16)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            c.close()
    threading.Thread(target=loop, daemon=True).start()
    return srv


@needs_browser
@pytest.mark.parametrize("kind", ["dead_port", "accept_then_close", "drop_upgrade", "close_after_handshake", "wedge"])
def test_a_shared_chrome_that_fails_setup_falls_back_to_a_spawned_one_within_the_bound(tmp_path: Path, kind: str) -> None:
    servers = []
    if kind == "dead_port":
        port = _dead_port()
    elif kind == "accept_then_close":
        s = _accept_then_close(); servers.append(s); port = s.getsockname()[1]
    else:
        s = _serve(kind); servers.append(s); port = s.server_address[1]
    log = tmp_path / "launches.log"
    t0 = time.monotonic()
    try:
        r = _drive(tmp_path, [{"eval": "1 + 1"}], {"LEVAIN_DRIVE_CDP_PORT": str(port), "LEVAIN_DRIVE_LAUNCH_LOG": str(log)}, timeout=120)
    finally:
        for s in servers:
            s.close() if isinstance(s, socket.socket) else s.shutdown()
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert json.loads(r.stdout)["values"] == [2]
    assert "launching a Chrome of its own" in r.stderr, r.stderr
    assert log.read_text().split() == ["spawn"], log.read_text()   # the fallback is the driver's own launch
    assert elapsed < FALLBACK_BOUND_SECONDS, elapsed


# ---- a killed pytest leaves no shared Chrome and no profile ----------------------------------------------------------------------

CHILD = """
import json, os, sys, time
sys.path.insert(0, {root!r})
from tests import _shared_chrome
port = _shared_chrome.port()
print(json.dumps([port, _shared_chrome._profile, _shared_chrome._proc.pid if _shared_chrome._proc else None]), flush=True)
time.sleep(120)
"""


def _group_alive(pgid: int) -> bool:
    """Whether Chrome's process group (it leads its own session) has a member that is not a zombie: read by pid,
    never by a command line, which ps on Linux CI does not reliably carry; a zombie left under a PID 1 that does
    not reap is not a live Chrome."""
    from levain.firing.confinement import group_live_proc

    return group_live_proc(pgid)


def _start_child(tmp_path: Path):
    script = tmp_path / "child.py"
    script.write_text(CHILD.format(root=str(ROOT)))
    p = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True, cwd=ROOT,
                         env={**os.environ, "PYTHONPATH": str(ROOT)})
    raw = p.stdout.readline()
    try:
        port, profile, pid = json.loads(raw)
    except ValueError:
        p.kill(); p.wait()
        raise AssertionError(f"the child printed no startup record: {raw!r}") from None
    if port is None or pid is None:
        p.kill(); p.wait()
        pytest.skip("the shared Chrome could not start here")
    return p, profile, int(pid)


def _gone(profile: str, pgid: int, bound: float = 15.0) -> bool:
    end = time.monotonic() + bound
    while time.monotonic() < end:
        if not _group_alive(pgid) and not os.path.exists(profile):
            return True
        time.sleep(0.25)
    return False


@needs_browser
def test_a_sigkilled_pytest_leaves_no_shared_chrome_and_no_profile(tmp_path: Path) -> None:
    """The watchdog case: nothing in the dead process runs, yet within the bound no process names the profile and the directory is gone."""
    p, profile, pgid = _start_child(tmp_path)
    try:
        assert _group_alive(pgid), "the child's Chrome should be running before the kill"
        p.send_signal(signal.SIGKILL)
        p.wait()
        assert _gone(profile, pgid), "Chrome or its profile outlived the kill"
    finally:
        if p.poll() is None:
            p.kill(); p.wait()


@needs_browser
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_terminated_pytest_cleans_up_in_process(tmp_path: Path, sig: int) -> None:
    p, profile, pgid = _start_child(tmp_path)
    try:
        p.send_signal(sig)
        p.wait(timeout=20)
        assert p.returncode == -sig, p.returncode   # ended by the signal, as it would have without the handler
        assert _gone(profile, pgid, bound=5.0), "Chrome or its profile outlived the signal"
    finally:
        if p.poll() is None:
            p.kill(); p.wait()


def test_the_ping_treats_a_dropped_socket_as_not_healthy(tmp_path: Path) -> None:
    """A browser that completes the handshake and then drops the socket is not healthy (exit 1, not a hang)."""
    node = _page_drive_run.shutil.which("node")
    if not node:
        pytest.skip("node not found")
    srv = _serve("close_after_handshake")
    try:
        r = subprocess.run([node, str(_shared_chrome.PING), str(srv.server_address[1]), "2000"], capture_output=True, timeout=20)
    finally:
        srv.shutdown()
    assert r.returncode == 1


@pytest.mark.parametrize("flag", ["LEVAIN_FULL_CHROME", "LEVAIN_DRIVE_FULL_CHROME"])
def test_a_full_chrome_flag_never_starts_the_shared_chrome(monkeypatch, flag):
    """With the drive taking its own full Chrome, the runner must not start (and then discard) the session's shared Chrome."""
    calls = []
    monkeypatch.setattr(_shared_chrome, "port", lambda: calls.append(1) or 4242)
    monkeypatch.setenv("LEVAIN_DRIVE_SHARE", "1")
    monkeypatch.delenv("LEVAIN_DRIVE_CHROME_FLAGS", raising=False)
    monkeypatch.delenv("LEVAIN_FULL_CHROME", raising=False)
    monkeypatch.delenv("LEVAIN_DRIVE_FULL_CHROME", raising=False)
    argv = [NODE or "node", str(DRIVE)]
    assert _page_drive_run._shared_env(argv) == {"LEVAIN_DRIVE_CDP_PORT": "4242"} and calls == [1]   # the positive control: the seam works
    calls.clear()
    monkeypatch.setenv(flag, "1")
    assert _page_drive_run._shared_env(argv) == {} and calls == []


@needs_browser
def test_a_browser_connection_and_a_json_listing_that_both_fail_report_both(tmp_path: Path, shared_port: int) -> None:
    # The protocol failure is the word of last resort: when /json cannot answer either, the line names both, so a reader can tell a dead
    # browser from a dropped connection.
    proxy = _ChromeProxy(shared_port, "drop", refuse_http=True)
    steps = [{"eval": "location.hash = '#shared-hang-marker'; setTimeout(() => { for (;;) {} }, 50); 1"}, {"until": "false", "ms": 1500, "artifact": "1"}]
    try:
        r = _drive(tmp_path, steps, {"LEVAIN_DRIVE_CDP_PORT": str(proxy.port), "LEVAIN_TEST_WAIT_SCALE": "1"}, timeout=90)
    finally:
        proxy.close()
    assert proxy.tripped, "the rig never saw Target.getTargets: the test would pass without exercising the failure"
    assert r.returncode == 1 and "until never came true within 1500ms" in r.stderr, (r.returncode, r.stderr)
    _, _, block = r.stderr.partition("--- page state at the timeout ---")
    assert re.search(r"^TARGET: browser connection failed: .+; /json also failed \(browser unreachable: .+\)$", block, re.M), r.stderr
    # a step's own artifact read goes down with the same dropped connection: the block still carries its line, saying the read failed
    assert re.search(r"^STEP ARTIFACT: evaluation error: .+$", block, re.M), r.stderr
