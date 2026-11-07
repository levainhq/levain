"""levain.voice.wsclient against an in-process fake server on a loopback socket (plain ws:// only via the test flag)."""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
import time

import pytest

from levain.voice import wsclient as ws

SECRET = "Token sk-SECRET-VALUE-123"


class Peer:
    """The server side of one accepted connection."""

    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.buf = b""
        self.request = b""

    def read_request(self) -> None:
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.conn.recv(4096)
        self.request, _, self.buf = self.buf.partition(b"\r\n\r\n")

    def header(self, name: str) -> str | None:
        for ln in self.request.decode("latin-1").split("\r\n")[1:]:
            k, _, v = ln.partition(":")
            if k.lower() == name.lower():
                return v.strip()
        return None

    def accept_key(self) -> str:
        return base64.b64encode(hashlib.sha1((self.header("Sec-WebSocket-Key") + ws.GUID).encode()).digest()).decode()

    def handshake(self, *, accept: str | None = None, status: str = "101 Switching Protocols", extra: str = "") -> None:
        accept = accept or self.accept_key()
        self.conn.sendall(
            (f"HTTP/1.1 {status}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n"
             f"{extra}\r\n").encode())

    def raw(self, data: bytes) -> None:
        self.conn.sendall(data)

    def send(self, op: int, payload: bytes = b"", fin: bool = True, rsv: int = 0, mask: bool = False) -> None:
        n = len(payload)
        b0 = (0x80 if fin else 0) | rsv | op
        mbit = 0x80 if mask else 0
        if n < 126:
            head = struct.pack("!BB", b0, mbit | n)
        elif n < 65536:
            head = struct.pack("!BBH", b0, mbit | 126, n)
        else:
            head = struct.pack("!BBQ", b0, mbit | 127, n)
        if mask:
            head += b"\x01\x02\x03\x04"
            payload = bytes(c ^ b"\x01\x02\x03\x04"[i % 4] for i, c in enumerate(payload))
        self.conn.sendall(head + payload)

    def _need(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.conn.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_frame(self) -> tuple[bool, int, bool, bytes, bytes]:
        b0, b1 = self._need(2)
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", self._need(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", self._need(8))
        masked = bool(b1 & 0x80)
        key = self._need(4) if masked else b""
        wire = self._need(n)
        data = bytes(c ^ key[i % 4] for i, c in enumerate(wire)) if masked else wire
        return bool(b0 & 0x80), b0 & 0x0F, masked, data, key


class Server:
    def __init__(self, script) -> None:
        self.script = script
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.error: BaseException | None = None
        self.peer: Peer | None = None
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self) -> None:
        conn = None
        try:
            conn, _ = self.sock.accept()
            conn.settimeout(10)
            self.peer = Peer(conn)
            self.peer.read_request()
            self.script(self.peer)
        except (EOFError, OSError):
            pass
        except BaseException as e:  # noqa: BLE001
            self.error = e
        finally:
            if conn:
                try:
                    conn.close()
                except OSError:
                    pass

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/listen?x=1"

    def stop(self) -> None:
        self.sock.close()
        self.t.join(5)


@pytest.fixture
def serve():
    servers = []

    def make(script):
        s = Server(script)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.stop()
        assert s.error is None


def dial(srv, **kw):
    kw.setdefault("insecure_for_test", True)
    kw.setdefault("idle_timeout", 3.0)
    return ws.connect(srv.url, {"Authorization": SECRET}, **kw)


# ---- handshake

def test_refuses_plain_ws_by_default(serve):
    srv = serve(lambda p: None)
    with pytest.raises(ws.WsError) as e:
        ws.connect(srv.url, {})
    assert e.value.code == "url"


@pytest.mark.parametrize("url", ["http://x/", "wss://", "wss://u:p@host/", "wss://host/#f", "wss://host/a b", "ftp://h/"])
def test_bad_urls(url):
    with pytest.raises(ws.WsError) as e:
        ws.connect(url, {})
    assert e.value.code == "url"


def test_host_pin():
    with pytest.raises(ws.WsError) as e:
        ws.connect("wss://evil.example/", {}, allowed_hosts={"api.deepgram.com"})
    assert e.value.code == "url"


@pytest.mark.parametrize("hdr", [{"A\r\nB": "x"}, {"A": "x\r\nInjected: 1"}, {"A": "x\x00"}, {"Host": "evil"},
                                 {"Sec-WebSocket-Key": "k"}, {"Upgrade": "x"}, {"bad name": "x"}])
def test_bad_headers_refused_without_echo(hdr):
    with pytest.raises(ws.WsError) as e:
        ws.connect("wss://api.deepgram.com/", hdr)
    assert e.value.code == "header"
    assert "Injected" not in str(e.value)


def test_handshake_sends_header_and_path(serve):
    seen = {}

    def script(p):
        seen["auth"] = p.header("Authorization")
        seen["req"] = p.request.split(b"\r\n")[0]
        seen["host"] = p.header("Host")
        seen["ver"] = p.header("Sec-WebSocket-Version")
        p.handshake()
        p.read_frame()

    srv = serve(script)
    c = dial(srv)
    c.close(wait=0.1)
    srv.t.join(5)
    assert seen["auth"] == SECRET
    assert seen["req"] == b"GET /v1/listen?x=1 HTTP/1.1"
    assert seen["host"] == f"127.0.0.1:{srv.port}"
    assert seen["ver"] == "13"


def test_wrong_accept_refused(serve):
    srv = serve(lambda p: p.handshake(accept="AAAAAAAAAAAAAAAAAAAAAAAAAAA="))
    with pytest.raises(ws.WsError) as e:
        dial(srv)
    assert e.value.code == "handshake"


@pytest.mark.parametrize("status", ["200 OK", "301 Moved Permanently", "302 Found", "401 Unauthorized", "400 Bad Request"])
def test_non_101_refused_and_redirect_not_followed(serve, status):
    other = serve(lambda p: pytest.fail("a redirect was followed"))

    def script(p):
        p.handshake(status=status, extra=f"Location: ws://127.0.0.1:{other.port}/\r\n")

    srv = serve(script)
    with pytest.raises(ws.WsError) as e:
        dial(srv)
    assert e.value.code == "handshake"
    assert other.peer is None
    other.sock.close()


def test_extension_or_protocol_refused(serve):
    srv = serve(lambda p: p.handshake(extra="Sec-WebSocket-Extensions: permessage-deflate\r\n"))
    with pytest.raises(ws.WsError):
        dial(srv)


def test_missing_upgrade_headers_refused(serve):
    def script(p):
        p.raw(f"HTTP/1.1 101 x\r\nSec-WebSocket-Accept: {p.accept_key()}\r\n\r\n".encode())

    with pytest.raises(ws.WsError):
        dial(serve(script))


def test_handshake_deadline(serve):
    def script(p):
        time.sleep(1.0)

    t0 = time.monotonic()
    with pytest.raises(ws.WsError) as e:
        dial(serve(script), handshake_deadline=0.2)
    assert e.value.code == "timeout"
    assert time.monotonic() - t0 < 0.9


def test_handshake_oversize_head_refused(serve):
    def script(p):
        p.raw(b"HTTP/1.1 101 x\r\n" + b"X-A: " + b"a" * (ws.MAX_HANDSHAKE + 100) + b"\r\n")
        time.sleep(0.5)

    with pytest.raises(ws.WsError) as e:
        dial(serve(script))
    assert e.value.code == "handshake"


def test_connect_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(ws.WsError) as e:
        ws.connect(f"ws://127.0.0.1:{port}/", {}, insecure_for_test=True, connect_timeout=1)
    assert e.value.code == "connect"


# ---- sending

def test_client_frames_masked_and_roundtrip(serve):
    got = []

    def script(p):
        p.handshake()
        for _ in range(3):
            got.append(p.read_frame())

    srv = serve(script)
    c = dial(srv)
    big = bytes(range(256)) * 300      # 76800 bytes: 8-byte length form
    c.send_binary(b"\x00\x01\x02hello")
    c.send_text("héllo")
    c.send_binary(big)
    srv.t.join(5)
    c.close(wait=0.1)
    assert [(g[1], g[2], g[3]) for g in got] == [(2, True, b"\x00\x01\x02hello"), (1, True, "héllo".encode()),
                                                  (2, True, big)]
    assert all(g[0] for g in got)
    assert all(len(g[4]) == 4 for g in got)


def test_mask_key_fresh_each_frame(serve):
    keys = []

    def script(p):
        p.handshake()
        for _ in range(4):
            keys.append(p.read_frame()[4])

    srv = serve(script)
    c = dial(srv)
    for _ in range(4):
        c.send_binary(b"x" * 10)
    srv.t.join(5)
    c.close(wait=0.1)
    assert len(set(keys)) == 4


def test_mask_helper_matches_naive():
    for n in range(0, 40):
        data = bytes(range(n))
        key = b"\xde\xad\xbe\xef"
        assert ws._mask(data, key) == bytes(c ^ key[i % 4] for i, c in enumerate(data))


def test_send_oversize_refused(serve):
    srv = serve(lambda p: p.handshake())
    c = dial(srv, max_message=100)
    with pytest.raises(ws.WsError) as e:
        c.send_binary(b"x" * 101)
    assert e.value.code == "too_big"
    c.close(wait=0.1)


def test_send_after_close_refused(serve):
    srv = serve(lambda p: p.handshake())
    c = dial(srv)
    c.close(wait=0.1)
    with pytest.raises(ws.WsError) as e:
        c.send_text("x")
    assert e.value.code == "closed"
    c.close()   # idempotent


# ---- receiving

def test_recv_text_and_binary(serve):
    def script(p):
        p.handshake()
        p.send(1, b'{"a": 1}')
        p.send(2, b"\xff\x00")

    c = dial(serve(script))
    assert c.recv() == ("text", '{"a": 1}')
    assert c.recv() == ("binary", b"\xff\x00")
    c.close(wait=0.1)


def test_fragmented_reassembly_with_interleaved_ping(serve):
    pong = []

    def script(p):
        p.handshake()
        p.send(1, "hel".encode(), fin=False)
        p.send(0, "lo ".encode(), fin=False)
        p.send(9, b"pp")
        p.send(0, "wörld".encode(), fin=True)
        pong.append(p.read_frame())

    srv = serve(script)
    c = dial(srv)
    assert c.recv() == ("text", "hello wörld")
    srv.t.join(5)
    c.close(wait=0.1)
    fin, op, masked, data, _ = pong[0]
    assert (fin, op, masked, data) == (True, 10, True, b"pp")


def test_split_multibyte_across_fragments(serve):
    raw = "é".encode()

    def script(p):
        p.handshake()
        p.send(1, raw[:1], fin=False)
        p.send(0, raw[1:])

    c = dial(serve(script))
    assert c.recv() == ("text", "é")
    c.close(wait=0.1)


def test_unsolicited_pong_swallowed(serve):
    def script(p):
        p.handshake()
        p.send(10, b"zz")
        p.send(1, b"ok")

    c = dial(serve(script))
    assert c.recv() == ("text", "ok")
    c.close(wait=0.1)


def test_byte_at_a_time_delivery(serve):
    def script(p):
        p.handshake()
        frame = struct.pack("!BB", 0x81, 5) + b"slowk"
        for i in range(len(frame)):
            p.raw(frame[i:i + 1])
            time.sleep(0.01)

    c = dial(serve(script))
    assert c.recv() == ("text", "slowk")
    c.close(wait=0.1)


def test_frame_right_after_handshake_in_same_segment(serve):
    def script(p):
        accept = p.accept_key()
        p.raw((f"HTTP/1.1 101 x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n")
              .encode() + struct.pack("!BB", 0x81, 2) + b"hi")

    c = dial(serve(script))
    assert c.recv() == ("text", "hi")
    c.close(wait=0.1)


def test_oversize_refused_from_declared_length(serve):
    sent = []

    def script(p):
        p.handshake()
        p.raw(struct.pack("!BBQ", 0x82, 127, 10_000_000))     # declares 10 MB, sends none
        sent.append(p.read_frame())

    srv = serve(script)
    c = dial(srv, max_message=1000)
    with pytest.raises(ws.WsError) as e:
        c.recv()
    assert e.value.code == "too_big"
    srv.t.join(5)
    fin, op, masked, data, _ = sent[0]
    assert op == 8 and struct.unpack("!H", data[:2])[0] == 1009


def test_oversize_by_fragments_refused(serve):
    def script(p):
        p.handshake()
        p.send(2, b"a" * 60, fin=False)
        p.send(0, b"a" * 60, fin=False)
        p.send(0, b"a" * 60)

    c = dial(serve(script), max_message=100)
    with pytest.raises(ws.WsError) as e:
        c.recv()
    assert e.value.code == "too_big"


@pytest.mark.parametrize("frame", [
    struct.pack("!BB", 0x81, 0x80 | 1) + b"\0\0\0\0x",      # masked server frame
    struct.pack("!BB", 0xC1, 1) + b"x",                      # RSV1
    struct.pack("!BB", 0x83, 0),                             # reserved opcode
    struct.pack("!BB", 0x09, 0),                             # fragmented ping
    struct.pack("!BBH", 0x89, 126, 200) + b"x" * 200,        # oversize control
    struct.pack("!BB", 0x80, 1) + b"x",                      # continuation without start
    struct.pack("!BB", 0x81, 2) + b"\xff\xfe",               # bad UTF-8
    struct.pack("!BB", 0x88, 1) + b"x",                      # close with 1-byte body
    struct.pack("!BB", 0x88, 2) + struct.pack("!H", 1005),   # invalid close code on the wire
])
def test_protocol_violations(serve, frame):
    def script(p):
        p.handshake()
        p.raw(frame)
        time.sleep(0.3)

    c = dial(serve(script))
    with pytest.raises(ws.WsError) as e:
        c.recv()
    assert e.value.code == "protocol"
    with pytest.raises(ws.WsError):
        c.recv()


def test_new_data_frame_inside_fragmented_message_refused(serve):
    def script(p):
        p.handshake()
        p.send(1, b"a", fin=False)
        p.send(1, b"b")
        time.sleep(0.3)

    c = dial(serve(script))
    with pytest.raises(ws.WsError) as e:
        c.recv()
    assert e.value.code == "protocol"


# ---- close + timeouts

def test_server_initiated_close_is_echoed(serve):
    echoed = []

    def script(p):
        p.handshake()
        p.send(8, struct.pack("!H", 1001) + b"bye")
        echoed.append(p.read_frame())

    srv = serve(script)
    c = dial(srv)
    assert c.recv() == ("close", (1001, "bye"))
    srv.t.join(5)
    assert echoed[0][1] == 8 and struct.unpack("!H", echoed[0][3][:2])[0] == 1001
    with pytest.raises(ws.WsError):
        c.recv()


def test_client_initiated_close_handshake(serve):
    seen = []

    def script(p):
        p.handshake()
        fin, op, masked, data, _ = p.read_frame()
        seen.append((op, masked, struct.unpack("!H", data[:2])[0]))
        p.send(8, data[:2])
        time.sleep(0.3)

    srv = serve(script)
    c = dial(srv)
    t0 = time.monotonic()
    c.close(1000, wait=2.0)
    assert time.monotonic() - t0 < 1.5
    assert seen == [(8, True, 1000)]
    assert c._closed


def test_close_gives_up_when_peer_silent(serve):
    def script(p):
        p.handshake()
        time.sleep(1.5)

    c = dial(serve(script))
    t0 = time.monotonic()
    c.close(wait=0.3)
    assert time.monotonic() - t0 < 1.0
    assert c._closed


def test_recv_idle_timeout_is_recoverable(serve):
    def script(p):
        p.handshake()
        time.sleep(0.5)
        p.send(1, b"late")

    c = dial(serve(script), idle_timeout=0.2)
    with pytest.raises(ws.WsTimeout):
        c.recv()
    while True:
        try:
            assert c.recv() == ("text", "late")
            break
        except ws.WsTimeout:
            continue
    c.close(wait=0.1)


def test_timeout_mid_frame_closes(serve):
    def script(p):
        p.handshake()
        p.raw(struct.pack("!BB", 0x81, 10) + b"abc")
        time.sleep(1.0)

    c = dial(serve(script), idle_timeout=0.2)
    with pytest.raises(ws.WsError) as e:
        c.recv()
    assert e.value.code == "timeout" and not isinstance(e.value, ws.WsTimeout)
    with pytest.raises(ws.WsError):
        c.recv()


def test_peer_eof_is_closed(serve):
    def script(p):
        p.handshake()

    c = dial(serve(script))
    with pytest.raises(ws.WsError) as e:
        c.recv()
    assert e.value.code == "closed"


# ---- no leaks

def test_no_header_value_in_any_error_or_repr(serve):
    srv = serve(lambda p: p.handshake(status="401 Unauthorized"))
    with pytest.raises(ws.WsError) as e:
        dial(srv)
    assert "SECRET" not in str(e.value) and "SECRET" not in repr(e.value)
    srv2 = serve(lambda p: p.handshake())
    c = dial(srv2)
    assert "SECRET" not in repr(c) and "SECRET" not in str(vars(c).keys())
    assert not any(isinstance(v, str) and "SECRET" in v for v in vars(c).values())
    c.close(wait=0.1)
