"""Voice V1-4: the premium voice routes through the real ``levain serve`` envelope (design
``project_memory/1105-7/DESIGN_voice_v14.md`` section 6).

The rig is the room's (``tests/test_cockpit_room.py``'s World over a real CoordStore, served by ``make_server`` with the
K2a write token) plus a REAL voice-key record signed into the broker's own data root by an ssh-keygen laptop key, and a
FAKE ``security`` (``tests/test_voice_custody.py``'s stand-in: ``custody.checked_security`` refuses the real binary under
pytest). The providers' HTTP is a fake transport set on the server (``voice_transport``): no test reaches a network or a
person's keychain. ``VALUE`` is a key no code in the repo spells, so finding it anywhere is this file's leak.

Each test names, in its docstring, the line whose revert turns it red (the lane's RETURN records each run)."""
from __future__ import annotations

import ast
import hashlib
import http.client
import json
import logging
import socket
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("cryptography")

import levain.authority as auth  # noqa: E402
from levain.authority import ReplayCache, anchor_path  # noqa: E402
from levain.chat_egress.key_setup import KeychainItems  # noqa: E402
from levain.chat_egress.keys import KeychainStore  # noqa: E402
from levain.cockpit import room as cockpit_room  # noqa: E402
from levain.cockpit import voice as cockpit_voice  # noqa: E402
from levain.cockpit.firejournal import CockpitDB  # noqa: E402
from levain.consent.sign import Signer  # noqa: E402
from levain.coordinator.chat import record_event  # noqa: E402
from levain.voice import custody  # noqa: E402
from levain.voice import meter as voice_meter  # noqa: E402
from levain.voice import providers as vprov  # noqa: E402
from levain.voice.custody import Secret, SecurityStdinWriter, VoiceKeyUnavailable  # noqa: E402
from levain.voice.text import reply_text, speakable  # noqa: E402
from levain.web_server import _MAX_INFLIGHT, _MAX_POST_BYTES, make_server  # noqa: E402
from levain.webpush import notify_tokens, presence_tokens  # noqa: E402
from tests.test_cockpit_k2a import TOKEN, _install  # noqa: E402
from tests.test_cockpit_pairing_http import PROXY  # noqa: E402
from tests.test_cockpit_room import World  # noqa: E402
from tests.test_cockpit_sign_card import _free_port  # noqa: E402
from tests.test_cockpit_threads_mount import qdir  # noqa: E402,F401  (fixture: the consent directory the server reads)
from tests.test_cockpit_webpush import NS  # noqa: E402
from tests.test_voice_custody import FakeSecurity  # noqa: E402

pytestmark = pytest.mark.skipif(subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0,
                                reason="needs ssh-keygen")

VALUE = "vkv-V14-ROUTE-KEY-5d2e8c1a9f3b7046"
SOURCE = "levain-voice-openai-route"
ACCOUNT = "levain-voice"
MESH_TOKEN = "v14-ipad-surface-token"     # a named surface: the browser (owner) token opens nothing off the box
MP3 = b"ID3\x04fake-mp3-audio-bytes"
TRANSCRIPT = "TRANSCRIPT-MARKER the words he said"
AUDIO = b"AUDIO-MARKER-" * 1000
SPEAK = cockpit_voice.SPEAK_PATH
TRANSCRIBE = cockpit_voice.TRANSCRIBE_PATH
VOICE_JSON = cockpit_voice.VOICE_JSON


# --- the rig ------------------------------------------------------------------------------------------------------------

class KeyedCockpit:
    """``tests/chat_egress/keys_rig.py``'s Cockpit, at a root the caller names (the broker's): one enrolled
    laptop-presence ssh key, a real signed log."""

    def __init__(self, root: Path, keys: Path) -> None:
        keys.mkdir()
        self.key = keys / "laptop"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "laptop", "-f", str(self.key)],
                       check=True, capture_output=True, timeout=30)
        pub = " ".join((keys / "laptop.pub").read_text().split()[:2])
        self.fp = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(self.key)], check=True,
                                 capture_output=True, text=True, timeout=30).stdout.split()[1]
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        CockpitDB(root).set_install_class("reported")
        conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
        try:
            auth.sign_and_append(conn, {"op": "enrol", "fingerprint": self.fp, "public_key": pub,
                                        "class": "laptop-presence", "custody": "passphrase"},
                                 signer_fp=self.fp, sign=self.sign, clock=lambda: datetime.now(timezone.utc),
                                 cache=ReplayCache(), anchor=anchor_path(root))
        finally:
            conn.close()

    def sign(self, m: bytes) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.key), "-n", auth.NAMESPACE],
                              input=m, capture_output=True, check=True, timeout=30).stdout.decode()

    def kw(self) -> dict:
        return {"signer_fp": self.fp, "sign": self.sign, "clock": lambda: datetime.now(timezone.utc)}


class Transport:
    """The providers' HTTP, replayed: a status and a body (an error body may quote the key, the T2 shape), or a raise.
    It keeps what it was sent (never the Authorization header itself: only whether it carried the key)."""

    def __init__(self) -> None:
        self.status, self.body, self.raises = 200, MP3, None
        self.seen: list[dict] = []
        self.hold: threading.Event | None = None
        self.entered = threading.Semaphore(0)
        self.on_call = None
        self.walk = False
        self.found: list[str] = []

    def __call__(self, req: vprov.HttpRequest) -> vprov.HttpResponse:
        if self.walk:
            self.found += frame_hits(sys._getframe(1))
        if self.on_call is not None:
            self.on_call()
        self.seen.append({"url": req.url, "body": req.body,
                          "keyed": req.headers.get("Authorization") in (f"Bearer {VALUE}", f"Token {VALUE}")})
        if self.hold is not None:
            self.entered.release()
            self.hold.wait(15)
        if self.raises is not None:
            raise self.raises
        body = self.body
        if req.url.endswith("/v1/audio/transcriptions") and self.status == 200 and body is MP3:
            body = json.dumps({"text": TRANSCRIPT}).encode()
        return vprov.HttpResponse(self.status, body)

    def sent_text(self, i: int = -1) -> str:
        return json.loads(self.seen[i]["body"])["input"]


def frame_hits(frame) -> list[str]:
    """Every local, in ``frame`` and each frame that called it, whose repr holds the key (this file's frames
    excepted: the transport's own ``req`` is the request it was handed)."""
    out = []
    while frame is not None:
        if frame.f_code.co_filename != __file__:
            for k, v in list(frame.f_locals.items()):
                try:
                    shown = v if isinstance(v, str) else v.decode("latin-1") if isinstance(v, bytes) else repr(v)
                except Exception:   # noqa: BLE001 - an object whose repr fails shows nothing
                    continue
                if VALUE in shown:
                    out.append(f"{Path(frame.f_code.co_filename).name}:{frame.f_code.co_name}:{k}")
        frame = frame.f_back
    return out


class Rig:
    def __init__(self, tmp_path: Path, qdir: Path, monkeypatch) -> None:
        self.tmp = tmp_path
        self.qdir = qdir
        self.w = World(qdir)
        _root, self.src, _ids = _install(tmp_path / "entity")
        self.data_root = Path(self.src.write_scope.ledger_root) / "cockpit"
        self.ck = KeyedCockpit(self.data_root, tmp_path / "keys")
        # the consent hook names the broker's own cockpit, so the grant routes revoke in the log read_key reads (a voice
        # route refuses otherwise: test_t_rev_a_voice_route_refuses_when_the_grant_routes_act_on_another_cockpit)
        (qdir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(self.data_root),
                                                           "away_mode": False, "away_hold_seconds": 0}))
        self.sec = FakeSecurity(tmp_path)
        monkeypatch.setattr(custody, "DEFAULT_SECURITY", self.sec.path)   # read_key's default store: the fake
        self.transport = Transport()
        self._pos = 1000
        self._n = 0

    def add_key(self, uses=("stt", "tts"), provider="openai") -> dict:
        record, _warning = custody.add_key(
            self.data_root, provider=provider, uses=uses, account=ACCOUNT, value=Secret(VALUE), source=SOURCE,
            confirm=lambda preview: True, writer=SecurityStdinWriter(self.sec.path),
            items=KeychainItems(self.sec.path), store=KeychainStore(self.sec.path), **self.ck.kw())
        return record

    def _event(self, conn, now, kind: str, body: str):
        self._pos += 1
        self._n += 1
        e, _ = record_event(conn, now, launch_id=self.w.launch, key=f"{kind}:v{self._n}", pos=f"{self._pos:08d}",
                            kind=kind, body=body, normalized=body)
        return e

    def reply(self, *texts: str, end: bool = True) -> int:
        """The room agent's words, then (``end``) its turn's end. The gseq of the end, else of the last text."""
        with self.w.cs._write() as (conn, now):
            last = None
            for t in texts:
                last = self._event(conn, now, "agent_text", t)
            if end:
                last = self._event(conn, now, "turn_ended", "")
        return last.gseq

    @property
    def ledger_path(self) -> Path:
        return self.qdir / cockpit_voice.LEDGER_NAME

    def rows(self) -> list[voice_meter.LedgerRow]:
        if not self.ledger_path.exists():
            return []
        return voice_meter.Meter(self.ledger_path).ledger()


@pytest.fixture
def rig(tmp_path, qdir, monkeypatch):
    return Rig(tmp_path, qdir, monkeypatch)


@contextmanager
def served(rig: Rig, *, room: bool = True, signer_q: Path | None = None, mesh: bool = False):
    port = _free_port()
    signer = None
    if signer_q is not None:
        signer = Signer(signer_q, (NS,), "localhost", lambda: {})
        signer.page_origin = f"http://localhost:{port}"
    kw = {"room_host": rig.w.host} if room else {}
    httpd = make_server(rig.src, host="127.0.0.1", port=port, write_token=TOKEN,
                        surface_tokens={"browser": TOKEN, "ipad": MESH_TOKEN}, chat_service=rig.w.svc,
                        signer=signer, **kw)
    httpd.voice_transport = rig.transport
    if mesh:
        httpd.is_loopback_bind = False
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def call(port, method, path, body=b"", *, ctype="application/json", token=TOKEN, headers=None):
    h = dict(headers or {})
    if ctype is not None and method == "POST":
        h["Content-Type"] = ctype
    if token is not None:
        h["X-Levain-Write-Token"] = token
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        c.request(method, path, body=body if method == "POST" else None, headers=h)
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


def speak(port, rig, gseq, chunk=0, *, provider="openai", voice="nova", speed=1.0, token=TOKEN, headers=None,
          extra=None, thread=None):
    body = {"room_thread_id": thread or rig.w.thread, "reply_gseq": gseq, "chunk": chunk, "provider": provider,
            "voice": voice, "speed": speed, **(extra or {})}
    return call(port, "POST", SPEAK, json.dumps(body).encode(), token=token, headers=headers)


def transcribe(port, audio=AUDIO, *, provider="openai", ctype="audio/webm;codecs=opus", token=TOKEN, headers=None):
    return call(port, "POST", f"{TRANSCRIBE}?provider={provider}", audio, ctype=ctype, token=token, headers=headers)


def js(data: bytes) -> dict:
    return json.loads(data)


def raw(port, head: str, wait: float = 5.0):
    """A hand-written request head with NO body; the answer must come before ``wait`` (the server reads no body)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    try:
        s.sendall(head.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        top, _, rest = buf.partition(b"\r\n\r\n")
        lines = top.decode("latin-1").split("\r\n")
        hdrs = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        n = int(hdrs.get("content-length", "0"))
        while len(rest) < n:
            chunk = s.recv(65536)
            if not chunk:
                break
            rest += chunk
        return int(lines[0].split()[1]), json.loads(rest or b"{}"), hdrs
    finally:
        s.close()


def head(port, path, *, ctype="application/json", clen="100", token=TOKEN):
    return (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n" + (f"Content-Type: {ctype}\r\n" if ctype else "")
            + (f"Content-Length: {clen}\r\n" if clen is not None else "")
            + (f"X-Levain-Write-Token: {token}\r\n" if token else "") + "\r\n")


def no_leak(*texts: str) -> None:
    for t in texts:
        assert VALUE not in t and VALUE[:4] not in t, t[:300]


# --- 1. T-GATE ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bind", ["loopback", "mesh", "proxied"])
def test_t_gate_every_voice_route_needs_the_surface_token_on_every_bind(rig, tmp_path, bind):
    """A presence tap token and a notify token are not the credential; nothing is reserved for a refusal.
    Red with: the carve-out moved after _refuse_non_json (transcribe 415), or the token check dropped."""
    signq = tmp_path / "signq"
    signq.mkdir(mode=0o700)
    tap = presence_tokens.create(signq, "tap")[1]
    notice = notify_tokens.create(signq, "svc")[1]
    rig.add_key()
    g = rig.reply("Hello there.")
    with served(rig, signer_q=signq, mesh=bind == "mesh") as (port, _httpd):
        base = dict(PROXY) if bind == "proxied" else {}
        creds = ("none", "wrong", "presence", "notify", "token") + (() if bind == "loopback" else ("owner_key",))
        for cred in creds:
            hdrs, token = dict(base), None
            if cred == "wrong":
                token = "not-the-token"
            elif cred == "owner_key":           # the browser token off the box: no credential (L-1)
                token = TOKEN
            elif cred == "presence":
                hdrs[presence_tokens.HEADER] = tap
            elif cred == "notify":
                hdrs[notify_tokens.HEADER] = notice
                hdrs["Authorization"] = f"Bearer {notice}"
            elif cred == "token":
                token = TOKEN if bind == "loopback" else MESH_TOKEN
            s1, _h1, b1 = speak(port, rig, g, token=token, headers=hdrs)
            s2, _h2, b2 = transcribe(port, token=token, headers=hdrs)
            s3, _h3, b3 = call(port, "GET", VOICE_JSON, token=token, headers=hdrs)
            if cred == "token":
                assert (s1, s2, s3) == (200, 200, 200), (bind, b1[:200], b2[:200], b3[:200])
            else:
                assert (s1, js(b1)["error"]) == (403, "credential_required"), (bind, cred, b1)
                assert (s2, js(b2)["error"]) == (403, "credential_required"), (bind, cred, b2)
                assert s3 == 403 and "token" in js(b3)["message"], (bind, cred, b3)
    assert [r.status for r in rig.rows()] == ["ok", "ok"]          # the two credentialed calls, nothing else
    assert len(rig.transport.seen) == 2


def test_t_gate_each_refusal_is_sent_before_the_body_and_closes_the_connection(rig, monkeypatch):
    """Every gate before the body read answers a request whose body never arrives. Red with: the owner check dropped
    (the owner case answers 200 or waits), the transcribe cap compared after a read, the busy gate taken blocking."""
    with served(rig) as (port, httpd):
        cases = [
            (head(port, SPEAK, token=None), 403, "credential_required"),
            (head(port, TRANSCRIBE + "?provider=openai", ctype="audio/webm", token="wrong"), 403,
             "credential_required"),
            (head(port, SPEAK, ctype="text/plain"), 415, "unsupported_media_type"),
            (head(port, TRANSCRIBE + "?provider=openai", ctype="text/plain"), 415, "unsupported_media_type"),
            (head(port, TRANSCRIBE + "?provider=openai", ctype="application/json"), 415, "unsupported_media_type"),
            (head(port, SPEAK, clen=None), 411, "length_required"),
            (head(port, SPEAK, clen=str(cockpit_voice.SPEAK_MAX + 1)), 413, "too_large"),
            (head(port, TRANSCRIBE + "?provider=openai", ctype="audio/mp4", clen=str(cockpit_voice.UPLOAD_MAX + 1)),
             413, "too_large"),
        ]
        for request, status, error in cases:
            got, body, hdrs = raw(port, request)
            assert (got, body["error"]) == (status, error), (request, body)
            assert hdrs.get("connection") == "close", request
        assert raw(port, cases[-1][0])[1]["message"] == cockpit_voice.WORDS["too_long"]
        # the voice slots full: busy, at once
        assert httpd.voice_gate.acquire(blocking=False) and httpd.voice_gate.acquire(blocking=False)
        try:
            got, body, hdrs = raw(port, head(port, SPEAK))
            assert (got, body["error"], body["message"]) == (503, "voice_busy", cockpit_voice.WORDS["voice_busy"])
            assert hdrs.get("connection") == "close"
        finally:
            httpd.voice_gate.release()
            httpd.voice_gate.release()
        # a credential that names no client
        from levain import web_server
        monkeypatch.setattr(web_server._Handler, "_thread_owner", lambda self: None)
        got, body, hdrs = raw(port, head(port, SPEAK))
        assert (got, body["error"], body["message"]) == (403, "forbidden", cockpit_voice.WORDS["no_owner"])
        assert hdrs.get("connection") == "close"
    assert rig.rows() == [] and rig.transport.seen == []


def test_t_gate_the_routes_are_absent_without_a_room_host(rig):
    """Red with: the room-host condition dropped from gate 2."""
    rig.add_key()
    g = rig.reply("Hello.")
    with served(rig, room=False) as (port, _httpd):
        got, body, hdrs = raw(port, head(port, SPEAK))
        assert (got, body["error"]) == (404, "not_found") and hdrs.get("connection") == "close"
        assert speak(port, rig, g)[0] == 404
        assert transcribe(port)[0] == 404
        assert call(port, "GET", VOICE_JSON)[0] == 404
    assert not rig.ledger_path.exists() and rig.transport.seen == []


# --- 2. T-NOGATE, 3. T-BUSY --------------------------------------------------------------------------------------------

def test_t_nogate_a_voice_call_never_takes_a_request_gate_slot(rig):
    """Every request_gate slot held by the test: speak and transcribe still answer. Red with: _voice_answer taking
    request_gate (non-blocking: 503 busy)."""
    rig.add_key()
    g = rig.reply("Hello there.")
    with served(rig) as (port, httpd):
        held = 0
        while httpd.request_gate.acquire(blocking=False):
            held += 1
        assert held == _MAX_INFLIGHT
        try:
            assert speak(port, rig, g)[0] == 200
            assert transcribe(port)[0] == 200
        finally:
            for _ in range(held):
                httpd.request_gate.release()


def test_t_busy_two_calls_in_flight_and_a_third_is_busy_while_the_room_still_reads(rig):
    """Red with: VOICE_INFLIGHT 3 (the third enters the transport instead of answering busy)."""
    rig.add_key()
    g = rig.reply("Hello there.")
    rig.transport.hold = threading.Event()
    results: list[int] = []
    with served(rig) as (port, _httpd):
        ts = [threading.Thread(target=lambda: results.append(speak(port, rig, g)[0])) for _ in range(2)]
        for t in ts:
            t.start()
        try:
            for _ in range(2):
                assert rig.transport.entered.acquire(timeout=10)
            status, _h, body = speak(port, rig, g)
            assert (status, js(body)["error"]) == (503, "voice_busy")
            assert call(port, "GET", cockpit_room.ROOM_PATH)[0] == 200
            assert call(port, "GET", VOICE_JSON)[0] == 200
        finally:
            rig.transport.hold.set()
            for t in ts:
                t.join(timeout=20)
    assert results == [200, 200]


# --- 4. T-RECORD, 5. T-ROTATE --------------------------------------------------------------------------------------------

def test_t_record_the_words_are_the_records_reply_and_a_body_cannot_carry_any(rig):
    """Red with: the exact-keys check loosened (a body with ``text`` is served)."""
    rig.add_key()
    long = " ".join(f"Sentence number {i} of the reply, said plainly." for i in range(12))
    g = rig.reply("The first part of the answer.", long)
    stream = cockpit_room.room_read(rig.qdir, rig.w.svc)["stream"]
    chunks = speakable(reply_text(stream, g))
    assert len(chunks) >= 2
    with served(rig) as (port, _httpd):
        for i, words in enumerate(chunks):
            status, hdrs, body = speak(port, rig, g, i)
            assert (status, body) == (200, MP3)
            assert hdrs["content-type"] == "audio/mpeg" and hdrs["x-voice-chunks"] == str(len(chunks))
            assert hdrs["x-voice-chunk"] == str(i) and hdrs["cache-control"] == "private, no-store"
            assert rig.transport.sent_text() == words and rig.transport.seen[-1]["keyed"]
        sent = len(rig.transport.seen)
        for extra in ({"text": "say this instead"}, {"input": "or this"}):
            status, _h, body = speak(port, rig, g, extra=extra)
            assert (status, js(body)["error"]) == (400, "bad_request"), extra
        agent_text = [i["gseq"] for i in stream if i["kind"] == "vendor_agent_text"][0]
        for gseq in (agent_text, g + 100):
            status, _h, body = speak(port, rig, gseq)
            assert (status, js(body)["error"]) == (404, "not_a_reply")
        running = rig.reply("A reply still running.", end=False)
        assert js(speak(port, rig, running)[2])["error"] == "not_a_reply"
        status, _h, body = speak(port, rig, g, len(chunks))
        assert (status, js(body)["error"], js(body)["chunks"]) == (404, "no_such_chunk", len(chunks))
        for bad in ({"chunk": -1}, {"chunk": True}, {"voice": "ballad"}, {"speed": 4.5}, {"speed": "1"},
                    {"provider": "nobody"}):
            body = {"room_thread_id": rig.w.thread, "reply_gseq": g, "chunk": 0, "provider": "openai",
                    "voice": "nova", "speed": 1.0, **bad}
            status, _h, out = call(port, "POST", SPEAK, json.dumps(body).encode())
            assert (status, js(out)["error"]) == (400, "bad_request"), bad
        status, _h, out = speak(port, rig, g, provider="elevenlabs")
        assert (status, js(out)["error"]) == (409, "provider_unsupported")
        assert js(out)["message"] == "ElevenLabs is not supported in this version."
        status, _h, out = speak(port, rig, g, provider="deepgram", voice=vprov.DEEPGRAM_TTS_VOICE, speed=1.25)
        assert (status, js(out)["error"]) == (400, "bad_request")       # Deepgram's speed is fixed at 1.0
    assert len(rig.transport.seen) == sent
    assert [r.status for r in rig.rows()] == ["ok"] * len(chunks)


def test_t_rotate_a_replaced_conversation_is_refused_and_nothing_is_reserved(rig):
    """Red with: BOTH rotation checks removed (``_need_room`` and the re-check after ``room_read``); either alone
    still holds the 409."""
    rig.add_key()
    g = rig.reply("Hello there.")
    old = rig.w.thread
    rig.w._rotate()
    with served(rig) as (port, _httpd):
        status, _h, body = speak(port, rig, g, thread=old)
    assert (status, js(body)["error"], js(body)["message"]) == (
        409, "room_rotated", "This conversation was replaced; nothing more is read from it.")
    assert not rig.ledger_path.exists() and rig.transport.seen == []


# --- 6. T-ORDER, 7. T-SETTLE, 8. T-RELEASE -------------------------------------------------------------------------------

def _spy_order(monkeypatch, rig) -> list[str]:
    order: list[str] = []
    for name in ("reserve", "settle", "release"):
        real = getattr(voice_meter.Meter, name)
        monkeypatch.setattr(voice_meter.Meter, name,
                            (lambda real, name: lambda self, *a, **k: (order.append(name), real(self, *a, **k))[1])(
                                real, name))
    real_read = custody.read_key
    monkeypatch.setattr(custody, "read_key", lambda *a, **k: (order.append("read_key"), real_read(*a, **k))[1])
    rig.transport.on_call = lambda: order.append("send")
    return order


def test_t_order_reserve_then_the_key_then_the_call_then_settle_and_a_cap_reads_no_key(rig, monkeypatch):
    """Red with: read_key moved before reserve in ``_call`` (the order, and a key read on a cap refusal)."""
    rig.add_key()
    g = rig.reply("Hello there.")
    order = _spy_order(monkeypatch, rig)
    with served(rig) as (port, httpd):
        assert speak(port, rig, g)[0] == 200
        assert order == ["reserve", "read_key", "send", "settle"]
        order.clear()
        assert transcribe(port)[0] == 200
        assert order == ["reserve", "read_key", "send", "settle"]
        order.clear()
        httpd.voice_meter = voice_meter.Meter(rig.ledger_path, caps=voice_meter.Caps(0, 0))
        status, _h, body = speak(port, rig, g)
        assert (status, js(body)["error"], js(body)["fallback"]) == (402, "month_cap", "on_device")
        assert order == ["reserve"]


_LIMITS = ("organization_spend_limit_exceeded", "project_spend_limit_exceeded", "organization_usage_limit_exceeded")


def _429(**error) -> bytes:
    return json.dumps({"error": {**error, "message": f"quota, for key {VALUE}"}}).encode()


SETTLE_CASES = [
    pytest.param(200, MP3, None, 200, None, None, id="200"),
    pytest.param(401, f"Incorrect API key provided: {VALUE}".encode(), None, 502, "provider_refused", "refused",
                 id="401"),
    pytest.param(403, VALUE.encode(), None, 502, "provider_refused", "refused", id="403"),
    pytest.param(402, b"", None, 402, "provider_out_of_credit", "out_of_credit", id="402"),
    pytest.param(429, _429(type="insufficient_quota", code="credit_balance_exhausted"), None, 402,
                 "provider_out_of_credit", "out_of_credit", id="429-credit"),
    pytest.param(429, _429(type="insufficient_quota"), None, 402, "provider_out_of_credit", "out_of_credit",
                 id="429-type"),
    *[pytest.param(429, _429(type="insufficient_quota", code=c), None, 402, "provider_spend_limit", "spend_limited",
                   id=f"429-{c}") for c in _LIMITS],
    pytest.param(429, _429(code="rate_limit_exceeded"), None, 503, "provider_rate_limited", "rate_limited",
                 id="429-rate"),
    pytest.param(500, VALUE.encode(), None, 504, "provider_unavailable", "unavailable", id="500"),
    pytest.param(200, b"", OSError(f"connection reset sending {VALUE}"), 504, "provider_unavailable", "unavailable",
                 id="transport-raise"),
    pytest.param(200, b"", None, 504, "provider_unavailable", "unavailable", id="200-empty"),
    pytest.param(404, VALUE.encode(), None, 502, "provider_rejected", "rejected", id="404"),
]


@pytest.mark.parametrize("pstatus,pbody,raises,status,error,code", SETTLE_CASES)
def test_t_settle_and_t_words_every_outcome_after_the_call_settles_in_fixed_words(
        rig, caplog, capsys, pstatus, pbody, raises, status, error, code):
    """The RETURN_voice23_fix rule: once the adapter was called the row is settled (it may have been billed), and
    what the operator sees is fixed words, never the provider's body (a 401 body quoting the key: T2).
    Red with: ``release`` on a VoiceError in ``_call``."""
    caplog.set_level(logging.DEBUG)
    rig.add_key()
    g = rig.reply("Hello there.")
    rig.transport.status, rig.transport.body, rig.transport.raises = pstatus, pbody, raises
    with served(rig) as (port, _httpd):
        got, hdrs, body = speak(port, rig, g)
    assert got == status, body[:300]
    if error is None:
        assert body == MP3
    else:
        out = js(body)
        assert (out["error"], out["fallback"]) == (error, "on_device")
        assert out["message"] == vprov.WORDS[code].format(p="OpenAI")
        assert hdrs["cache-control"] == "private, no-store"
    (row,) = rig.rows()
    assert (row.status, row.use, row.model) == ("ok", "tts", "tts-1") and row.cost_micros > 0
    out_text = body.decode("latin-1")
    no_leak(out_text, caplog.text, *capsys.readouterr())


def _raising(code: str):
    def read_key(*a, **k):
        raise VoiceKeyUnavailable(code, f"words quoting {VALUE}")
    return read_key


RELEASE_CASES = [
    ("not_set", 409, "no_key", "No OpenAI key is set on this Levain. Reading uses the on-device voice."),
    ("use_not_granted", 409, "no_key", "The OpenAI key is not set for reading aloud."),
    ("authority_unreadable", 503, "authority_unreadable",
     "Levain could not verify its authority log just now; premium voice is off until it can."),
    ("keychain_unreadable", 503, "keychain_unreadable",
     "Levain could not read the key from this Mac's keychain (it may be locked)."),
    ("unusable", 409, "key_unusable",
     "The keychain item for OpenAI does not hold a usable key; set it again on the laptop."),
]


@pytest.mark.parametrize("code,status,error,message", RELEASE_CASES)
def test_t_release_a_key_that_cannot_be_read_releases_and_nothing_is_sent(rig, monkeypatch, caplog, code, status,
                                                                          error, message):
    """Red with: settle instead of release on the VoiceKeyUnavailable path of ``_call``."""
    g = rig.reply("Hello there.")
    monkeypatch.setattr(custody, "read_key", _raising(code))
    with served(rig) as (port, httpd):
        got, _h, body = speak(port, rig, g)
        last = dict(httpd.voice_last_read)
    out = js(body)
    assert (got, out["error"], out["message"], out["fallback"]) == (status, error, message, "on_device")
    (row,) = rig.rows()
    assert (row.status, row.cost_micros) == ("released", 0)
    assert rig.transport.seen == []
    assert last.get("openai") == (code if code in cockpit_voice.LAST_READ_STATES else None)
    no_leak(body.decode(), caplog.text)


def test_t_release_through_the_real_reader_no_key_a_locked_keychain_and_a_tts_only_key(rig):
    """The same rule through ``custody.read_key`` itself: no record, a locked keychain, a key not granted for stt."""
    g = rig.reply("Hello there.")
    with served(rig) as (port, httpd):
        assert js(speak(port, rig, g)[2])["error"] == "no_key"
        rig.add_key(uses=("tts",))
        rig.sec.flag("locked")
        assert js(speak(port, rig, g)[2])["error"] == "keychain_unreadable"
        assert httpd.voice_last_read["openai"] == "keychain_unreadable"
        rig.sec.clear("locked")
        status, _h, body = transcribe(port)
        assert (status, js(body)["error"], js(body)["message"]) == (409, "no_stt_key",
                                                                     "The OpenAI key is not set for dictation.")
        assert speak(port, rig, g)[0] == 200
        assert httpd.voice_last_read["openai"] is None
    assert [(r.status, r.use) for r in rig.rows()] == [("ok", "tts"), ("released", "stt"), ("released", "tts"),
                                                        ("released", "tts")]


# --- 9. T-METERERR -----------------------------------------------------------------------------------------------------

def _foreign(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = path.parent / (path.name + suffix)
        if p.exists():
            p.unlink()
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE other (x INTEGER)")
    c.commit()
    c.close()


def test_t_metererr_a_foreign_ledger_is_meter_unavailable_never_a_500(rig):
    """Red with: the B3 wrapper removed from ``Meter.reserve`` (a raw sqlite3.OperationalError reaches the outer catch:
    a 500)."""
    rig.add_key()
    g = rig.reply("Hello there.")
    with served(rig) as (port, _httpd):
        assert speak(port, rig, g)[0] == 200              # the server's meter is ready: no schema check runs again
        _foreign(rig.ledger_path)
        for status, _h, body in (speak(port, rig, g), transcribe(port)):
            out = js(body)
            assert (status, out["error"], out["fallback"]) == (503, "meter_unavailable", "on_device")
            assert out["message"] == "Levain could not check the voice budget, so premium voice is off for now."
        status, _h, body = call(port, "GET", VOICE_JSON)
        assert status == 200 and js(body)["meter"] == "unreadable"
    assert len(rig.transport.seen) == 1


def test_t_metererr_a_write_lock_held_past_the_busy_wait_is_meter_unavailable(rig, monkeypatch):
    """Red with: the B3 wrapper removed from ``Meter.reserve``."""
    rig.add_key()
    g = rig.reply("Hello there.")
    with served(rig) as (port, _httpd):
        assert speak(port, rig, g)[0] == 200
        monkeypatch.setattr(voice_meter, "_BUSY_WAIT_S", 0.1)
        holder = sqlite3.connect(str(rig.ledger_path), isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            for status, _h, body in (speak(port, rig, g), transcribe(port)):
                assert (status, js(body)["error"]) == (503, "meter_unavailable")
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert speak(port, rig, g)[0] == 200
    assert len(rig.transport.seen) == 2


# --- 10. T-FRAME, T-ESCAPE, T-WORDS, T-JSON, T-DISK, T-NOWRITE -----------------------------------------------------------

def test_t_frame_no_frame_on_the_call_stack_holds_the_key_while_the_provider_is_called(rig):
    """At the moment the transport is called, every frame above it (the adapter's, the route's, the handler's) is
    walked: no local's repr holds the key. Red with: ``_call`` binding ``secret.reveal()`` to a local."""
    rig.add_key()
    g = rig.reply("Hello there.")
    rig.transport.walk = True
    with served(rig) as (port, _httpd):
        assert speak(port, rig, g)[0] == 200
        assert transcribe(port)[0] == 200
    assert len(rig.transport.seen) == 2 and all(s["keyed"] for s in rig.transport.seen)
    assert rig.transport.found == []


class _RaisingAdapter:
    """An adapter that walks its callers' frames, then raises with the key in its args (T-FRAME's chained shape)."""
    found: list[str] = []

    def speak(self, key, text, voice, speed):
        _RaisingAdapter.found += frame_hits(sys._getframe(1))
        raise RuntimeError(f"adapter blew up holding {key}")

    def transcribe(self, key, audio, mime, lang=""):
        _RaisingAdapter.found += frame_hits(sys._getframe(1))
        raise RuntimeError(f"adapter blew up holding {key}")


def test_t_frame_and_t_escape_an_adapter_raising_with_the_key_is_a_fixed_500_settled(rig, monkeypatch, caplog,
                                                                                     capsys):
    """An unexpected exception after the call began: 500 in fixed words, the row settled, the key nowhere (no route
    frame held it, no log, no output). Red with: ``_call`` logging ``str(exc)`` or binding the reveal to a local."""
    caplog.set_level(logging.DEBUG)
    rig.add_key()
    g = rig.reply("Hello there.")
    _RaisingAdapter.found = []
    monkeypatch.setattr(vprov, "provider_for", lambda name, transport: _RaisingAdapter())
    with served(rig) as (port, _httpd):
        for status, _h, body in (speak(port, rig, g), transcribe(port)):
            out = js(body)
            assert (status, out["error"], out["message"], out["fallback"]) == (
                500, "internal", "Premium voice failed; reading uses the on-device voice.", "on_device")
            no_leak(body.decode())
    assert _RaisingAdapter.found == []
    assert [r.status for r in rig.rows()] == ["ok", "ok"]
    no_leak(caplog.text, *capsys.readouterr())


def test_t_escape_nothing_escapes_the_route_and_the_log_names_a_type_only(rig, monkeypatch, caplog, capsys):
    """A fault in the route itself (here: the whole speak raising with the key in its words): one 500 in fixed words,
    a log line naming the type. Red with: the outer catch in ``_voice_post`` logging ``exc`` (or exc_info)."""
    caplog.set_level(logging.DEBUG)
    rig.add_key()
    g = rig.reply("Hello there.")

    def broken(*a, **k):
        raise RuntimeError(f"route fault quoting {VALUE}")

    monkeypatch.setattr(cockpit_voice, "speak", broken)
    with served(rig) as (port, _httpd):
        status, hdrs, body = speak(port, rig, g)
    assert (status, js(body)["error"], hdrs.get("connection")) == (500, "internal", "close")
    assert "RuntimeError" in caplog.text
    no_leak(body.decode(), caplog.text, *capsys.readouterr())


def test_t_escape_the_voice_module_never_logs_an_exception_or_chains_one():
    """A grep pin: no ``exc_info``, no ``str(exc)``/``repr(exc)`` and no ``raise ... from exc`` in the voice module, and
    no traceback printing."""
    src = Path(cockpit_voice.__file__).read_text()
    for banned in ("exc_info", "str(exc", "repr(exc", "from exc", "print_exc", "format_exc", "_log.exception"):
        assert banned not in src, banned


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def test_t_json_voice_json_shows_the_record_id_and_uses_and_nothing_of_the_key(rig):
    """No value equals or holds the key, a 4-character prefix of it, its sha256, or the keychain names.
    Red with: the row carrying ``record["source"]`` (or ``account``)."""
    record = rig.add_key()
    g = rig.reply("Hello there.")
    rig.transport.status = 401
    with served(rig) as (port, _httpd):
        speak(port, rig, g)                               # a refused key: last_read says so
        status, hdrs, body = call(port, "GET", VOICE_JSON)
    assert status == 200 and hdrs["cache-control"] == "private, no-store" and "X-Levain-Write-Token" in hdrs["vary"]
    out = js(body)
    openai, deepgram = out["providers"]
    assert (openai["name"], openai["key"], openai["record_id"], openai["uses"], openai["last_read"]) == (
        "openai", "set", record["id"], ["stt", "tts"], "refused")
    assert (deepgram["key"], deepgram["record_id"]) == ("not_set", None)
    assert openai["default_voice"] == "nova" and openai["tts_model"] == "tts-1" and "nova" in openai["voices"]
    assert (out["caps"]["month_usd"], out["caps"]["day_usd"]) == ("10.00", "2.00")
    assert out["estimate"] is True and out["authority"] == "ok" and out["meter"] == "ok"
    banned = [VALUE, VALUE[:4], hashlib.sha256(VALUE.encode()).hexdigest(), SOURCE, ACCOUNT]
    for s in _strings(out):
        for b in banned:
            assert b not in s, (b, s)
    assert "source" not in body.decode() and "account" not in body.decode()


def test_t_json_an_unreadable_authority_log_is_said_and_every_key_reads_not_set(rig, monkeypatch):
    rig.add_key()

    def unreadable(_root):
        def source():
            raise RuntimeError("the log does not replay")
        return source

    monkeypatch.setattr(custody, "authority_for", unreadable)
    with served(rig) as (port, _httpd):
        status, _h, body = call(port, "GET", VOICE_JSON)
    out = js(body)
    assert status == 200 and out["authority"] == "unreadable"
    assert {p["key"] for p in out["providers"]} == {"not_set"}


def test_t_disk_and_t_upload_nothing_holds_the_audio_or_the_transcript_and_units_are_estimated(rig):
    """After a transcribe larger than the generic POST cap: the text comes back, the ledger holds the estimated
    seconds (ceil(bytes/4000)) and no file under the test's root holds the audio, the transcript or the key.
    Red with: the upload cap compared against ``_MAX_POST_BYTES`` (this upload is over it: 413)."""
    rig.add_key()
    audio = (b"AUDIO-MARKER-" * ((_MAX_POST_BYTES // 13) + 7000))[: _MAX_POST_BYTES + 80_000]
    assert _MAX_POST_BYTES < len(audio) < cockpit_voice.UPLOAD_MAX
    with served(rig) as (port, _httpd):
        status, hdrs, body = transcribe(port, audio, ctype="audio/mp4")
    assert (status, js(body)) == (200, {"text": TRANSCRIPT})
    assert hdrs["cache-control"] == "private, no-store"
    (row,) = rig.rows()
    assert (row.use, row.model, row.units, row.status) == ("stt", "gpt-4o-mini-transcribe", -(-len(audio) // 4000),
                                                           "ok")
    assert row.units == cockpit_voice.upload_units(len(audio))
    needles = [b"AUDIO-MARKER-AUDIO-MARKER", TRANSCRIPT.encode(), VALUE.encode()]
    for p in rig.tmp.rglob("*"):
        if p.is_file() and not p.is_symlink() and rig.sec.root not in p.parents:
            data = p.read_bytes()
            assert not any(n in data for n in needles), p


def test_t_upload_a_body_that_never_arrives_still_gets_the_413_at_once(rig):
    """Red with: the transcribe cap checked after the body read (the 4 MiB never comes: no answer in 5 s)."""
    with served(rig) as (port, _httpd):
        got, body, hdrs = raw(port, head(port, TRANSCRIBE + "?provider=openai", ctype="audio/webm",
                                         clen=str(4 * 1024 * 1024 + 1)))
        assert (got, body["error"], body["message"]) == (413, "too_large", cockpit_voice.WORDS["too_long"])
        status, _h, out = transcribe(port, b"x" * 10, ctype="text/plain")
        assert (status, js(out)["error"]) == (415, "unsupported_media_type")
        status, _h, out = transcribe(port, b"")
        assert (status, js(out)["error"]) == (400, "no_audio")
        status, _h, out = call(port, "POST", TRANSCRIBE + "?provider=openai&lang=en", AUDIO, ctype="audio/webm")
        assert (status, js(out)["error"]) == (400, "bad_request")
    assert rig.rows() == []


_WRITERS = ("prepare", "append", "sign_and_append", "add_key", "revoke_key", "_append", "_signed_append",
            "append_revoke", "prepare_revoke", "setup_key")


def _names_used(tree: ast.AST) -> set[str]:
    """Every bare name, imported name and attribute ``tree`` uses, but ``<x>.append`` on anything other than a module
    alias (a list's append is not the authority log's)."""
    modules = {"custody", "auth", "authority", "_auth", "grants", "keys", "levain"}
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            if n.attr == "append" and not (isinstance(n.value, ast.Name) and n.value.id in modules):
                continue
            out.add(n.attr)
        elif isinstance(n, ast.ImportFrom):
            out |= {a.name for a in n.names}
    return out


def test_t_nowrite_no_voice_route_names_an_authority_writer():
    """V1-4 writes no record (key swap, T3): the voice module and the server's voice methods name none of the
    authority writers. (tests/test_cockpit_authority_s3a2.py's roster runs beside this file and must not change.)
    Red with: any reference to ``custody.add_key`` (or ``prepare``/``append``) in the voice module."""
    tree = ast.parse(Path(cockpit_voice.__file__).read_text())
    assert _names_used(tree) & set(_WRITERS) == set()
    import levain.web_server as ws
    server = ast.parse(Path(ws.__file__).read_text())
    methods = [n for n in ast.walk(server) if isinstance(n, ast.FunctionDef)
               and n.name in ("_voice_meter", "_serve_voice", "_voice_post", "_voice_answer")]
    assert len(methods) == 4
    for m in methods:
        assert _names_used(m) & set(_WRITERS) == set(), m.name


# --- 11. T-CAPS --------------------------------------------------------------------------------------------------------

NY = ZoneInfo("America/New_York")


def _prefill(meter: voice_meter.Meter, at: datetime, cost: int) -> None:
    meter.spent()                                        # the schema
    c = sqlite3.connect(str(meter.path))
    try:
        c.execute("INSERT INTO calls (at, provider, use, model, units, cost_micros, status) "
                  "VALUES (?, 'openai', 'tts', 'tts-1', 0, ?, 'ok')", (voice_meter._stamp(at), cost))
        c.commit()
    finally:
        c.close()


@pytest.mark.parametrize("which", ["day", "month"])
def test_t_caps_reaching_a_cap_exactly_is_allowed_and_passing_it_is_refused_naming_the_cap(rig, which):
    """A 100-character chunk of tts-1 costs 1500 micro-dollars. Red with: the meter's cap test written ``>=`` (the
    exactly-at-cap call is refused)."""
    rig.add_key()
    g = rig.reply("A" * 99 + ".")
    now = datetime(2026, 11, 20, 12, 0, tzinfo=NY)
    meter = voice_meter.Meter(rig.ledger_path, clock=lambda: now, tz=NY)
    if which == "day":
        _prefill(meter, now, 2_000_000 - 1500)
        error, words = "day_cap", ("Premium voice is paused until tomorrow: today's $2 cap is reached. Reading uses "
                                   "the on-device voice.")
    else:
        for day in range(1, 11):                          # earlier days of the month, each under the day cap
            _prefill(meter, datetime(2026, 11, day, 12, 0, tzinfo=NY), 999_850)
        error, words = "month_cap", ("Premium voice is paused: this month's $10 cap is reached. Reading uses the "
                                     "on-device voice.")
    with served(rig) as (port, httpd):
        httpd.voice_meter = meter
        assert speak(port, rig, g)[0] == 200              # lands exactly on the cap
        status, _h, body = speak(port, rig, g)
    out = js(body)
    assert (status, out["error"], out["message"], out["fallback"]) == (402, error, words, "on_device")
    assert len(rig.transport.seen) == 1


# --- 14. T-REV, 15. T-GETRO --------------------------------------------------------------------------------------------

# The V1-4 lane's T-REV finding (RETURN_voice_v14.md), fixed by the 1105-17 trev lane: grant_view listed only consent,
# contained-shell and inert grants, so a voice key never reached the grant page; and nothing tied the cockpit the grant
# routes act on (the consent hook's) to the one read_key replays (broker.data_root).

def _hook(queue: Path, root: Path) -> None:
    (queue / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False,
                                                       "away_hold_seconds": 0}))


def test_t_rev_the_phone_grant_page_lists_the_voice_key_that_read_key_reads(rig, tmp_path):
    """The row names the provider and the record id, nothing of the keychain item and nothing of the value. Red with:
    grant_view's voice rows removed (the V1-4 xfail)."""
    from levain.consent.grants import grant_view
    from levain.consent.sign import sign_root

    record = rig.add_key()
    signq = tmp_path / "signq"
    signq.mkdir(mode=0o700)
    _hook(signq, rig.data_root)
    assert Path(sign_root(signq)).resolve() == rig.data_root.resolve()    # the hook names read_key's own log
    view = grant_view(signq, now=datetime.now(timezone.utc))
    rows = {r["id"]: r for r in view["grants"]}
    assert record["id"] in rows, view
    row = rows[record["id"]]
    assert (row["kind"], row["provider"], row["state"], row["id_shown"]) == ("voice-key", "openai", "live",
                                                                            record["id"])
    assert row["instance"] and row["answered"]["count"] == 0
    shown = json.dumps(view)
    no_leak(shown)
    assert SOURCE not in shown and "source" not in row and "account" not in row and "uses" not in row
    assert "levain consent grant revoke" in row["revoke"] and record["id"] in row["revoke"]


RP = "phone.example"                    # a passkey's revoke is taken only from a key made on an https name
ORIGIN = "https://phone.example"


def _phone(ck: "KeyedCockpit"):
    """A software passkey enrolled (display-phone, passkey custody) in ``ck``'s log by its laptop key."""
    from tests.test_dispatch_phone import SoftPasskey

    phone = SoftPasskey(RP)
    conn = sqlite3.connect(ck.root / "cockpit.db", isolation_level=None)
    try:
        auth.sign_and_append(conn, {"op": "enrol", "fingerprint": phone.fp, "public_key": phone.line,
                                    "class": "display-phone", "custody": "passkey"},
                             signer_fp=ck.fp, sign=ck.sign, clock=lambda: datetime.now(timezone.utc),
                             cache=ReplayCache(), anchor=anchor_path(ck.root))
    finally:
        conn.close()
    return phone


def _grant_phone(queue: Path):
    """The cockpit's grant routes exactly as make_server builds them: over the consent queue, acting on sign_root."""
    from levain.consent.answering import passkey_keys
    from levain.consent.grant_phone import GrantPhone
    from levain.consent.sign import sign_root

    return GrantPhone(queue, RP, None,
                      lambda: {k.fingerprint: k for k in passkey_keys(custody.authority_for(sign_root(queue))(), RP)},
                      lambda: sign_root(queue))


def test_t_rev_a_phone_passkey_revoke_of_the_voice_key_stops_the_next_speak(rig):
    """The whole phone path: the grant listing names the voice key and its passkey, the challenge, the passkey's
    assertion, the append; the next speak reads no key (409 no_key, no provider call) and so does transcribe. Red with:
    grant_view's voice rows removed (the listing has no row to revoke from)."""
    record = rig.add_key()
    phone = _phone(rig.ck)
    g = rig.reply("Read this aloud.")
    gp = _grant_phone(rig.qdir)
    with served(rig) as (port, _httpd):
        assert speak(port, rig, g)[0] == 200
        rows = {r["id"]: r for r in gp.listing()["grants"]}
        assert record["id"] in rows, gp.listing()
        row = rows[record["id"]]
        assert row["signers"] == [phone.fp]
        c = gp.step(record["id"], "revoke-challenge", {"signer_fp": phone.fp, "instance": row["instance"]})
        line = json.loads(c["line"])
        assert (line["op"], line["grant_id"]) == ("revoke_grant", record["id"])
        got = gp.step(record["id"], "revoke", phone.assertion(c["challenge"], origin=ORIGIN))
        assert got["revoked"] == record["id"]
        status, _h, body = speak(port, rig, g)
        assert (status, js(body)["error"]) == (409, "no_key"), body
        status, _h, body = transcribe(port)
        assert (status, js(body)["error"]) == (409, "no_key"), body
    assert len(rig.transport.seen) == 1                      # only the call before the revoke went out
    assert record["id"] not in {r["id"] for r in gp.listing()["grants"]}


@pytest.mark.parametrize("hooks", ["other", "none", "two"])
def test_t_rev_a_voice_route_refuses_when_the_grant_routes_act_on_another_cockpit(rig, tmp_path, hooks):
    """The consent hook names a cockpit that is not the broker's data root (or none, or two): the grant page's revoke
    cannot reach the key read_key would read, so speak and transcribe refuse in fixed words before anything is
    reserved or read. Red with: the revoke-reach check in ``voice._call`` removed (both answer 200)."""
    record = rig.add_key()
    other = KeyedCockpit(tmp_path / "other-cockpit", tmp_path / "keys-other")
    _phone(other)
    if hooks == "other":
        _hook(rig.qdir, other.root)
    elif hooks == "none":
        (rig.qdir / "claude_code.json").unlink()
    else:
        (rig.qdir / "codex.json").write_text(json.dumps({"cockpit_data_root": str(other.root)}))
    g = rig.reply("Read this aloud.")
    reads = rig.sec.calls()
    with served(rig) as (port, _httpd):
        for status, _h, body in (speak(port, rig, g), transcribe(port)):
            out = js(body)
            assert (status, out["error"], out["message"], out["fallback"]) == (
                503, "revoke_unreachable", cockpit_voice.WORDS["revoke_unreachable"], "on_device"), body
    assert rig.transport.seen == [] and rig.rows() == []
    assert rig.sec.calls() == reads                          # the keychain was never asked
    if hooks == "other":
        # and the observed half: the phone's revoke, acting on the hook's cockpit, cannot find the key at all
        from levain.consent.grant_phone import GrantRefused
        from levain.consent.grants import grant_instance

        state = custody.authority_for(rig.data_root)()
        gp = _grant_phone(rig.qdir)
        assert record["id"] not in {r["id"] for r in gp.listing()["grants"]}
        signer = next(iter(gp._signers()[0]))
        with pytest.raises(GrantRefused) as exc:
            gp.step(record["id"], "revoke-challenge",
                    {"signer_fp": signer, "instance": grant_instance(state.grant_link(record["id"]))})
        assert exc.value.error == "not_found"


def test_t_getro_voice_json_creates_no_ledger(rig):
    """Red with: ``serve_voice`` reading ``meter.spent()`` (which creates the ledger) instead of
    ``spent_if_present``."""
    rig.add_key()
    with served(rig) as (port, _httpd):
        for _ in range(2):
            status, _h, body = call(port, "GET", VOICE_JSON)
            assert status == 200 and js(body)["caps"]["spent_month_est"] == "0.00"
        assert call(port, "HEAD", VOICE_JSON)[0] == 200
    assert not rig.ledger_path.exists()
    assert not any(p.name.startswith(cockpit_voice.LEDGER_NAME) for p in rig.qdir.iterdir())
