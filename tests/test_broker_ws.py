"""The brokered websocket (S1 session 3b-7, design 1.3 step 7, 1.6 websocket, 1.7, DONE row 7): keys' upgrade request
and its check of the upstream's 101, the `U` / `D` relay, and the frame-aware scanner that closes the websocket on a
secret in a server frame. The keys cases drive the real `Keys._exchange` in-process against a scripted upstream (the
seam above `_connect`, as `test_broker_keys_upstream.py` does); the over-the-wire form of row 7 (real front and keys
processes, a TLS websocket upstream) is the lane's RUN harness."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import tempfile

import pytest

from levain.broker import creds as CR
from levain.broker import keys as K
from levain.broker import redact as RD
from levain.contracts import canon
from tests.test_broker_keys_upstream import SECRET, TOKEN, _Rig, _scripted, _types, _Up

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets")

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
KEY = base64.b64encode(bytes(range(16))).decode()


def _accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()


def _frame(opcode: int, payload: bytes, *, mask: bool = False, rsv: int = 0) -> bytes:
    n = len(payload)
    head = bytes([0x80 | rsv | opcode])
    m = 0x80 if mask else 0
    if n < 126:
        head += bytes([m | n])
    elif n < 65536:
        head += bytes([m | 126]) + struct.pack(">H", n)
    else:
        head += bytes([m | 127]) + struct.pack(">Q", n)
    if mask:
        k = b"\x01\x02\x03\x04"
        return head + k + bytes(b ^ k[i % 4] for i, b in enumerate(payload))
    return head + payload


def _lib(d: str, responses=("content-type", "sec-websocket-protocol")) -> None:
    os.makedirs(os.path.join(d, "routes"))
    os.makedirs(os.path.join(d, "keys", "cred"))
    route = {"route": "r", "type": "brokered",
             "upstream": {"scheme": "https", "host": "api.example.com", "base_path": ""}, "credential": "c1",
             "requests": {"templates": [{"method": "GET", "path": "/ws", "upgrade": "websocket"},
                                        {"method": "GET", "path": "/plain"}],
                          "headers": {"sec-websocket-key": "[A-Za-z0-9+/]{22}==", "sec-websocket-version": "[0-9]+",
                                      "sec-websocket-protocol": "[a-z0-9.,-]+"},
                          "body_cap": 4096, "body_codec": "none"},
             "responses": {"headers": list(responses)},
             "max_call_microdollars": 1000, "max_session_microdollars": 100000}
    with open(os.path.join(d, "routes", "base.json"), "wb") as f:
        f.write(canon.canonical_bytes({"format": K.BASE_MAP_FORMAT, "routes": [route]}))
    with open(os.path.join(d, "keys", "cred", "c1.json"), "w") as f:
        f.write(json.dumps({"format": CR.FORMAT, "name": "c1", "rev": "0" * 32, "hosts": ["api.example.com"],
                            "inject": {"header": "x-api-key", "scheme": ""}, "kind": "static", "secret": SECRET}))


def _q(path: str = "/ws", headers=None, upgrade: str = "websocket") -> dict:
    hs = [["sec-websocket-key", KEY], ["sec-websocket-version", "13"]] if headers is None else headers
    return {"v": 1, "token": TOKEN, "route": "r", "method": "GET", "path": path, "query": "", "headers": hs,
            "body": "none", "upgrade": upgrade}


def _h101(key: str = KEY, extra: bytes = b"") -> bytes:
    return (b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\nconnection: Upgrade\r\n"
            b"sec-websocket-accept: " + _accept(key).encode() + b"\r\n" + extra + b"\r\n")


def _run(coro_fn, **lib):
    d = tempfile.mkdtemp(prefix="lws", dir="/tmp")
    _lib(d, **lib)
    try:
        return asyncio.run(asyncio.wait_for(coro_fn(d), 20))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _exchange(monkeypatch, q: dict, script, front_frames: bytes, *, eof: bool = True, lib=None):
    """Run one exchange: `script(up)` feeds the upstream, `front_frames` are the front's frames after `Q`, then the
    front's EOF unless `eof` is False (a front that stays open, so only the upstream can end the websocket). `lib`:
    `_lib`'s keywords (the route's response allow-list)."""
    up = _Up()
    _scripted(monkeypatch, up)

    async def go(d: str) -> tuple:
        async with _Rig(d) as rig:
            rig.front.feed_data(front_frames)
            if eof:
                rig.front.feed_eof()
            script(up)
            await asyncio.wait_for(rig.run(q), 10)
            return await rig.got(), b"".join(up.writes)
    return _run(go, **(lib or {}))


def _gw(t: bytes, payload: bytes = b"") -> bytes:
    return t + len(payload).to_bytes(4, "big") + payload


# --- row 7: a websocket frame passes ----------------------------------------------------------------------------

def test_websocket_upgraded_frames(monkeypatch):
    """T:3b/test_websocket_upgraded_frames: keys asks the upstream to upgrade with the slot's key and no extension,
    checks the 101's accept, answers `U`, relays the server's frame (sent in the 101's own read: h11's trailing
    data) to the front as `D`, and the client's masked frame upstream unchanged."""
    client = _frame(1, b"hello", mask=True)
    server = _frame(1, b"hello")

    def script(up):
        up.feed(_h101() + server)

    frames, sent = _exchange(monkeypatch, _q(), script, _gw(b"E") + _gw(b"D", client) + _gw(b"E"))
    assert _types(frames) == b"AUD", frames
    assert frames[2][1] == server
    head, _, rest = sent.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    assert lines[0] == b"GET /ws HTTP/1.1"
    assert b"connection: upgrade" in lines and b"upgrade: websocket" in lines
    assert b"sec-websocket-key: " + KEY.encode() in lines and b"sec-websocket-version: 13" in lines
    assert not any(x.lower().startswith(b"sec-websocket-extensions") for x in lines)
    assert b"x-api-key: " + SECRET.encode() in lines
    assert rest == client   # the client's frame, masked, byte for byte


def test_a_secret_in_one_server_frame_split_across_two_reads_closes_the_websocket(monkeypatch):
    """r1-18: the credential echoed in ONE frame split across two upstream reads: the frame header goes, the
    held secret bytes never do, keys sends `E` and closes."""
    frame = _frame(1, SECRET.encode())
    at = len(frame) // 2

    def script(up):
        up.feed(_h101())
        up.feed(frame[:at])
        up.feed(frame[at:])

    frames, _sent = _exchange(monkeypatch, _q(), script, _gw(b"E"), eof=False)
    assert _types(frames) == b"AUDE", frames
    down = b"".join(p for t, p in frames if t == b"D")
    assert down == frame[:2]
    assert not any(SECRET.encode()[:k] in down for k in range(1, len(SECRET) + 1))


def test_a_wrong_accept_is_upstream_error_and_never_a_U(monkeypatch):
    frames, _ = _exchange(monkeypatch, _q(), lambda up: up.feed(_h101(key=KEY + "x")), _gw(b"E"))
    assert _types(frames) == b"AF", frames
    assert json.loads(frames[1][1]) == {"refused": "upstream-error"}


@pytest.mark.parametrize("extra", [b"sec-websocket-extensions: permessage-deflate\r\n",
                                   b"sec-websocket-protocol: other\r\n"])
def test_a_101_with_an_unoffered_extension_or_protocol_is_upstream_error(monkeypatch, extra):
    frames, _ = _exchange(monkeypatch, _q(), lambda up: up.feed(_h101(extra=extra)), _gw(b"E"))
    assert _types(frames) == b"AF", frames


def test_an_offered_protocol_travels_in_U(monkeypatch):
    q = _q(headers=[["sec-websocket-key", KEY], ["sec-websocket-version", "13"], ["sec-websocket-protocol", "chat,v2"]])
    frames, _ = _exchange(monkeypatch, q, lambda up: up.feed(_h101(extra=b"sec-websocket-protocol: v2\r\n")),
                          _gw(b"E") + _gw(b"E"))
    assert _types(frames) == b"AU", frames
    assert json.loads(frames[1][1])["headers"] == [["sec-websocket-protocol", "v2"]]


def test_a_non_101_answer_to_an_upgrade_is_an_ordinary_response(monkeypatch):
    frames, _ = _exchange(monkeypatch, _q(),
                          lambda up: up.feed(b"HTTP/1.1 426 Upgrade Required\r\ncontent-length: 2\r\n\r\nno"),
                          _gw(b"E"))
    assert _types(frames) == b"ARbe", frames
    assert json.loads(frames[1][1])["status"] == 426


def test_a_masked_server_frame_closes_the_websocket(monkeypatch):
    frames, _ = _exchange(monkeypatch, _q(), lambda up: up.feed(_h101() + _frame(1, b"x", mask=True)), _gw(b"E"),
                          eof=False)
    assert _types(frames) == b"AUE", frames


# --- L3 r1 (3b7/L3_r1_TRIAGE.md T1-T4, T7) ----------------------------------------------------------------------

_CHAT = [["sec-websocket-key", KEY], ["sec-websocket-version", "13"], ["sec-websocket-protocol", "chat"]]


def test_a_101_before_the_front_E_waits_for_that_E(monkeypatch):
    """T1 (codex HIGH = complement MED): the upstream's 101 is read BEFORE the front's request-ending `E`. The 101
    must not cancel the upload: keys waits for that `E`, then answers `U`, and the client's next frame goes up. Before
    the fix the request's `E` was read as the websocket's own end, and the client's frame never left keys."""
    client = _frame(1, b"hello", mask=True)
    up = _Up()
    _scripted(monkeypatch, up)

    async def go(d: str) -> tuple:
        async with _Rig(d) as rig:
            up.feed(_h101())
            task = rig.run(_q())
            await asyncio.wait_for(up.read_called.wait(), 5)
            await asyncio.sleep(0.05)   # the 101 is parsed, and the exchange has moved on from it, before the `E`
            assert not task.done()
            rig.front.feed_data(_gw(b"E") + _gw(b"D", client) + _gw(b"E"))
            rig.front.feed_eof()
            await asyncio.wait_for(task, 10)
            return await rig.got(), b"".join(up.writes)

    frames, sent = _run(go)
    assert _types(frames) == b"AU", frames
    assert sent.partition(b"\r\n\r\n")[2] == client


def test_a_101_whose_request_never_ends_is_refused_at_the_head_clock(monkeypatch):
    """T1's bound: a 101 arrives and the front's `E` never does: `upstream-error` once `head_timeout_s` passes, and
    never a `U`. The front closes only after that (a front half-closes on reading `F`, which ends keys' drain)."""
    up = _Up()
    _scripted(monkeypatch, up)

    async def go(d: str) -> tuple:
        async with _Rig(d, head_timeout_s=0.2) as rig:
            up.feed(_h101())
            asyncio.get_running_loop().call_later(0.6, rig.front.feed_eof)
            await asyncio.wait_for(rig.run(_q()), 10)
            return await rig.got(), b"".join(up.writes)

    frames, _sent = _run(go)
    assert _types(frames) == b"AF", frames
    assert json.loads(frames[1][1]) == {"refused": "upstream-error"}


def test_mixed_case_handshake_names_upgrade(monkeypatch):
    """T2 (complement MED-LOW): the handshake is read once from the re-checked (lowercased) headers. Before the fix a
    `Sec-WebSocket-Key` passed keys' check, went upstream, and then the 101 was refused `malformed`."""
    q = _q(headers=[["Sec-WebSocket-Key", KEY], ["Sec-WebSocket-Version", "13"], ["Sec-WebSocket-Protocol", "chat"]])
    frames, _ = _exchange(monkeypatch, q, lambda up: up.feed(_h101(extra=b"sec-websocket-protocol: chat\r\n")),
                          _gw(b"E") + _gw(b"E"))
    assert _types(frames) == b"AU", frames
    assert json.loads(frames[1][1])["headers"] == [["sec-websocket-protocol", "chat"]]


def test_a_selected_protocol_the_route_cannot_relay_is_upstream_error(monkeypatch):
    """T3 (codex MED): a route whose `responses.headers` omits `sec-websocket-protocol` would drop the upstream's
    selection from `U`, and the client and the upstream would disagree on the protocol: refused instead."""
    frames, _ = _exchange(monkeypatch, _q(headers=_CHAT),
                          lambda up: up.feed(_h101(extra=b"sec-websocket-protocol: chat\r\n")), _gw(b"E"),
                          lib={"responses": ("content-type",)})
    assert _types(frames) == b"AF", frames
    assert json.loads(frames[1][1]) == {"refused": "upstream-error"}


def test_no_selected_protocol_upgrades_on_a_route_that_relays_none(monkeypatch):
    """T3's other half: the same route, the upstream selects nothing: nothing to disagree on, so `U`."""
    frames, _ = _exchange(monkeypatch, _q(headers=_CHAT), lambda up: up.feed(_h101()), _gw(b"E") + _gw(b"E"),
                          lib={"responses": ("content-type",)})
    assert _types(frames) == b"AU", frames
    assert json.loads(frames[1][1])["headers"] == []


def test_an_http_1_0_101_is_upstream_error(monkeypatch):
    """T4 (codex LOW): h11 takes an `HTTP/1.0 101` as a switch; RFC 6455 §4.1 needs 1.1."""
    frames, _ = _exchange(monkeypatch, _q(), lambda up: up.feed(_h101().replace(b"HTTP/1.1", b"HTTP/1.0", 1)),
                          _gw(b"E"))
    assert _types(frames) == b"AF", frames
    assert json.loads(frames[1][1]) == {"refused": "upstream-error"}


@pytest.mark.parametrize("q,why", [
    (_q(path="/plain"), "upgrade on a template without upgrade"),
    (_q(headers=[["sec-websocket-version", "13"]]), "no key"),
    (_q(headers=[["sec-websocket-key", "A" * 21 + "B=="], ["sec-websocket-version", "13"]]), "a non-canonical key"),
    (_q(headers=[["sec-websocket-key", KEY], ["sec-websocket-version", "8"]]), "version 8"),
])
def test_keys_refuses_a_websocket_Q_it_did_not_check_itself(monkeypatch, q, why):
    frames, sent = _exchange(monkeypatch, q, lambda up: None, _gw(b"E"))
    assert _types(frames) == b"F", (why, frames)
    assert json.loads(frames[0][1]) == {"refused": "malformed"}, why
    assert sent == b"", why


# --- the frame-aware scanner ----------------------------------------------------------------------------------

S = b"sk-ws-secret-0123456789"


def _fs() -> RD.FrameScanner:
    return RD.Scanner([S]).frames()


def test_a_frame_ending_in_a_secret_prefix_is_released_at_its_own_end():
    """Per-frame hold-back: bytes that could start a secret wait only until their frame ends."""
    f = _frame(1, b"data " + S[:6])
    out, end = _fs().feed(f)
    assert (out, end) == (f, None)


def test_a_secret_split_across_two_frames_is_not_caught():
    """The canary's stated edge (design 1.7): the hold-back never crosses a frame boundary."""
    f = _frame(1, S[:10], ) + _frame(0, S[10:])
    out, end = _fs().feed(f)
    assert (out, end) == (f, None)


def test_byte_at_a_time_matches_the_whole_feed():
    stream = _frame(1, b"a" * 200) + _frame(2, b"b" * 70000) + _frame(9, b"") + _frame(1, b"x" + S + b"y")
    whole_out, whole_end = _fs().feed(stream)
    fs = _fs()
    parts, end = [], None
    for i in range(len(stream)):
        o, e = fs.feed(stream[i:i + 1])
        parts.append(o)
        end = end or e
    assert (b"".join(parts), end) == (whole_out, whole_end)
    assert whole_end == RD.REDACTED and whole_out.endswith(_frame(1, b"x" + S + b"y")[:2] + b"x")


@pytest.mark.parametrize("frame", [_frame(1, b"x", rsv=0x40), _frame(1, b"x", mask=True),
                                   b"\x82\x7f" + struct.pack(">Q", 1 << 63)])
def test_a_frame_keys_does_not_relay_ends_the_stream_before_its_header(frame):
    sc = RD.Scanner([S])
    out, end = sc.frames().feed(_frame(1, b"ok") + frame)
    assert (out, end) == (_frame(1, b"ok"), RD.BAD_FRAME)
    assert sc.count == 0


@pytest.mark.parametrize("opcode", [3, 4, 5, 6, 7, 0xB, 0xC, 0xD, 0xE, 0xF])
def test_a_reserved_server_opcode_is_a_bad_frame(opcode):
    """T5 (ruling W2, RFC 6455 §5.2: an unknown opcode MUST fail the connection): the frame's header never goes."""
    out, end = _fs().feed(_frame(1, b"ok") + _frame(opcode, b"x"))
    assert (out, end) == (_frame(1, b"ok"), RD.BAD_FRAME)


@pytest.mark.parametrize("opcode", [0, 1, 2, 8, 9, 0xA])
def test_a_defined_server_opcode_passes(opcode):
    f = _frame(opcode, b"x")
    assert _fs().feed(f) == (f, None)


def test_a_match_counts_in_the_owning_scanner():
    sc = RD.Scanner([S])
    fs = sc.frames()
    out, end = fs.feed(_frame(1, S))
    assert (end, sc.count) == (RD.REDACTED, 1)
    assert fs.feed(b"more") == (b"", None)
