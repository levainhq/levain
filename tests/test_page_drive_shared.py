"""The shared session Chrome and the driver's use of it: the same viewport as the driver's own Chrome, a setup failure falls
back to a Chrome of its own (never a hang, never a failure), and a killed pytest leaves neither the Chrome nor its profile."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
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
        self._socks: list[socket.socket] = []
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        for s in [self._srv, *self._socks]:
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
                    up.sendall(bytes(head[:end + 4]))
                    data = bytes(head[end + 4:])
                if not browser:
                    if self.tripped and self.refuse_http:
                        c.shutdown(socket.SHUT_RDWR)
                        return
                    up.sendall(data)
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
    steps = [{"eval": "location.hash = '#shared-hang-marker'; setTimeout(() => { for (;;) {} }, 50); 1"}, {"until": "false", "ms": 1500}]
    try:
        r = _drive(tmp_path, steps, {"LEVAIN_DRIVE_CDP_PORT": str(proxy.port), "LEVAIN_TEST_WAIT_SCALE": "1"}, timeout=90)
    finally:
        proxy.close()
    assert proxy.tripped, "the rig never saw Target.getTargets: the test would pass without exercising the failure"
    assert r.returncode == 1 and "until never came true within 1500ms" in r.stderr, (r.returncode, r.stderr)
    _, _, block = r.stderr.partition("--- page state at the timeout ---")
    assert re.search(r"^TARGET: browser connection failed: .+; /json also failed \(browser unreachable: .+\)$", block, re.M), r.stderr
