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
from datetime import date, datetime, timezone
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
    def __init__(self, tmp_path: Path, queue: Path, monkeypatch) -> None:
        self.tmp = tmp_path
        self.qdir = queue
        self.w = World(queue)
        _root, self.src, _ids = _install(tmp_path / "entity")
        self.data_root = Path(self.src.write_scope.ledger_root) / "cockpit"
        self.ck = KeyedCockpit(self.data_root, tmp_path / "keys")
        # the consent hook names the broker's own cockpit, so the grant routes revoke in the log read_key reads (a voice
        # route refuses otherwise: test_t_rev_a_voice_route_refuses_when_the_grant_routes_act_on_another_cockpit)
        (queue / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(self.data_root),
                                                           "away_mode": False, "away_hold_seconds": 0}))
        self.sec = FakeSecurity(tmp_path)
        monkeypatch.setattr(custody, "DEFAULT_SECURITY", self.sec.path)   # read_key's default store: the fake
        self.transport = Transport()
        self._pos = 1000
        self._n = 0
        self._opened = False

    def add_key(self, uses=("stt", "tts"), provider="openai", source=SOURCE) -> dict:
        record, _warning = custody.add_key(
            self.data_root, provider=provider, uses=uses, account=ACCOUNT, value=Secret(VALUE), source=source,
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
        """The room agent's words, then (``end``) its turn's end. The gseq of the end, else of the last text. A launch's
        FIRST turn is the open check the launcher sends itself (``RoomLauncher._open_check``), which the room's read leaves
        out (``cockpit.room._without_open_check``); so the first call here writes that turn, then the reply."""
        with self.w.cs._write() as (conn, now):
            last = None
            if not self._opened:
                self._event(conn, now, "agent_text", "done")
                self._event(conn, now, "turn_ended", "")
                self._opened = True
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
def rig(tmp_path, qdir, monkeypatch):  # noqa: F811 - the imported fixture, taken by name
    return Rig(tmp_path, qdir, monkeypatch)


_REAL_TODAY = cockpit_voice._today
BEFORE_CUTOFF = date(2026, 11, 5)       # the day OpenAI is still offered; the tests of the cutoff set their own


@pytest.fixture(autouse=True)
def _clock_before_the_openai_cutoff(monkeypatch):
    """Every test here runs on a pinned day before OpenAI's 2027-01-06 cutoff, so the OpenAI tests do not start failing
    when the real calendar passes it; the cutoff tests below move the clock themselves."""
    monkeypatch.setattr(cockpit_voice, "_today", lambda: BEFORE_CUTOFF)


@contextmanager
def served(rig: Rig, *, room: bool = True, signer_q: Path | None = None, signer_on: bool = True,
           mesh: bool = False, premium_stt: bool = True):
    """``levain serve`` over the rig. By default with a Signer over the consent reader's own queue, as a real install
    runs it, so the phone's grant routes are served (a voice route reads no key without them). ``signer_q`` names
    another queue; ``signer_on=False`` serves no Signer at all. ``premium_stt`` is the server's premium-dictation
    switch (``make_server(premium_stt=)``, off in the product): this fixture turns it ON so every transcribe test below
    runs the route as it was built; the tests of the hidden default pass ``premium_stt=False``."""
    port = _free_port()
    signer = None
    if signer_q is None and signer_on:
        signer_q = rig.qdir
    if signer_q is not None:
        signer = Signer(signer_q, (NS,), "localhost", lambda: {})
        signer.page_origin = f"http://localhost:{port}"
    kw = {"room_host": rig.w.host} if room else {}
    httpd = make_server(rig.src, host="127.0.0.1", port=port, write_token=TOKEN,
                        surface_tokens={"browser": TOKEN, "ipad": MESH_TOKEN}, chat_service=rig.w.svc,
                        signer=signer, premium_stt=premium_stt, **kw)
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
    signq = rig.qdir        # the Signer's queue is the consent reader's, so the grant routes (and voice) are served
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
        # Deepgram's speed range is Aura-2's own, the adapter's constants (the adapter sends a speed other than 1.0):
        # past it is a 400. An in-range request would reserve a meter row, so the range itself is pinned as data below.
        status, _h, out = speak(port, rig, g, provider="deepgram", voice=vprov.DEEPGRAM_TTS_VOICE, speed=1.75)
        assert (status, js(out)["error"], js(out)["message"]) == (400, "bad_request",
                                                                  "That speed is outside what Deepgram accepts.")
        assert (cockpit_voice.PROVIDERS["deepgram"].speed_min, cockpit_voice.PROVIDERS["deepgram"].speed_max) == (
            vprov.DEEPGRAM_SPEED_MIN, vprov.DEEPGRAM_SPEED_MAX) == (0.7, 1.5)
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


def test_t_the_deepgram_listing_is_the_adapters_voice_tuple_and_a_voice_outside_it_is_refused_unsent(rig):
    """Red with: the deepgram ``ProviderData`` listing one voice again (or any list that is not the adapter's tuple)."""
    rig.add_key()
    g = rig.reply("Hello there.")
    with served(rig) as (port, _httpd):
        status, _h, body = call(port, "GET", VOICE_JSON)
        deepgram = next(p for p in js(body)["providers"] if p["name"] == "deepgram")
        assert status == 200 and deepgram["voices"] == list(vprov.DEEPGRAM_TTS_VOICES)
        assert deepgram["default_voice"] == vprov.DEEPGRAM_TTS_VOICE == "aura-2-thalia-en"
        for bad in ("aura-2-sirio-es", "aura-1-asteria-en", "aura-2-nobody-en", "nova"):
            status, _h, out = speak(port, rig, g, provider="deepgram", voice=bad)
            assert (status, js(out)["error"]) == (400, "bad_request"), bad
            assert js(out)["message"] == "That voice is not offered for Deepgram.", bad
    assert rig.transport.seen == []                                  # nothing was reserved or sent


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
    inworld, openai, deepgram = out["providers"]
    assert (inworld["name"], inworld["key"], inworld["record_id"], inworld["uses"]) == ("inworld", "not_set", None, [])
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


# --- trevfix (L3 trev r1) ------------------------------------------------------------------------------------

def _refused_with(port, rig, g, error: str) -> None:
    for status, _h, body in (speak(port, rig, g), transcribe(port)):
        out = js(body)
        assert (status, out["error"], out["message"], out["fallback"]) == (
            503, error, cockpit_voice.WORDS[error], "on_device"), body


@pytest.mark.parametrize("how", ["no_signer", "other_queue"])
def test_r1_no_grant_route_served_means_no_key_read_even_when_the_readers_hook_names_the_root(rig, tmp_path, how):
    """R1: the consent reader's hook names the broker's root, but the server serves no phone grant route (no Signer, or
    a Signer over another queue, whose own hook names the same root too): no phone revoke exists, so speak and
    transcribe refuse ``revoke_unreachable`` and nothing is reserved or read. Red with: ``revoke_queue`` fed the
    reader's store directory instead of the grant routes' queue (both answer 200)."""
    rig.add_key()
    g = rig.reply("Read this aloud.")
    reads = rig.sec.calls()
    kw: dict = {"signer_on": False}
    if how == "other_queue":
        signq = tmp_path / "signq"
        signq.mkdir(mode=0o700)
        _hook(signq, rig.data_root)
        kw = {"signer_q": signq}
    with served(rig, **kw) as (port, httpd):
        assert httpd.grant_phone is None
        _refused_with(port, rig, g, "revoke_unreachable")
    assert rig.transport.seen == [] and rig.rows() == [] and rig.sec.calls() == reads


@pytest.mark.parametrize("fault", ["raises", "not_a_path"])
def test_r3_a_fault_checking_the_reach_is_its_own_sentence_and_logs_the_type_only(rig, monkeypatch, caplog, fault):
    """R3: a fault while checking (the hook read raising; a root that is not a path) is not "another cockpit": it
    refuses ``revoke_unchecked`` in its own words, reads nothing, and the log names the type only. Red with: the fault
    branch of ``revoke_refusal`` answering ``revoke_unreachable``."""
    import levain.consent.answering as answering

    rig.add_key()
    g = rig.reply("Read this aloud.")
    reads = rig.sec.calls()
    secret_text = "SECRET-FAULT-TEXT /private/hooks"

    def broken(_q):
        if fault == "raises":
            raise PermissionError(secret_text)
        return [object()]

    assert cockpit_voice.WORDS["revoke_unchecked"] != cockpit_voice.WORDS["revoke_unreachable"]
    with served(rig) as (port, _httpd):
        monkeypatch.setattr(answering, "hook_roots", broken)
        with caplog.at_level(logging.WARNING, logger="levain.cockpit.voice"):
            _refused_with(port, rig, g, "revoke_unchecked")
    assert rig.transport.seen == [] and rig.rows() == [] and rig.sec.calls() == reads
    lines = [r.getMessage() for r in caplog.records if r.name == "levain.cockpit.voice"]
    assert lines and all(("PermissionError" if fault == "raises" else "TypeError") in m for m in lines), lines
    assert not any(secret_text in m for m in lines) and all(r.exc_info is None for r in caplog.records)


def test_r4_speak_and_transcribe_take_data_root_first_and_their_paths_by_keyword_only(rig):
    """R4: both routes take ``data_root`` first and the queue paths keyword-only, so a caller writing the old
    ``speak(directory, data_root, ...)`` order is a TypeError, never a silent swap. Red with: speak's old positional
    ``(directory, data_root, service, raw)``."""
    import inspect

    for fn in (cockpit_voice.speak, cockpit_voice.transcribe):
        params = inspect.signature(fn).parameters
        assert next(iter(params)) == "data_root", fn
        assert params["revoke_queue"].kind is inspect.Parameter.KEYWORD_ONLY, fn
    assert inspect.signature(cockpit_voice.speak).parameters["directory"].kind is inspect.Parameter.KEYWORD_ONLY
    kw = {"revoke_queue": rig.qdir, "meter": None, "transport": rig.transport, "read_key": None, "last_read": {}}
    with pytest.raises(TypeError):
        cockpit_voice.speak(rig.qdir, rig.data_root, rig.w.svc, b"{}", **kw)
    with pytest.raises(TypeError):
        cockpit_voice.transcribe(rig.qdir, rig.data_root, "provider=openai", "audio/webm", AUDIO, **kw)


class _Proxy:
    """An object whose attributes are ``inner``'s except those given."""

    def __init__(self, inner, **over) -> None:
        self._inner = inner
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_r2_a_voice_record_without_provider_is_listed_and_never_takes_the_page_down(rig, tmp_path, monkeypatch):
    """R2: a record the listing classifies as a voice key but that lacks ``provider`` (a shape no replay writes today,
    put here by wrapping the snapshot) is still listed, with an empty provider, beside the other key. Red with: the
    provider read by ``[...]`` again (KeyError: grant_view raises)."""
    from types import MappingProxyType

    import levain.consent.grants as grants

    a = rig.add_key(provider="openai")
    b = rig.add_key(provider="deepgram", source=SOURCE + "-deepgram")
    real = grants._signing_authority

    def stripped(*args, **kwargs):
        state = real(*args, **kwargs)
        held = dict(state.policy.grants)
        held[a["id"]] = MappingProxyType({k: v for k, v in held[a["id"]].items() if k != "provider"})
        return _Proxy(state, policy=_Proxy(state.policy, grants=MappingProxyType(held)))

    monkeypatch.setattr(grants, "_signing_authority", stripped)
    view = grants.grant_view(rig.qdir, now=datetime.now(timezone.utc))
    rows = {r["id"]: r for r in view["grants"]}
    assert (rows[a["id"]]["provider"], rows[b["id"]]["provider"]) == ("", "deepgram"), view


def test_r2_one_voice_row_that_cannot_be_built_is_skipped_and_the_rest_still_list(rig, monkeypatch, caplog):
    """R2: a fault building one voice-key row leaves that row out (logged by type name) and every other row is still
    listed, so the page can still revoke them. Red with: the per-row catch removed (grant_view raises)."""
    import levain.consent.grants as grants

    a = rig.add_key(provider="openai")
    b = rig.add_key(provider="deepgram", source=SOURCE + "-deepgram")
    state = custody.authority_for(rig.data_root)()
    bad = grants.grant_instance(state.grant_link(a["id"]))
    real = grants.grant_instance

    def instance(link):
        got = real(link)
        if got == bad:
            raise RuntimeError("SECRET-ROW-TEXT")
        return got

    monkeypatch.setattr(grants, "grant_instance", instance)
    with caplog.at_level(logging.WARNING, logger="levain.consent.grants"):
        view = grants.grant_view(rig.qdir, now=datetime.now(timezone.utc))
    ids = {r["id"] for r in view["grants"]}
    assert b["id"] in ids and a["id"] not in ids, view
    lines = [r.getMessage() for r in caplog.records if r.name == "levain.consent.grants"]
    assert lines and all("RuntimeError" in m and "SECRET-ROW-TEXT" not in m for m in lines), lines


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


# --- voice14 round 1 fixes (FINDINGS_voice14_r1.md F1-F10) -----------------------------------------------------------

def _spent(rig) -> voice_meter.Spend:
    return voice_meter.Meter(rig.ledger_path).spent()


def test_f2_a_reader_fault_custody_does_not_name_releases_the_reservation_and_logs_the_type_once(
        rig, monkeypatch, caplog, capsys):
    """F2 + F3 (the 'unexpected' reader-fault branch): read_key raising something custody does not name (a KeyError
    quoting the key) means nothing went out, so the row must end ``released`` at cost zero. Left ``reserved`` it
    counted toward both caps for good (the meter has no reaper). The log names the exception's TYPE once per fault.
    Red with: the ``key_failure == "unexpected"`` branch of ``_call`` returning without ``_end(..., release=True)``."""
    caplog.set_level(logging.DEBUG)
    g = rig.reply("Hello there.")

    def read_key(*a, **k):
        raise KeyError(f"reader fault quoting {VALUE}")

    monkeypatch.setattr(custody, "read_key", read_key)
    with served(rig) as (port, httpd):
        for _ in range(3):
            for status, _h, body in (speak(port, rig, g), transcribe(port)):
                out = js(body)
                assert (status, out["error"], out["fallback"]) == (500, "internal", "on_device")
                assert out["message"] == "Premium voice failed; reading uses the on-device voice."
                no_leak(body.decode())
    rows = rig.rows()
    assert len(rows) == 6 and {(r.status, r.cost_micros) for r in rows} == {("released", 0)}
    spend = _spent(rig)
    assert (spend.day_micros, spend.month_micros) == (0, 0)
    assert rig.transport.seen == []
    lines = [r.getMessage() for r in caplog.records if "key reader" in r.getMessage()]
    assert len(lines) == 6 and all("KeyError" in line for line in lines), lines
    no_leak(caplog.text, *capsys.readouterr())


def _trickle(port, head_text: str, body: bytes, gap: float, wait: float):
    """Send ``head_text``, then ``body`` one byte every ``gap`` seconds; read whatever answer comes. Returns the status
    line's code (or None when the server hung up with nothing) and the seconds until the answer or the hang-up."""
    import time
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    started = time.monotonic()
    try:
        s.sendall(head_text.encode("ascii"))
        s.setblocking(False)
        answered = b""
        for i in range(len(body)):
            try:
                s.sendall(body[i:i + 1])
            except OSError:
                break
            end = time.monotonic() + gap
            while time.monotonic() < end:
                try:
                    got = s.recv(65536)
                except BlockingIOError:
                    time.sleep(0.02)
                    continue
                except OSError:
                    got = b""
                answered += got
                if not got:
                    break
                if b"\r\n\r\n" in answered:
                    break
            if answered:
                break
        if not answered:
            s.setblocking(True)
            s.settimeout(wait)
            try:
                while b"\r\n\r\n" not in answered:
                    got = s.recv(65536)
                    if not got:
                        break
                    answered += got
            except OSError:
                pass
        code = int(answered.split(b" ", 2)[1]) if answered.startswith(b"HTTP/") else None
        return code, answered, time.monotonic() - started
    finally:
        s.close()


def test_f10_a_body_that_trickles_is_cut_at_one_total_deadline_and_frees_the_slot(rig, monkeypatch):
    """F10: the socket timeout is per recv, so a client sending a byte before each one fires held a voice slot for as
    long as it liked; two of them made every later call ``voice_busy``. The body is read under ONE total deadline: 408 in
    fixed words, the connection closed, the slot released. Red with: ``_voice_answer`` reading ``self.rfile.read(clen)``
    in one call (the trickle runs to its end and the answer is not a 408)."""
    monkeypatch.setattr(cockpit_voice, "BODY_DEADLINE_S", 1.0)
    rig.add_key()
    g = rig.reply("Hello there.")
    with served(rig) as (port, httpd):
        body = b"x" * 12
        for path in (SPEAK, TRANSCRIBE + "?provider=openai"):
            ctype = "application/json" if path == SPEAK else "audio/webm"
            code, answered, seconds = _trickle(port, head(port, path, ctype=ctype, clen=str(len(body))), body, 0.4, 10)
            assert code == 408, (path, answered[:200])
            assert seconds < 3.5, seconds
            payload = json.loads(answered.partition(b"\r\n\r\n")[2] or b"{}")
            assert payload["error"] == "request_timeout" and payload["message"] == cockpit_voice.WORDS["body_timeout"]
            assert b"connection: close" in answered.lower()
        for _ in range(cockpit_voice.VOICE_INFLIGHT):                  # both slots are free again
            assert httpd.voice_gate.acquire(blocking=False)
        for _ in range(cockpit_voice.VOICE_INFLIGHT):
            httpd.voice_gate.release()
        assert speak(port, rig, g)[0] == 200
    assert len(rig.transport.seen) == 1


def _early_close(port, path: str, ctype: str, declared: int, sent: bytes, wait: float = 6.0) -> bytes:
    """Declare ``declared`` body bytes, send ``sent`` (fewer), half-close the write side and read whatever the server
    answers until it hangs up. b"" is the server hanging up with no answer."""
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    try:
        s.sendall(head(port, path, ctype=ctype, clen=str(declared)).encode("ascii") + sent)
        s.shutdown(socket.SHUT_WR)
        got = b""
        try:
            while True:
                piece = s.recv(65536)
                if not piece:
                    break
                got += piece
        except OSError:
            pass
        return got
    finally:
        s.close()


def test_u1_a_body_that_ends_short_is_refused_unread_and_reaches_no_provider(rig):
    """The voice routes read their body through the shared ``_read_body``, which hands back the SHORT bytes when the
    client closes before its declared length. A cut-off recording must not be transcribed, nor a cut-off speak request
    run: the route answers nothing (the client is gone), closes, reserves nothing, calls no provider, and the slot is
    free for the next call. The speak body here is a COMPLETE valid request with a longer length declared, so it would
    run if read. Red with: the ``len(raw) != clen`` refusal in ``_voice_answer`` removed (the provider is called)."""
    rig.add_key()
    g = rig.reply("Hello there.")
    whole = json.dumps({"room_thread_id": rig.w.thread, "reply_gseq": g, "chunk": 0, "provider": "openai",
                        "voice": "nova", "speed": 1.0}).encode()
    with served(rig) as (port, httpd):
        for path, ctype, sent in ((SPEAK, "application/json", whole),
                                  (TRANSCRIBE + "?provider=openai", "audio/webm;codecs=opus", AUDIO[:64])):
            assert _early_close(port, path, ctype, len(sent) + 40, sent) == b"", path
            assert rig.transport.seen == [] and rig.rows() == [], path
            for _ in range(cockpit_voice.VOICE_INFLIGHT):                  # both slots are free again
                assert httpd.voice_gate.acquire(blocking=False)
            for _ in range(cockpit_voice.VOICE_INFLIGHT):
                httpd.voice_gate.release()
        assert speak(port, rig, g)[0] == 200 and transcribe(port)[0] == 200
    assert len(rig.transport.seen) == 2


def test_u2_the_timeout_words_the_voice_client_is_told_are_the_shared_readers_words():
    """The voice route no longer sends its own 408: the shared reader does, in ``http_guards.BODY_TIMEOUT_WORDS``. The
    voice module's ``WORDS["body_timeout"]`` stays the voice client's documented sentence, so the two must be one text
    (f10 above reads the answer against the voice words). Red with: either sentence edited alone."""
    from levain import http_guards
    assert cockpit_voice.WORDS["body_timeout"] == http_guards.BODY_TIMEOUT_WORDS


def test_u3_the_voice_route_reads_its_body_only_through_the_shared_reader():
    """``_read_voice_body`` was a second copy of ``_read_body``'s loop (two pieces of code computing one deadline). It
    is gone, and ``_voice_answer`` reads the body in exactly one call, ``self._read_body(clen,
    cockpit_voice.BODY_DEADLINE_S)``, with no direct ``rfile`` or ``connection`` use of its own (the voice deadline stays
    voice's own number, which f10 moves). Red with: the old method restored, or the call made without the voice deadline."""
    import levain.web_server as ws
    tree = ast.parse(Path(ws.__file__).read_text())
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_read_voice_body"]
    answer = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_voice_answer")
    reads = [n for n in ast.walk(answer) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_read_body"]
    assert len(reads) == 1
    assert [ast.unparse(a) for a in reads[0].args] == ["clen", "cockpit_voice.BODY_DEADLINE_S"] and not reads[0].keywords
    assert not [n for n in ast.walk(answer) if isinstance(n, ast.Attribute) and n.attr in ("rfile", "connection")]


class _ValueErrorAdapter:
    """An adapter whose call raises ValueError, as every adapter does for input it refuses BEFORE it sends."""

    def speak(self, key, text, voice, speed):
        raise ValueError("refused before sending")

    def transcribe(self, key, audio, mime, lang=""):
        raise ValueError("refused before sending")


def test_f3_and_f8_an_adapter_valueerror_releases_the_row_and_answers_bad_call_with_the_fallback(rig, monkeypatch):
    """F3 (the ``except ValueError`` release branch) + F8 (the refusal names ``fallback: on_device`` as every sibling
    failure does). Nothing went out, so the row ends ``released`` at cost zero and last_read stays clear. Red with:
    the ``except ValueError`` branch of ``_call`` deleted (it settles: status ``ok``, a 'internal' body) or the
    ``fallback=True`` of the bad_call refusal removed."""
    rig.add_key()
    g = rig.reply("Hello there.")
    monkeypatch.setattr(vprov, "provider_for", lambda name, transport: _ValueErrorAdapter())
    with served(rig) as (port, httpd):
        for status, _h, body in (speak(port, rig, g), transcribe(port)):
            out = js(body)
            assert (status, out["error"], out["message"], out["fallback"]) == (
                500, "internal", "Premium voice could not use that request.", "on_device")
        assert httpd.voice_last_read["openai"] is None
    assert {(r.status, r.cost_micros) for r in rig.rows()} == {("released", 0)} and len(rig.rows()) == 2
    spend = _spent(rig)
    assert (spend.day_micros, spend.month_micros) == (0, 0)


def test_f5_a_call_failure_after_a_refused_key_clears_the_refused_state(rig):
    """F5(a): a key the provider refused sets last_read to ``refused``; once the key is read again the entry is cleared,
    so a later rate limit (a failure that is not a shown state) leaves it clear, and a success leaves it clear.
    Red with: the clear after a successful ``read_key`` in ``_call`` removed (``refused`` outlives the replaced key)."""
    rig.add_key()
    g = rig.reply("Hello there.")
    with served(rig) as (port, httpd):
        rig.transport.status, rig.transport.body = 401, b""
        assert speak(port, rig, g)[0] == 502
        assert httpd.voice_last_read["openai"] == "refused"
        rig.transport.status, rig.transport.body = 429, _429(code="rate_limit_exceeded")
        assert speak(port, rig, g)[0] == 503
        assert httpd.voice_last_read["openai"] is None
        _h = call(port, "GET", VOICE_JSON)
        assert next(p for p in js(_h[2])["providers"] if p["name"] == "openai")["last_read"] is None


@pytest.mark.parametrize("later", ["not_set", "use_not_granted"])
def test_f5_a_key_failure_that_is_not_a_shown_state_clears_an_earlier_one(rig, monkeypatch, later):
    """F5(b): ``keychain_unreadable`` was shown; then the read answers ``not_set`` / ``use_not_granted``, which are not
    states voice.json shows, so the earlier cause must not stay on the page. Red with: ``last_read`` written only when
    the failure is one of ``LAST_READ_STATES`` (the old line)."""
    g = rig.reply("Hello there.")
    with served(rig) as (port, httpd):
        monkeypatch.setattr(custody, "read_key", _raising("keychain_unreadable"))
        assert speak(port, rig, g)[0] == 503
        assert httpd.voice_last_read["openai"] == "keychain_unreadable"
        monkeypatch.setattr(custody, "read_key", _raising(later))
        assert speak(port, rig, g)[0] == 409
        assert httpd.voice_last_read["openai"] is None


def test_f7_a_speed_past_the_float_range_is_a_400_bad_speed_not_a_500(rig):
    """F7: an int of 400 digits fits the 4 KiB speak body; ``math.isfinite`` raised OverflowError on it (a 500).
    Red with: ``_number`` calling ``math.isfinite(value)`` on an int."""
    rig.add_key()
    g = rig.reply("Hello there.")
    with served(rig) as (port, _httpd):
        for speed in (10 ** 400, -(10 ** 400)):
            status, _h, body = speak(port, rig, g, speed=speed)
            out = js(body)
            assert (status, out["error"], out["message"]) == (400, "bad_request",
                                                              "That speed is outside what OpenAI accepts.")
    assert rig.rows() == [] and rig.transport.seen == []


def test_t_gate_the_too_long_sentence_names_the_size_cap_that_is_enforced_not_a_duration():
    """land9e G2: the only cap on a transcribe upload is UPLOAD_MAX bytes, checked on the declared length (a 32 kbps
    15-minute recording is under it and is sent), so the fixed sentence says "large" and never a length of time. The
    key stays ``too_long`` (the code and tests key on it). Red with: "...too long to transcribe (the limit is two
    minutes)." back in WORDS while only bytes are enforced."""
    sentence = cockpit_voice.WORDS["too_long"]
    assert sentence == "That recording is too large to transcribe."
    assert not any(w in sentence.lower() for w in ("minute", "second", "hour", "duration", "long"))
    assert isinstance(cockpit_voice.UPLOAD_MAX, int) and cockpit_voice.UPLOAD_MAX == 4 * 1024 * 1024


# --- 12. HIDDEN-STT: premium dictation is off for Levain 1.0 (Phill 11-05) -------------------------------------------------

OFF_WORDS = "Premium dictation is not offered in this version."


def test_hidestt_the_switch_is_off_unless_named_true_and_the_flag_is_hidden(tmp_path, monkeypatch, capsys):
    """The default is off in the class, in ``make_server`` and on the CLI, only ``True`` turns it on, and ``--premium-stt``
    is parsed but absent from ``levain serve --help``.
    Red with: any default turned to True, ``premium_stt is True`` loosened to a truthiness test (the string case), or the
    flag's ``help=argparse.SUPPRESS`` removed."""
    import inspect

    import levain.web_server as ws
    from levain.cli import main

    assert ws._LevainHTTPServer.premium_stt is False
    assert inspect.signature(make_server).parameters["premium_stt"].default is False
    assert inspect.signature(ws.run_web_server).parameters["premium_stt"].default is False
    got: dict = {}
    monkeypatch.setattr(ws, "run_web_server", lambda path, **k: got.update(k) or 0)
    assert main(["serve", "--path", str(tmp_path)]) == 0 and got["premium_stt"] is False
    got.clear()
    assert main(["serve", "--path", str(tmp_path), "--premium-stt"]) == 0 and got["premium_stt"] is True
    with pytest.raises(SystemExit):
        main(["serve", "--help"])
    assert "--premium-stt" not in capsys.readouterr().out


def test_hidestt_a_truthy_non_true_value_does_not_turn_it_on(rig):
    """``make_server(premium_stt="yes")`` is off: the route answers the fixed refusal.
    Red with: ``httpd.premium_stt = premium_stt is True`` loosened to ``bool(premium_stt)``."""
    rig.add_key()
    with served(rig, premium_stt="yes") as (port, httpd):      # type: ignore[arg-type]
        assert httpd.premium_stt is False
        status, _h, body = transcribe(port)
    assert (status, js(body)["message"]) == (404, OFF_WORDS)
    assert rig.transport.seen == [] and rig.rows() == []


def test_hidestt_off_the_transcribe_route_refuses_in_fixed_words_before_anything_is_read(rig, monkeypatch):
    """With the switch off the transcribe POST answers one 404 in the fixed words, the connection closed, BEFORE the
    content type, the length, the voice slot, the body, the meter and the key are looked at: no key is read, no
    ledger exists, no provider is called, and the answer is the same with a key set and with none. A request that sends
    no body at all, a non-audio type and an over-size length get the same answer (the switch precedes those gates).
    Speak is untouched.
    Red with: the ``not srv.premium_stt`` guard removed from ``_voice_answer`` (a 200 with a key, a 409 with none,
    a 415/413 for the wrong type or size), or moved after ``_read_body``/``_call`` (the body is read or the key is read)."""
    read = []
    real = custody.read_key
    monkeypatch.setattr(custody, "read_key", lambda *a, **k: read.append(a) or real(*a, **k))
    q = TRANSCRIBE + "?provider=openai"
    answers = []
    for with_key in (False, True):
        if with_key:
            rig.add_key()
        g = rig.reply("Hello there.")
        with served(rig, premium_stt=False) as (port, httpd):
            for request in (head(port, q, ctype="audio/webm", clen="4096"),
                            head(port, q, ctype="text/plain", clen="4096"),
                            head(port, q, ctype="audio/webm", clen=str(cockpit_voice.UPLOAD_MAX + 1)),
                            head(port, q, ctype="audio/webm", clen=None)):
                got, body, hdrs = raw(port, request)           # answered with no body ever sent
                assert (got, body["error"], body["message"]) == (404, "not_found", OFF_WORDS), (with_key, request)
                assert hdrs.get("connection") == "close", request
                answers.append(json.dumps(body, sort_keys=True))
            status, _h, out = transcribe(port)                  # and a full audio upload, body sent
            assert (status, js(out)["message"]) == (404, OFF_WORDS)
            answers.append(json.dumps(js(out), sort_keys=True))
            for _ in range(cockpit_voice.VOICE_INFLIGHT):        # the voice slots were never taken
                assert httpd.voice_gate.acquire(blocking=False)
            for _ in range(cockpit_voice.VOICE_INFLIGHT):
                httpd.voice_gate.release()
            if with_key:
                assert speak(port, rig, g)[0] == 200             # read-aloud still works beside the hidden dictation
    assert len(set(answers)) == 1, set(answers)                 # nothing of whether a key exists
    assert [a[:2] for a in read] == [("openai", "tts")], read      # speak's read only: transcribe read no key
    assert [(r.use, r.status) for r in rig.rows()] == [("tts", "ok")]
    assert len(rig.transport.seen) == 1


def test_hidestt_off_the_listing_offers_no_dictation_and_on_it_does(rig):
    """The page reads ``voice.json`` to decide what to offer: off, no provider lists ``stt`` among its uses, the body
    says ``premium_stt: false`` and the retention words leave transcription out; on, it is as it was.
    Red with: the ``u != "stt" or (premium_stt and data.stt_model)`` filter dropped (stt listed while off), or the retention choice dropped."""
    rig.add_key()
    with served(rig, premium_stt=False) as (port, _httpd):
        off = js(call(port, "GET", VOICE_JSON)[2])
    with served(rig, premium_stt=True) as (port, _httpd):
        on = js(call(port, "GET", VOICE_JSON)[2])
    openai_off, openai_on = (next(p for p in body["providers"] if p["name"] == "openai") for body in (off, on))
    assert off["premium_stt"] is False and on["premium_stt"] is True
    assert openai_off["uses"] == ["tts"] and openai_on["uses"] == ["stt", "tts"]
    assert "ranscri" not in openai_off["retention"] and "Transcription" in openai_on["retention"]
    for text in _strings(off):
        assert "dictation" not in text.lower() and "transcri" not in text.lower(), text
    assert openai_off["key"] == "set"                            # the key itself is as it was: only the use is hidden


# --- Inworld, the premium default (Phill 11-05, np-c290888c), and OpenAI's 2027-01-06 cutoff ---------------------------

INWORLD_URL = "https://api.inworld.ai/v1/audio/speech"


def test_inworld_is_the_first_provider_listed_with_the_documented_voices_and_honest_retention_words(rig):
    """The first row is the premium default: the only place that says so is the order of ``PROVIDERS``.
    Red with: the inworld entry moved after openai, a voice added that no page prints, or the retention words claiming
    what Inworld's pages do not."""
    with served(rig) as (port, _httpd):
        status, _h, body = call(port, "GET", VOICE_JSON)
    rows = js(body)["providers"]
    assert status == 200 and [r["name"] for r in rows] == ["inworld", "openai", "deepgram"]
    inworld = rows[0]
    assert inworld["voices"] == list(vprov.INWORLD_TTS_VOICES) == ["Alex", "Ashley", "Dennis"]
    assert (inworld["default_voice"], inworld["tts_model"], inworld["speed"]) == ("Dennis", "inworld-tts-2-flash", [0.5, 1.5])
    assert "do not say how long" in inworld["retention"] and "train AI models" in inworld["retention"]
    assert "not used to train" not in inworld["retention"].lower()
    # L3 inworld r3 codex LOW: the words named a plan ("its Developer plan and above") as able to turn on zero data
    # retention, which Inworld's pricing page (read 2026-11-05) lists only as an add-on on a higher plan. Plan eligibility
    # moves, so the words name no plan and point at Inworld's own page. Red with: the old sentence restored.
    assert "plan and above" not in inworld["retention"] and "Developer" not in inworld["retention"]
    assert "https://inworld.ai/pricing" in inworld["retention"]


def test_a_speak_naming_inworld_sends_one_call_to_inworld_and_meters_its_characters(rig):
    """Red with: provider_for lacking inworld, the meter row missing, or the route reading another provider's model.
    The price is $15 per million characters, so a character is 15 micro-dollars."""
    rig.add_key(provider="inworld", uses=("tts",), source=SOURCE + "-inworld")
    g = rig.reply("Hello there.")
    with served(rig) as (port, _httpd):
        status, hdrs, body = speak(port, rig, g, provider="inworld", voice="Ashley", speed=1.25)
    assert (status, body) == (200, MP3) and hdrs["content-type"] == "audio/mpeg"
    (seen,) = rig.transport.seen
    assert seen["url"] == INWORLD_URL and seen["keyed"]
    sent = json.loads(seen["body"])
    assert sent == {"model": "inworld-tts-2-flash", "input": "Hello there.", "voice": "Ashley", "speed": 1.25,
                    "response_format": "mp3"}
    (row,) = rig.rows()
    assert (row.provider, row.use, row.model, row.units, row.cost_micros, row.status) == (
        "inworld", "tts", "inworld-tts-2-flash", len("Hello there."), 15 * len("Hello there."), "ok")


@pytest.mark.parametrize("bad", [{"voice": "alloy"}, {"voice": "dennis"}, {"voice": "nova"}, {"speed": 1.6},
                                 {"speed": 0.4}, {"speed": 4.0}])
def test_a_voice_or_speed_inworld_does_not_offer_is_refused_unsent_and_unreserved(rig, bad):
    """OpenAI's voice names and speed range do not carry over (the pages: ``alloy`` is a 404, a speed past 0.5-1.5 a 400).
    Red with: the inworld ``ProviderData`` listing an OpenAI voice, or taking OpenAI's speed range."""
    rig.add_key(provider="inworld", uses=("tts",), source=SOURCE + "-inworld")
    g = rig.reply("Hello there.")
    with served(rig) as (port, _httpd):
        status, _h, out = speak(port, rig, g, provider="inworld", **{"voice": "Dennis", "speed": 1.0, **bad})
    assert (status, js(out)["error"]) == (400, "bad_request") and "Inworld" in js(out)["message"]
    assert rig.transport.seen == [] and rig.rows() == []


def test_another_providers_key_is_never_used_for_inworld(rig):
    """Red with: voice_key_for or the route falling back to any key when the named provider has none."""
    rig.add_key(provider="openai")
    g = rig.reply("Hello there.")
    with served(rig) as (port, _httpd):
        status, _h, out = speak(port, rig, g, provider="inworld", voice="Dennis")
    assert (status, js(out)["error"]) == (409, "no_key") and js(out)["message"].startswith("No Inworld key is set")
    assert rig.transport.seen == [] and [r.status for r in rig.rows()] == ["released"]


def test_inworld_is_speech_only_a_transcribe_naming_it_is_refused_before_any_key_or_meter_row(rig, monkeypatch):
    """Premium dictation is off in the product; with the hidden switch on, Inworld still has no dictation to offer. The record
    names ``stt`` as one signed by a build without custody's per-provider uses would (custody refuses it now).
    Red with: the ``stt_model`` check dropped from ``_query_provider``."""
    monkeypatch.setattr(custody, "PROVIDER_USES", {})
    rig.add_key(provider="inworld", uses=("stt", "tts"), source=SOURCE + "-inworld")
    with served(rig, premium_stt=True) as (port, _httpd):
        status, _h, out = transcribe(port, provider="inworld")
    assert (status, js(out)["error"]) == (409, "no_dictation")
    assert js(out)["message"] == "Inworld reads aloud only; it does not offer dictation."
    assert rig.transport.seen == [] and rig.rows() == []


def test_voice_json_never_lists_dictation_for_inworld_even_when_its_record_allows_it(rig, monkeypatch):
    """A record naming ``stt`` for Inworld (signed by a build without custody's per-provider uses; custody refuses it now) with
    premium dictation on: Inworld's row must not offer it. Red with: the ``data.stt_model`` condition dropped from ``serve_voice``."""
    monkeypatch.setattr(custody, "PROVIDER_USES", {})
    rig.add_key(provider="inworld", uses=("stt", "tts"), source=SOURCE + "-inworld")
    rig.add_key(provider="openai")
    with served(rig, premium_stt=True) as (port, _httpd):
        rows = {r["name"]: r for r in js(call(port, "GET", VOICE_JSON)[2])["providers"]}
    assert rows["inworld"]["uses"] == ["tts"] and rows["openai"]["uses"] == ["stt", "tts"]


def test_the_cutoff_is_the_sixth_of_january_2027():
    """Red with: the constant moved (OpenAI's page announced tts-1's shutdown for 2027-01-06)."""
    assert cockpit_voice.OPENAI_OFFERED_UNTIL == date(2027, 1, 6)


def test_the_real_clock_seam_reads_the_utc_date():
    """Red with: ``_today`` reading something other than today's UTC date."""
    assert _REAL_TODAY() in {datetime.now(timezone.utc).date(), date.fromordinal(datetime.now(timezone.utc).date().toordinal() - 1)}


@pytest.mark.parametrize("today,offered", [(date(2026, 11, 5), True), (date(2027, 1, 5), True),
                                           (date(2027, 1, 6), False), (date(2027, 1, 7), False),
                                           (date(2030, 6, 1), False)])
def test_openai_is_offered_until_the_cutoff_and_not_from_it_in_both_the_listing_and_the_refusals(rig, monkeypatch, today,
                                                                                                    offered):
    """One function (``offered``) decides for ``voice.json``, ``speak`` and ``transcribe``, so they cannot disagree.
    Red with: the comparison changed to ``>``, the cutoff applied to the listing only, or to the speak route only."""
    monkeypatch.setattr(cockpit_voice, "_today", lambda: today)
    rig.add_key(provider="openai")
    rig.add_key(provider="inworld", uses=("tts",), source=SOURCE + "-inworld")
    g = rig.reply("Hello there.")
    with served(rig, premium_stt=True) as (port, _httpd):
        names = [r["name"] for r in js(call(port, "GET", VOICE_JSON)[2])["providers"]]
        o_status, _h, o_out = speak(port, rig, g, provider="openai", voice="nova")
        t_status, _h, t_out = transcribe(port, provider="openai")
        i_status, _h, i_body = speak(port, rig, g, provider="inworld", voice="Dennis")
    assert i_status == 200 and i_body == MP3                       # the default is not touched by the date
    if offered:
        assert names == ["inworld", "openai", "deepgram"]
        assert (o_status, t_status) == (200, 200)
    else:
        assert names == ["inworld", "deepgram"]
        assert (o_status, js(o_out)["error"], js(o_out)["message"]) == (
            409, "provider_unsupported", "OpenAI is not supported in this version.")
        assert (t_status, js(t_out)["error"]) == (409, "provider_unsupported")
        assert [s["url"] for s in rig.transport.seen] == [INWORLD_URL]          # nothing went to OpenAI
        assert {r.provider for r in rig.rows()} == {"inworld"}


def test_an_inworld_call_is_metered_in_utf16_code_units_so_an_emoji_reserves_two(rig):
    """L3 inworld r1 codex HIGH: Inworld bills what it counts, UTF-16 code units. Red with: the reservation using len(words)."""
    rig.add_key(provider="inworld", uses=("tts",), source=SOURCE + "-inworld")
    g = rig.reply("Hi \U0001F600")
    with served(rig) as (port, _httpd):
        status, _hdrs, _body = speak(port, rig, g, provider="inworld", voice="Ashley", speed=1.0)
    assert status == 200
    (row,) = rig.rows()
    assert (row.units, row.cost_micros) == (5, 15 * 5), (row.units, row.cost_micros)       # H i space = 3, the emoji = 2
