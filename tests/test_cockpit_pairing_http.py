"""PAIR P1, the HTTP half: the paired credential path, the pair routes and the stream's revocation check
(levain project_memory/1024-18/PAIR_DESIGN.md, rev 6). Each test names the ruling whose path it holds."""

from __future__ import annotations

import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from levain.cockpit.firejournal import revoke_digest

from levain import web_server
from levain.cockpit.broker import Broker, Lifecycle
from levain.cockpit import firejournal
from levain.cockpit.firejournal import PAIRED_PREFIX, CockpitDB, revision_identity
from levain.dashboard import SubstrateSource
from levain.web_server import make_server
from tests._waits import scaled, wait_until
from tests.test_cockpit_k2a import _install

T = "pairing-browser-token"
H = "X-Levain-Write-Token"
PROXY = {"X-Forwarded-For": "100.101.102.103"}
JUNK = "j" * 43


@contextmanager
def _serve(src: SubstrateSource, **kw: Any) -> Iterator[tuple[int, Any]]:
    kw.setdefault("surface_tokens", {"browser": T})
    kw.setdefault("install_class", "reported")
    httpd = make_server(src, host="127.0.0.1", port=0, **kw)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1], httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _call(port: int, method: str, path: str, body: Any = None, token: str | None = None,
          headers: dict | None = None, conn: http.client.HTTPConnection | None = None) -> tuple[int, dict, dict]:
    c = conn or http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    hdrs = dict(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
    if token is not None:
        hdrs[H] = token
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=hdrs)
    r = c.getresponse()
    raw = r.read()
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        out = {"_raw": raw.decode("utf-8", "replace")}
    return r.status, out, {k.lower(): v for k, v in r.getheaders()}


def _pair(port: int, token: str = T, label: str = "ipad", proxied: bool = False) -> tuple[str, str]:
    # the owner key mints on loopback only (W1b L-1); a paired token mints through the proxy
    st, off, _ = _call(port, "POST", "/cockpit/pair/offer", {"label": label}, token=token,
                       headers=PROXY if proxied and token != T else None)
    assert st == 200, off
    st, cl, hdrs = _call(port, "POST", "/cockpit/pair/claim", {"code": off["code"]},
                         headers=PROXY if proxied else None)
    assert st == 200 and hdrs["cache-control"] == "no-store", (st, cl, hdrs)   # RQ-7
    return cl["device_id"], cl["token"]


def _tray_row(port: int, token: str) -> dict:
    st, panel, _ = _call(port, "GET", "/cockpit/panel/tray.json", token=token, headers=PROXY)
    assert st == 200, panel
    return panel["rows"][0]


@pytest.fixture
def src(tmp_path: Path) -> SubstrateSource:
    return _install(tmp_path)[1]


@pytest.fixture
def quick_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_server, "_PAIR_WAIT_S", scaled(0.3))   # the settle wait is scaled, so a loaded runner gets longer to leave ACTIVATING


@contextmanager
def _standby(src: SubstrateSource) -> Iterator[tuple[int, int, str, Any]]:
    """A live serve and a second serve on the SAME ledger (its broker STANDBY: the first holds cockpit.lock), plus
    a device token paired on the live one: (live port, standby port, device token, standby httpd)."""
    with _serve(src) as (live, a):
        # start the live broker synchronously: the pairing call below must not depend on a settle wait (under
        # quick_wait a still-ACTIVATING broker answers the offer 503 on a loaded runner)
        assert a.broker.start() is Lifecycle.ACTIVE
        _dev, tok = _pair(live)
        with _serve(src) as (other, b):
            assert b.broker.start() is Lifecycle.STANDBY
            yield live, other, tok, b


# --- RP-10 ---------------------------------------------------------------------------------------------------------

def test_rp10_1_offer_revoke_and_list_need_a_token_loopback_and_proxied_and_the_claim_is_guarded(src) -> None:
    with _serve(src) as (port, _h):
        for headers in (None, PROXY):
            for path, body in (("/cockpit/pair/offer", {"label": "x"}), ("/cockpit/pair/revoke", {"id": "d-0"})):
                st, out, _ = _call(port, "POST", path, body, headers=headers)
                assert (st, out["error"]) == (403, "credential_required"), (path, headers, out)
            st, out, _ = _call(port, "GET", "/cockpit/pair/devices.json", headers=headers)
            assert st == 403
        st, off, _ = _call(port, "POST", "/cockpit/pair/offer", {"label": "x"}, token=T)
        assert st == 200
        # the claim: a cross-site page and a non-JSON body are refused before the code is looked at
        st, out, _ = _call(port, "POST", "/cockpit/pair/claim", {"code": off["code"]},
                           headers={"Sec-Fetch-Site": "cross-site"})
        assert (st, out["error"]) == (403, "forbidden")
        st, out, _ = _call(port, "POST", "/cockpit/pair/claim", None,
                           headers={"Content-Type": "text/plain", "Content-Length": "2"})
        assert st == 415
        st, out, _ = _call(port, "POST", "/cockpit/pair/claim", {"code": off["code"], "label": "mine"})
        assert st == 400           # RP-6: the label is the offer's, never the claim's
        st, out, _ = _call(port, "POST", "/cockpit/pair/claim", {"code": off["code"] + "x" * 1100})
        assert st == 413           # the 1 KiB cap (RP-4)
        st, out, _ = _call(port, "POST", "/cockpit/pair/claim", {"code": off["code"]})
        assert st == 200 and out["token"]


def test_rp10_2_a_paired_token_makes_a_c1_write_and_a_broker_backed_t2(src) -> None:
    """RP-1's class: a ':' in the credential name would 500 every broker write while reads looked healthy."""
    with _serve(src) as (port, _h):
        device, tok = _pair(port)       # the owner key pairs on loopback only (L-1); the paired token then travels proxied
        st, man, _ = _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)
        assert st == 200 and man["credential"]["name"] == PAIRED_PREFIX + device
        row = _tray_row(port, tok)
        st, out, _ = _call(port, "POST", "/cockpit/verb", {"verb": "intention_touch", "panel_id": "tray",
                                                           "row_id": row["id"], "row_version": row["version"],
                                                           "params": {}}, token=tok, headers=PROXY)
        assert st == 200 and out["ok"], out
        row = _tray_row(port, tok)
        st, out, _ = _call(port, "POST", "/cockpit/verb", {
            "verb": "intention_set_disposition", "panel_id": "tray", "row_id": row["id"], "row_version": row["version"],
            "params": {"disposition": "agenda"}, "intent": "reported", "tier_seen": "T2",
            "gesture_seen": "direct-reported", "idempotency_key": "t2-1"}, token=tok, headers=PROXY)
        assert (st, out.get("state"), out.get("decision_kind")) == (200, "COMMITTED", "ALLOWED_REPORTED"), out


def test_rp10_4_revoke_refuses_the_next_request_cascades_and_rotates(src) -> None:
    with _serve(src) as (port, httpd):
        a, ta = _pair(port)
        b, tb = _pair(port, token=ta)                   # B minted by A: A's family
        s, ts = _pair(port)                             # a sibling minted by the browser surface
        row = _tray_row(port, ta)
        st, held, _ = _call(port, "POST", "/cockpit/verb", {      # a WAITING pending under A (no intent = sign)
            "verb": "intention_set_disposition", "panel_id": "tray", "row_id": row["id"],
            "row_version": row["version"], "params": {"disposition": "agenda"}}, token=ta, headers=PROXY)
        assert (st, held["state"]) == (200, "WAITING"), held
        db = httpd.broker.active_db()
        assert db.token_generation(PAIRED_PREFIX + a) == 1
        st, pv, _ = _call(port, "POST", "/cockpit/pair/revoke-preview", {"id": a}, token=ts)
        st, out, _ = _call(port, "POST", "/cockpit/pair/revoke", {"id": a, "expect_digest": pv["digests"]["family"], "mode": "family"}, token=ts)   # any credential may revoke
        assert st == 200 and sorted(out["revoked"]) == sorted([a, b])
        for tok in (ta, tb):
            assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 403
            assert _call(port, "POST", "/cockpit/pair/offer", {}, token=tok)[0] == 403
        assert _call(port, "GET", "/cockpit/manifest.json", token=ts, headers=PROXY)[0] == 200
        assert db.token_generation(PAIRED_PREFIX + a) == 2
        assert db.outcome(held["pending_id"]).state == "CANCELLED"
        st, out, _ = _call(port, "POST", "/cockpit/pair/revoke", {"id": "d-0123456789ab", "expect_digest": "0" * 64, "mode": "family"}, token=T)
        assert st == 404
        st, out, _ = _call(port, "POST", "/cockpit/pair/revoke", {"id": "browser", "expect_digest": "0" * 64, "mode": "family"}, token=T)
        assert st == 400           # RP-7: a surface token is never revocable here


# --- RS-1 / RS-2 / RU-4 / RT-4 -------------------------------------------------------------------------------------

def test_rs1_a_revoke_is_seen_by_the_next_request_on_the_same_keep_alive_connection(src) -> None:
    with _serve(src) as (port, _h):
        device, tok = _pair(port)
        keep = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        st, _o, _ = _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY, conn=keep)
        sock = keep.sock
        assert st == 200 and sock is not None
        assert _call(port, "POST", "/cockpit/pair/revoke", {"id": device, "expect_digest": revoke_digest([device], "family"), "mode": "family"}, token=T)[0] == 200
        st, out, _ = _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY, conn=keep)
        assert keep.sock is sock, "the second request must ride the same connection"
        assert st == 403, out
        keep.close()


def test_rs2_rt4_ru4_the_first_paired_request_on_a_fresh_serve_starts_the_broker_once_and_succeeds(
        src, monkeypatch) -> None:
    """RS-2 / RT-4 (the cockpit unbuilt) / RU-4 (async start once, every request waits on state, the initiator too):
    a slow start sequence (ACTIVATING 0.6 s) still answers 200 to four concurrent paired requests, one start."""
    with _serve(src) as (port, _h):
        _d, tok = _pair(port)
    starts: list[str] = []
    real_attempt = Broker._attempt

    def slow_attempt(self: Broker) -> bool:
        starts.append(threading.current_thread().name)
        with self._mu:
            self._state = Lifecycle.ACTIVATING
        time.sleep(0.6)
        with self._mu:
            self._state = Lifecycle.STANDBY
        return real_attempt(self)

    monkeypatch.setattr(Broker, "_attempt", slow_attempt)
    with _serve(src) as (port, httpd):
        assert httpd.cockpit is None and httpd.broker.state is Lifecycle.STANDBY
        res: list = []
        ths = [threading.Thread(target=lambda: res.append(
            _call(port, "GET", "/cockpit/pair/devices.json", token=tok, headers=PROXY)[0])) for _ in range(4)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(15)
        assert res == [200, 200, 200, 200], res
        assert starts == ["cockpit-broker-start"], starts


# --- RT-1 / RU-1 / RU-2 / RU-5 -------------------------------------------------------------------------------------

def test_rt1_ru2_a_paired_request_through_every_gate_class_in_standby_is_503_without_the_word(src, quick_wait) -> None:
    with _standby(src) as (_live, port, tok, httpd):
        httpd.compose_signer = lambda verb, params: "never-called"
        cases = [
            ("GET", "/cockpit/manifest.json", None, PROXY, tok),             # the read gate
            ("POST", "/cockpit/verb", {"verb": "x"}, None, tok),             # _is_write_route
            ("POST", "/cockpit/sign/compose", {}, None, tok),                # sign / compose / grant
            ("POST", "/no-such-route", {}, PROXY, tok),                      # the general POST gate
            ("POST", "/cockpit/pair/offer", {}, None, tok),                  # an authed pair route
            ("GET", "/cockpit/pair/devices.json", None, None, JUNK),         # a junk header on a token-every-bind read
        ]
        # (a junk header on a loopback, unproxied read that needs no credential is class none since F2(c): its test)
        for method, path, body, headers, token in cases:
            st, out, hdrs = _call(port, method, path, body, token=token, headers=headers)
            assert (st, out.get("error")) == (503, "broker_unavailable"), (path, st, out)
            assert "token" not in json.dumps(out).lower(), out
            assert hdrs.get("retry-after") == "2" and hdrs.get("connection") == "close", hdrs   # RU-2
        # a surface token is unaffected by the standby broker (the owner key, on loopback: it is refused proxied, L-1)
        assert _call(port, "GET", "/cockpit/manifest.json", token=T)[0] == 200


def test_ru5_the_cockpit_read_path_with_its_inner_catch_answers_503_not_500(src, quick_wait, monkeypatch) -> None:
    """RU-5: on a loopback read the credential is first computed INSIDE handle_cockpit_get's try (no gate before it);
    its broad ``except`` must re-raise so the one outer catch answers. Since F2(c) such a read needs no credential and
    is answered as class none, so the request is marked as one that requires a credential to reach the re-raise."""
    monkeypatch.setattr(web_server._Handler, "_credential_required", lambda self: True)
    with _standby(src) as (_live, port, tok, _h):
        st, out, _ = _call(port, "GET", "/cockpit/panel/tray.json", token=tok)
        assert (st, out.get("error")) == (503, "broker_unavailable"), (st, out)


def test_ru1_where_paired_tokens_cannot_exist_a_junk_header_is_a_plain_miss(src, tmp_path) -> None:
    """RU-1: a read-only source (no ledger_root, no broker): an off-box read with a junk header is served token-free
    (intention-220) as today, never a 503. A disabled broker (no install class) over a journal with no paired device keeps
    a wrong token's definite 403; over one that holds a paired device, that device's token gets the 503 (L3 r2), since
    a 403 would make the page drop a good token."""
    ro = SubstrateSource(anneal=src.anneal, write_scope=None, context_json=src.context_json)
    with _serve(ro, surface_tokens=None, install_class=None) as (port, httpd):
        httpd.is_loopback_bind = False
        assert httpd.broker is None
        st, _o, _ = _call(port, "GET", "/cockpit/manifest.json", token=JUNK)
        assert st == 200
        st, out, _ = _call(port, "POST", "/cockpit/pair/claim", {"code": "x"})
        assert st == 404           # RP-3
    with _serve(src, install_class=None) as (port, httpd):
        st, out, _ = _call(port, "POST", "/cockpit/verb", {"verb": "x"}, token=JUNK)
        assert (st, out["error"]) == (403, "credential_required"), out
        assert httpd.broker.state is Lifecycle.INSTALL_DISABLED
    with _serve(src) as (port, httpd):
        _d, tok = _pair(port)
    with _serve(src, install_class=None) as (port, httpd):
        st, out, _ = _call(port, "POST", "/cockpit/verb", {"verb": "x"}, token=tok)
        assert httpd.broker.state is Lifecycle.INSTALL_DISABLED
        assert (st, out["error"]) == (503, "broker_unavailable"), out
        assert "token" not in json.dumps(out).lower()


# --- RT-3 ----------------------------------------------------------------------------------------------------------

def test_rt3_a_misshapen_header_is_never_looked_up_and_a_full_lookup_set_is_503(src, monkeypatch) -> None:
    surface = "flowconnect-surface-token-0123456789abcdef"      # a named surface: the owner key is refused proxied (L-1)
    with _serve(src, surface_tokens={"browser": T, "flowconnect": surface}) as (port, httpd):
        _d, tok = _pair(port)
        db = httpd.broker.active_db()
        looked: list[str] = []
        real = type(db).verify_device

        def spy(self, token, *, now):
            looked.append(token)
            return real(self, token, now=now)

        monkeypatch.setattr(type(db), "verify_device", spy)
        for bad in ("short", "x" * 129, "has space in it ok", "é" * 20):
            assert _call(port, "GET", "/cockpit/manifest.json", token=bad, headers=PROXY)[0] in (403, 400)
        assert looked == []
        for _ in range(web_server._PAIR_VERIFY_SLOTS):
            assert httpd.pair_verify_gate.acquire(blocking=False)
        try:
            st, out, _ = _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)
            assert (st, out.get("error")) == (503, "broker_unavailable")
            assert _call(port, "GET", "/cockpit/manifest.json", token=surface, headers=PROXY)[0] == 200
        finally:
            for _ in range(web_server._PAIR_VERIFY_SLOTS):
                httpd.pair_verify_gate.release()
        assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 200


# --- RT-2 / RU-3: the stream -----------------------------------------------------------------------------------------

def _open_stream(port: int, tok: str) -> tuple[socket.socket, list[tuple[float, bytes]], threading.Thread, dict]:
    s = socket.create_connection(("127.0.0.1", port), timeout=30)
    s.sendall((f"GET /cockpit/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{H}: {tok}\r\n"
               "X-Forwarded-For: 100.101.102.103\r\n\r\n").encode())
    chunks: list[tuple[float, bytes]] = []
    done: dict = {}

    def pump() -> None:
        while True:
            try:
                c = s.recv(65536)
            except OSError:
                break
            if not c:
                break
            chunks.append((time.monotonic(), c))
        done["at"] = time.monotonic()

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    return s, chunks, t, done


def _ids(chunks: list[tuple[float, bytes]]) -> list[int]:
    text = b"".join(c for _t, c in chunks).decode("utf-8", "replace")
    return [int(line.rsplit(":", 1)[1]) for line in text.splitlines() if line.startswith("id: ") and "." in line]


def test_ru3_a_revoke_during_a_steady_event_flow_ends_the_stream_before_its_next_frame(src) -> None:
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0             # events far faster than the heartbeat
        device, tok = _pair(port)
        s, chunks, pump, done = _open_stream(port, tok)
        w = httpd.stream_watcher
        stop = threading.Event()

        def publish() -> None:
            while not stop.is_set():
                with w._lock:
                    w._publish_locked("panel-changed", {"id": "tray", "etag": str(time.monotonic())})
                time.sleep(0.02)

        pub = threading.Thread(target=publish, daemon=True)
        deadline = time.monotonic() + 5
        while w.stream_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        pub.start()
        try:
            time.sleep(0.4)
            assert len(_ids(chunks)) > 3, "the flow is steady before the revoke"
            assert _call(port, "POST", "/cockpit/pair/revoke", {"id": device, "expect_digest": revoke_digest([device], "family"), "mode": "family"}, token=T)[0] == 200
            with w._lock:
                last_published = w._seq
            pump.join(5)
            assert "at" in done, "the stream did not end"
            assert max(_ids(chunks)) <= last_published
        finally:
            stop.set()
            pub.join(2)
            s.close()


def test_rt2_a_revoke_made_by_another_process_ends_the_stream_within_one_heartbeat(src) -> None:
    """RT-2 as amended by RU-3: a revoke that moves no in-process epoch (the box-side CLI writes the journal
    directly) is still seen. Since levain#231 this one ends through the journal's revocation revision, which the
    revoke (``revoke_devices``) moves, on the stream's next pass, not through the heartbeat re-check; the heartbeat
    path is pinned by ``test_231_a_revoke_that_moves_no_revision_ends_the_stream_at_the_heartbeat`` (a raw UPDATE)."""
    from datetime import datetime, timezone

    from levain.cockpit.firejournal import CockpitDB

    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 0.4
        device, tok = _pair(port)
        s, chunks, pump, done = _open_stream(port, tok)
        time.sleep(0.3)
        CockpitDB(httpd.broker.data_root).revoke_devices(None, by="box", now=datetime.now(timezone.utc))
        t0 = time.monotonic()
        pump.join(5)
        s.close()
        assert "at" in done and done["at"] - t0 < 0.4 + 0.6, done


# --- levain#231: a paired stream sees another process's revoke on its next pass ---------------------------------------
# Every test here but the heartbeat-backstop one sets ``stream_heartbeat_s = 30.0``, so the heartbeat re-check cannot
# be what ends the stream: the bounds below are the revision read on each pass, not the heartbeat. Every end bound goes
# through ``scaled`` (``_ends_within``), so a loaded runner stretches it.

def _revoke_journal(httpd: Any, ids: "list[str] | None") -> None:
    """A revoke written straight to the journal (no epoch of this server moves), as the box-side CLI writes it."""
    CockpitDB(httpd.broker.data_root).revoke_devices(ids, by="box", now=datetime.now(timezone.utc))


def _stream_up(httpd: Any, port: int, tok: str) -> tuple[socket.socket, list[tuple[float, bytes]], threading.Thread, dict]:
    s, chunks, pump, done = _open_stream(port, tok)
    w = httpd.stream_watcher
    deadline = time.monotonic() + 5
    while w.stream_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert w.stream_count == 1, "the stream did not open"
    return s, chunks, pump, done


def _flow(httpd: Any) -> tuple[threading.Event, threading.Thread]:
    """Events published faster than any check cadence, until the returned event is set."""
    w = httpd.stream_watcher
    stop = threading.Event()

    def publish() -> None:
        while not stop.is_set():
            with w._lock:
                w._publish_locked("panel-changed", {"id": "tray", "etag": str(time.monotonic())})
            time.sleep(0.02)

    pub = threading.Thread(target=publish, daemon=True)
    pub.start()
    return stop, pub


def _ends_within(pump: threading.Thread, done: dict, t0: float, bound: float) -> None:
    pump.join(5)
    assert "at" in done, "the stream did not end"
    assert done["at"] - t0 < scaled(bound), done["at"] - t0


@contextmanager
def _revokes_seen_clean_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A revoke made inside this block is read by a stream's revision pass either before it or after its clean write,
    never between the revoke's dirty marker and its clean one (a pass in that window reads None and would add a check
    a test counting checks cannot tell apart)."""
    gate = threading.Lock()
    held = [False]      # whether a dirty write took the gate; set and cleared only by the revoking thread
    real_bump = firejournal.bump_revision
    real_read = web_server._pair_revision

    def release() -> None:
        if held[0]:
            held[0] = False
            gate.release()

    def bump(root: Any, *, dirty: bool) -> None:
        if dirty and not held[0]:
            gate.acquire()
            held[0] = True
        try:
            real_bump(root, dirty=dirty)
        except BaseException:
            if dirty:
                release()       # the dirty write failed: no clean write follows, so nothing else will release
            raise
        finally:
            if not dirty:
                release()       # a clean write, however it ended, closes the window a dirty write opened (if any)

    def read(root: Any) -> "str | None":
        with gate:
            return real_read(root)

    with monkeypatch.context() as m:
        m.setattr(firejournal, "bump_revision", bump)
        m.setattr(web_server, "_pair_revision", read)
        try:
            yield
        finally:
            release()       # a dirty write whose commit failed made no clean write: the gate is let go at the end


def _spy_device_live(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = CockpitDB.device_live

    def spy(self: Any, device_id: str) -> bool:
        calls.append(device_id)
        return real(self, device_id)

    monkeypatch.setattr(CockpitDB, "device_live", spy)
    return calls


def test_231_a_revoke_by_the_terminal_cli_ends_an_open_stream_on_its_next_pass(src) -> None:
    """The box-side ``levain cockpit pair --revoke`` writes the journal directly, so no epoch of this server moves."""
    from levain.cli import main

    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0
        device, tok = _pair(port)
        s, _chunks, pump, done = _stream_up(httpd, port, tok)
        try:
            assert main(["cockpit", "pair", "--data-root", str(httpd.broker.data_root), "--revoke", device]) == 0
            t0 = time.monotonic()
            _ends_within(pump, done, t0, 0.5 + 0.6)
        finally:
            s.close()


def test_231_a_revoke_by_another_os_process_ends_an_open_stream_on_its_next_pass(src) -> None:
    repo_root = str(Path(__file__).resolve().parents[1])
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0
        device, tok = _pair(port)
        s, _chunks, pump, done = _stream_up(httpd, port, tok)
        try:
            subprocess.run([sys.executable, "-m", "levain", "cockpit", "pair", "--data-root",
                            str(httpd.broker.data_root), "--revoke", device],
                           check=True, timeout=60, env={**os.environ, "PYTHONPATH": repo_root},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            t0 = time.monotonic()
            _ends_within(pump, done, t0, 0.5 + 0.6)
        finally:
            s.close()


def test_231_a_revoke_whose_clean_write_fails_still_ends_the_open_stream(src, monkeypatch) -> None:
    """The revoke commits and its marker stays dirty (the clean write cannot be made): a stream that cannot compare
    the revision checks its device instead."""
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0
        device, tok = _pair(port)
        s, _chunks, pump, done = _stream_up(httpd, port, tok)
        try:
            real = firejournal.bump_revision
            calls: list[int] = []

            def only_the_first(*a: Any, **kw: Any) -> None:
                calls.append(1)
                if len(calls) > 1:
                    raise OSError(28, "No space left on device")
                real(*a, **kw)

            monkeypatch.setattr(firejournal, "bump_revision", only_the_first)
            monkeypatch.setattr(firejournal, "_REVISION_CLEAN_PAUSE_S", 0.0)
            _revoke_journal(httpd, [device])
            t0 = time.monotonic()
            assert revision_identity(httpd.broker.data_root) is None, "the marker stayed dirty"
            _ends_within(pump, done, t0, 0.5 + 0.6)
        finally:
            s.close()


def test_231_an_unreadable_revision_neither_ends_a_good_stream_nor_hides_a_revoke(src, monkeypatch) -> None:
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0
        device, tok = _pair(port)
        s, chunks, pump, done = _stream_up(httpd, port, tok)
        stop, pub = _flow(httpd)
        try:
            def denied(_root: Any) -> Any:
                raise PermissionError(13, "Permission denied")

            calls = _spy_device_live(monkeypatch)
            monkeypatch.setattr(web_server, "revision_identity", denied)
            before = len(_ids(chunks))
            time.sleep(1.5)
            assert "at" not in done, "a revision that cannot be read ended a good stream"
            assert len(_ids(chunks)) > before, "the stream still got frames"
            assert len(calls) > 1.5 / 0.5 + 2, f"an uncomparable revision checks on every pass, not {len(calls)} times"
            _revoke_journal(httpd, [device])
            t0 = time.monotonic()
            _ends_within(pump, done, t0, 0.5 + 0.6)
        finally:
            stop.set()
            pub.join(2)
            s.close()


def test_231_a_check_that_passed_on_a_dirty_marker_lets_no_frame_through_after_the_revoke_commits(
        src, monkeypatch) -> None:
    """L3 r1 codex HIGH: a revoke made by another process while the stream's check passes on the revoke's own dirty
    marker (the check runs between the dirty write and the commit, so it still sees the device live), and the revoke's
    clean write then fails, so the marker stays dirty. A pass that cannot compare the revision runs the device check
    every time, so no frame passes on the strength of that earlier check: at most the one frame already past its
    check when the revoke commits is written, and the stream ends."""
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0
        device, tok = _pair(port)
        s, chunks, pump, done = _stream_up(httpd, port, tok)
        w = httpd.stream_watcher
        armed, checked = threading.Event(), threading.Event()
        real_live = CockpitDB.device_live

        def live(self: Any, device_id: str) -> bool:
            out = real_live(self, device_id)
            if armed.is_set() and device_id == device:
                checked.set()
            return out

        real_bump = firejournal.bump_revision
        committed: dict = {}

        def dirty_waits_for_a_check_then_clean_fails(root: Any, *, dirty: bool) -> None:
            if dirty:
                real_bump(root, dirty=True)
                armed.set()
                assert checked.wait(scaled(5)), "the stream ran a check on the dirty marker before the commit"
                return
            if "seq" not in committed:      # the first clean write runs after the commit
                with w._lock:
                    committed["seq"] = w._seq
                committed["at"] = time.monotonic()
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(CockpitDB, "device_live", live)
        monkeypatch.setattr(firejournal, "bump_revision", dirty_waits_for_a_check_then_clean_fails)
        monkeypatch.setattr(firejournal, "_REVISION_CLEAN_PAUSE_S", 0.0)
        stop, pub = _flow(httpd)
        try:
            time.sleep(0.4)
            assert len(_ids(chunks)) > 3, "the flow is steady before the revoke"
            _revoke_journal(httpd, [device])
            assert revision_identity(httpd.broker.data_root) is None, "the marker stayed dirty"
            _ends_within(pump, done, committed["at"], 0.5 + 0.6)
            after = [i for i in _ids(chunks) if i > committed["seq"]]
            assert len(after) <= 1, f"frames published after the revoke committed were written: {after}"
        finally:
            stop.set()
            pub.join(2)
            s.close()


def test_231_a_broker_that_raises_on_a_recheck_ends_the_stream_cleanly(src, monkeypatch) -> None:
    """L3 r1 deepseek MED: the journal handle is taken inside the check's guard, so a broker that raises when a pass
    must run the device check ends the stream (a check that cannot run), never an unhandled exception in the
    stream's handler thread."""
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0
        _a, tok_a = _pair(port, label="a")
        b, _tok_b = _pair(port, label="b")
        s, _chunks, pump, done = _stream_up(httpd, port, tok_a)
        errors: list[BaseException] = []
        real_handle_error = httpd.handle_error

        def recorded(request: Any, client_address: Any) -> None:
            errors.append(sys.exc_info()[1])
            real_handle_error(request, client_address)

        monkeypatch.setattr(httpd, "handle_error", recorded)
        raised: list[int] = []

        def broken() -> Any:
            raised.append(1)
            raise RuntimeError("the broker cannot hand out its journal")

        try:
            monkeypatch.setattr(httpd.broker, "active_db", broken)
            _revoke_journal(httpd, [b])      # another device's revoke moves the revision: the next pass checks
            t0 = time.monotonic()
            _ends_within(pump, done, t0, 0.5 + 0.6)
            assert raised, "the recheck asked the broker for its journal"
            assert errors == [], f"the stream's handler thread died with an unhandled exception: {errors!r}"
        finally:
            s.close()


def test_231_a_revoke_that_moves_no_revision_ends_the_stream_at_the_heartbeat(src) -> None:
    """The backstop (design E1): a revoke that bypasses the journal's write path (a raw UPDATE of ``revoked_at``, a
    hand edit, another tool) moves neither the epoch nor the revision, so only the heartbeat re-check can see it. This
    is the one test that ends a stream that way: the revision identity is asserted unmoved, so the end cannot be the
    revision path (``test_rt2_`` and ``test_g2_`` revoke through ``revoke_devices``, which moves it).

    Red-first does not apply: it passes on the code as written. It was shown able to fail by running it with the
    heartbeat at 30 s (the stream then outlives the bound)."""
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 0.4
        device, tok = _pair(port)
        s, _chunks, pump, done = _stream_up(httpd, port, tok)
        try:
            before =revision_identity(httpd.broker.data_root)
            assert before is not None, "a clean revision to compare"
            time.sleep(0.6)         # a pass or two on the open stream first
            assert "at" not in done, "the stream is open before the revoke"
            conn = sqlite3.connect(httpd.broker.data_root / "cockpit.db", isolation_level=None, timeout=5)
            try:
                conn.execute("UPDATE paired_devices SET revoked_at = ? WHERE id = ?",
                             (datetime.now(timezone.utc).isoformat(), device))
            finally:
                conn.close()
            t0 = time.monotonic()
            assert revision_identity(httpd.broker.data_root) == before, "no revision moved: the revision path cannot end it"
            _ends_within(pump, done, t0, 0.4 + 0.6)
            assert revision_identity(httpd.broker.data_root) == before, "still unmoved when the stream ended"
        finally:
            s.close()


def test_231_a_revoke_landing_between_the_credential_check_and_the_stream_is_seen_by_the_first_check(
        src, monkeypatch) -> None:
    """The baseline is the revision read BEFORE the lookup that admitted the request: a revoke committing after
    ``verify_device`` returned moves it, so the first check (before the hello frame) sees the device gone."""
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0
        device, tok = _pair(port)
        armed = threading.Event()
        real = CockpitDB.verify_device

        def verify_then_revoke(self: Any, *a: Any, **kw: Any) -> Any:
            out = real(self, *a, **kw)
            if armed.is_set():
                armed.clear()
                _revoke_journal(httpd, [device])
            return out

        monkeypatch.setattr(CockpitDB, "verify_device", verify_then_revoke)
        armed.set()
        s, chunks, pump, done = _open_stream(port, tok)
        try:
            t0 = time.monotonic()
            _ends_within(pump, done, t0, 1.1)
            assert not armed.is_set(), "the revoke landed between the credential check and the stream"
            assert b"event: hello" not in b"".join(c for _t, c in chunks), "no hello frame was written"
        finally:
            s.close()


def test_231_a_revoke_landing_between_a_cache_hit_and_the_stream_is_seen_by_the_first_check(src, monkeypatch) -> None:
    """The hit path of the credential check: the stream is ADMITTED from the cache (no journal lookup ran for it) and
    its baseline is the revision that hit was compared with, so a revoke landing after that compare read is seen by
    the first check. The two admission asserts below are what tell this apart from a 403 refusal of a request that
    missed the cache and met the revoke in its own lookup (which also ends without a hello frame)."""
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0
        device, tok = _pair(port)
        assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 200   # primes the cache
        assert httpd.pair_verified, "the cache is primed"
        lookups: list[str] = []
        real_verify = CockpitDB.verify_device

        def counted_verify(self: Any, token: str, **kw: Any) -> Any:
            lookups.append(token)
            return real_verify(self, token, **kw)

        monkeypatch.setattr(CockpitDB, "verify_device", counted_verify)
        armed = threading.Event()
        real = web_server._pair_revision

        def read_then_revoke(root: Any) -> "str | None":
            value = real(root)
            if armed.is_set():
                armed.clear()
                _revoke_journal(httpd, [device])
            return value

        monkeypatch.setattr(web_server, "_pair_revision", read_then_revoke)
        armed.set()
        s, chunks, pump, done = _open_stream(port, tok)
        try:
            t0 = time.monotonic()
            _ends_within(pump, done, t0, 1.1)
            assert not armed.is_set(), "the revoke landed after the hit's revision read"
            assert b"event: hello" not in b"".join(c for _t, c in chunks), "no hello frame was written"
            assert lookups == [], "admitted from the cache: no journal lookup ran for this request"
            head = b"".join(c for _t, c in chunks)
            assert head.startswith(b"HTTP/1.1 200") or head.startswith(b"HTTP/1.0 200"), head[:80]
        finally:
            s.close()


def test_231_a_stream_admitted_from_the_cache_starts_with_the_hits_revision_as_its_baseline(src, monkeypatch) -> None:
    """The hit path hands the revision it compared to the stream (``self._pair_rev_seen = rev``): on an idle stream
    nothing moved, so its first pass and the passes after it run no device check. With the baseline left at None the
    first pass would run one, which is what this counts. (T5b above pins the admission and the revoke; this pins the
    assignment.)"""
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0
        _device, tok = _pair(port)
        assert _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY)[0] == 200   # primes the cache
        assert httpd.pair_verified, "the cache is primed"
        lookups: list[str] = []
        real_verify = CockpitDB.verify_device

        def counted_verify(self: Any, token: str, **kw: Any) -> Any:
            lookups.append(token)
            return real_verify(self, token, **kw)

        monkeypatch.setattr(CockpitDB, "verify_device", counted_verify)
        checks = _spy_device_live(monkeypatch)
        s, _chunks, pump, done = _stream_up(httpd, port, tok)
        try:
            time.sleep(1.2)         # two idle passes at least
            assert "at" not in done, "the admitted stream is open"
            assert lookups == [], "admitted from the cache"
            assert checks == [], f"nothing moved, so no device check ran on any pass: {checks}"
        finally:
            s.close()


def test_231_a_stream_admitted_as_a_follower_starts_with_the_leaders_revision_as_its_baseline(src, monkeypatch) -> None:
    """The follower path (``self._pair_rev_seen = flight.rev``): a stream request that waited on another request's
    lookup of the same token takes the revision that lookup read, so its idle passes run no device check."""
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0
        _device, tok = _pair(port)
        entered, release = threading.Event(), threading.Event()
        lookups: list[str] = []
        real_verify = CockpitDB.verify_device

        def held_verify(self: Any, token: str, **kw: Any) -> Any:
            lookups.append(token)
            if len(lookups) == 1:
                entered.set()
                assert release.wait(scaled(10)), "the leader's lookup was never released"
            return real_verify(self, token, **kw)

        monkeypatch.setattr(CockpitDB, "verify_device", held_verify)
        checks = _spy_device_live(monkeypatch)
        socks = [_open_stream(port, tok)]                   # the leader: its lookup is held in verify_device
        assert entered.wait(scaled(5)), "the leader's lookup began"
        socks.append(_open_stream(port, tok))               # the follower: waits on the leader's flight
        wait_until(lambda: any(f.followers >= 1 for f in list(httpd.pair_flights.values())), 5, what="a follower waiting")
        release.set()
        try:
            wait_until(lambda: httpd.stream_watcher.stream_count == 2, 5, what="both streams open")
            time.sleep(1.2)
            assert len(lookups) == 1, f"one lookup served both requests: {lookups}"
            assert checks == [], f"nothing moved, so neither stream ran a device check: {checks}"
            assert all("at" not in done for _s, _c, _p, done in socks), "both streams stay open"
        finally:
            for s, _c, _p, _d in socks:
                s.close()


def test_231_an_unrelated_devices_revoke_by_another_process_keeps_the_stream_and_checks_once(src, monkeypatch) -> None:
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0
        a, tok_a = _pair(port, label="a")
        b, _tok_b = _pair(port, label="b")
        s, chunks, pump, done = _stream_up(httpd, port, tok_a)
        calls = _spy_device_live(monkeypatch)
        stop, pub = _flow(httpd)
        try:
            time.sleep(0.2)
            calls.clear()
            with _revokes_seen_clean_only(monkeypatch):
                _revoke_journal(httpd, [b])
            before = len(_ids(chunks))
            time.sleep(1.5)
            assert "at" not in done, "another device's revoke ended this stream"
            assert len(_ids(chunks)) > before, "the stream still got frames"
            assert calls.count(a) == 1, f"one check for the one new revision, not {calls}"
            _revoke_journal(httpd, [a])
            t0 = time.monotonic()
            _ends_within(pump, done, t0, 1.1)
        finally:
            stop.set()
            pub.join(2)
            s.close()


def test_231_a_revoke_landing_during_the_streams_own_recheck_is_seen_by_the_next_pass(src, monkeypatch) -> None:
    """A check reads the revision BEFORE the journal read: a revoke landing inside the check leaves a newer revision
    than the one the check read, so the next pass sees it move. (Read after the journal, the check would hold the
    revoke's own clean revision and hide the revoke.) What it does not pin is the order of adopting against the
    check: a failing check ends the stream either way."""
    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 30.0
        a, tok_a = _pair(port, label="a")
        b, _tok_b = _pair(port, label="b")
        s, _chunks, pump, done = _stream_up(httpd, port, tok_a)
        armed = threading.Event()
        real = CockpitDB.device_live

        def live_then_revoke(self: Any, device_id: str) -> bool:
            out = real(self, device_id)
            if armed.is_set() and device_id == a:
                armed.clear()
                _revoke_journal(httpd, [a])
                return True
            return out

        monkeypatch.setattr(CockpitDB, "device_live", live_then_revoke)
        stop, pub = _flow(httpd)    # a pass per frame: the two checks are not each an idle poll apart
        try:
            armed.set()
            with _revokes_seen_clean_only(monkeypatch):
                _revoke_journal(httpd, [b])
            t0 = time.monotonic()
            _ends_within(pump, done, t0, 1.1)
            assert not armed.is_set(), "the stream's check ran and the revoke landed inside it"
        finally:
            stop.set()
            pub.join(2)
            s.close()


# --- RQ-6 and the box-side CLI ---------------------------------------------------------------------------------------

def test_rq6_a_surface_token_name_in_the_paired_namespace_is_refused(src) -> None:
    with pytest.raises(ValueError, match="paired"):
        make_server(src, host="127.0.0.1", port=0, surface_tokens={"browser": T, "paired-d-0123456789ab": "x" * 40})


def test_a_surface_token_named_box_is_refused(src) -> None:
    """`box` is the laptop's own `levain cockpit pair`, a launch-grant root (row 460): a surface holding the name would
    mint launch-capable devices over HTTP."""
    from levain.cockpit.firejournal import BOX_MINTER

    with pytest.raises(ValueError, match="laptop's own pairing"):
        make_server(src, host="127.0.0.1", port=0, surface_tokens={"browser": T, BOX_MINTER: "x" * 40})


def test_rp7_rs5_box_side_pair_and_revoke_all(src, capsys) -> None:
    """RP-7 / RS-5: `levain cockpit pair` mints straight into the journal with minted_by 'box' and prints the link to
    the terminal; --revoke-all ends every paired device."""
    from levain.cli import main

    with _serve(src) as (port, httpd):
        root = httpd.broker.data_root
        assert main(["cockpit", "pair", "--data-root", str(root), "--revoke-all"]) == 1   # no journal: nothing to end
        assert "no cockpit journal" in capsys.readouterr().err and not (root / "cockpit.db").exists()
        # W1b L3 A: a mint on a root no serve has opened yet starts the journal itself (the box's mint is an
        # off-loopback cockpit's first credential), and the running serve then opens that journal as its own
        assert main(["cockpit", "pair", "--data-root", str(root), "--label", "phone", "--print",
                     "--url", f"http://127.0.0.1:{port}/"]) == 0
        link = capsys.readouterr().out.strip()
        assert link.startswith(f"http://127.0.0.1:{port}/pair#code=")
        st, cl, _ = _call(port, "POST", "/cockpit/pair/claim", {"code": link.split("#code=", 1)[1]})
        assert st == 200
        devs = _call(port, "GET", "/cockpit/pair/devices.json", token=T)[1]["devices"]
        assert [(d["label"], d["paired_by"]) for d in devs] == [("phone", "box")]
        assert main(["cockpit", "pair", "--data-root", str(root), "--revoke-all"]) == 0
        assert "revoked 1" in capsys.readouterr().out
        assert _call(port, "GET", "/cockpit/manifest.json", token=cl["token"], headers=PROXY)[0] == 403


# --- the L1/L2 fix round (1024-18/pair_p1/L1L2_findings.md, F1..F8) -------------------------------------------------

def test_f1_a_held_writer_lock_does_not_hold_the_answer(src) -> None:
    """F1: the last_seen stamp is fail-soft with no busy wait, after the answer: a writer holding cockpit.db's lock
    costs a paired request nothing (it used to wait the 30 s busy_timeout inside a verify slot)."""
    with _serve(src) as (port, httpd):
        _d, tok = _pair(port)                         # last_seen is NULL: the next lookup is due a stamp
        holder = sqlite3.connect(httpd.broker.data_root / "cockpit.db", isolation_level=None, timeout=5)
        holder.execute("BEGIN IMMEDIATE")
        try:
            t0 = time.monotonic()
            st, out, _ = _call(port, "GET", "/cockpit/pair/devices.json", token=tok)
            took = time.monotonic() - t0
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert st == 200 and len(out["devices"]) == 1, out
        assert took < 3.0, took
        assert httpd.pair_verify_gate.acquire(blocking=False)    # every verify slot came back
        httpd.pair_verify_gate.release()


def test_f2a_a_finished_starter_in_standby_answers_at_once(src, monkeypatch) -> None:
    """F2(a): wait_settled waits only while ACTIVATING or while the starter runs; a broker settled in STANDBY (another
    process holds the ledger) answers its 503 at once, not after the whole wait."""
    monkeypatch.setattr(web_server, "_PAIR_WAIT_S", 5.0)
    with _standby(src) as (_live, port, tok, _h):
        t0 = time.monotonic()
        st, out, _ = _call(port, "GET", "/cockpit/pair/devices.json", token=tok)
        took = time.monotonic() - t0
        assert (st, out.get("error")) == (503, "broker_unavailable"), out
        assert took < 2.0, took


def test_f2b_a_starter_that_raised_is_recorded_and_cleared_so_a_later_request_retries(src, monkeypatch) -> None:
    from levain.cockpit import broker as broker_mod

    real = broker_mod.decide_install
    calls: list[int] = []

    def flaky(*a: Any, **kw: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("install check blew up")
        return real(*a, **kw)

    monkeypatch.setattr(broker_mod, "decide_install", flaky)
    with _serve(src) as (_port, httpd):
        b = httpd.broker
        b.start_async()
        first = b._starter
        assert first is not None
        first.join(5)
        assert b._starter is None, "a finished starter is cleared"
        assert isinstance(b.starter_error, RuntimeError)
        b.start_async()                                # the retry
        assert b.wait_settled(5) is Lifecycle.ACTIVE
        assert len(calls) == 2


def test_f2c_a_request_needing_no_credential_treats_unavailable_as_none(src, quick_wait) -> None:
    """F2(c), amending RT-1 / RU-1: a loopback, unproxied read on a STANDBY broker is answered as an unauthenticated
    loopback read (class none) whatever token header it carries; every route that requires a credential keeps the
    503."""
    with _standby(src) as (_live, port, tok, _h):
        for token in (JUNK, tok):
            st, man, _ = _call(port, "GET", "/cockpit/manifest.json", token=token)
            assert st == 200 and man["credential"]["class"] == "none", (st, man)
        for method, path, body, headers in (("GET", "/cockpit/pair/devices.json", None, None),
                                            ("GET", "/cockpit/manifest.json", None, PROXY),
                                            ("POST", "/cockpit/verb", {"verb": "x"}, None)):
            st, out, _ = _call(port, method, path, body, token=tok, headers=headers)
            assert (st, out.get("error")) == (503, "broker_unavailable"), (path, st, out)


def test_f5_every_pair_503_carries_retry_after_and_the_claims_closes(src, quick_wait) -> None:
    with _standby(src) as (_live, port, _tok, _h):
        for method, path, body in (("GET", "/cockpit/pair/devices.json", None),
                                   ("POST", "/cockpit/pair/offer", {}),
                                   ("POST", "/cockpit/pair/revoke", {"id": "d-0123456789ab", "expect_digest": "0" * 64, "mode": "family"})):
            st, out, hdrs = _call(port, method, path, body, token=T)
            assert (st, out.get("error"), hdrs.get("retry-after")) == (503, "broker_unavailable", "2"), (path, hdrs)
        st, out, hdrs = _call(port, "POST", "/cockpit/pair/claim", {"code": "x" * 22})
        assert (st, hdrs.get("retry-after"), hdrs.get("connection")) == (503, "2", "close"), (out, hdrs)
    with _serve(src) as (port, httpd):
        assert httpd.broker.start() is Lifecycle.ACTIVE      # quick_wait is short: a still-ACTIVATING broker answers 503 on a loaded runner (hence the scale)
        for gate, path, body in ((httpd.pair_claim_gate, "/cockpit/pair/claim", {"code": "x"}),
                                 (httpd.request_gate, "/cockpit/pair/offer", {})):
            held = 0
            while gate.acquire(blocking=False):
                held += 1
            try:
                st, out, hdrs = _call(port, "POST", path, body, token=T)
                assert (st, out.get("error"), hdrs.get("retry-after")) == (503, "busy", "2"), (path, out, hdrs)
            finally:
                for _ in range(held):
                    gate.release()


def test_f6_box_side_pair_refuses_a_non_tty_stdout_without_print(src, capsys) -> None:
    from levain.cli import main
    from levain.cockpit.firejournal import CockpitDB

    with _serve(src) as (_port, httpd):
        root = httpd.broker.data_root
        root.mkdir(parents=True, exist_ok=True)
        CockpitDB(root).meta("format")
        capsys.readouterr()
        assert main(["cockpit", "pair", "--data-root", str(root)]) == 1
        cap = capsys.readouterr()
        assert cap.out == "" and "not a terminal" in cap.err and "--print" in cap.err
        assert CockpitDB(root).list_devices() == []
        assert main(["cockpit", "pair", "--data-root", str(root), "--print"]) == 0
        assert capsys.readouterr().out.startswith("/pair#code=")


def test_f7_a_journal_fault_on_a_pair_route_is_a_500_json(src, monkeypatch) -> None:
    from levain.cockpit.firejournal import CockpitDB

    def boom(self: Any) -> Any:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(CockpitDB, "list_devices", boom)
    with _serve(src) as (port, _h):
        st, out, _ = _call(port, "GET", "/cockpit/pair/devices.json", token=T)
        assert (st, out.get("error")) == (500, "internal"), (st, out)


def test_f8_a_stream_not_allowed_before_hello_writes_no_frame() -> None:
    from types import SimpleNamespace

    from levain.cockpit import stream as cockpit_stream

    written: list[bytes] = []
    import queue

    sub = SimpleNamespace(hello={"x": 1}, hello_id="e:0", dead=False, q=queue.Queue())
    cockpit_stream.run(written.append, sub, stop=threading.Event(), allowed=lambda beat: False)
    assert written == []


# --- the L3 r1 fix round (1024-18/pair_p1/L3_r1_findings.md, G1..G6) -------------------------------------------------

def test_g1_a_pre_auth_pair_refusal_closes_without_reading_the_body(src) -> None:
    """G1 (F4 deleted): a pair POST refused before its body is read answers and closes at once; it never waits on a
    declared body that has not arrived (a slow-body client would hold a server thread outside any gate)."""
    with _serve(src) as (port, _httpd):
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        try:
            s.sendall((f"POST /cockpit/pair/offer HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                       "Content-Type: application/json\r\nContent-Length: 500\r\n\r\n").encode())   # no body follows
            got = b""
            t0 = time.monotonic()
            while b"\r\n\r\n" not in got:
                c = s.recv(65536)
                if not c:
                    break
                got += c
            took = time.monotonic() - t0
        finally:
            s.close()
        head = got.split(b"\r\n\r\n", 1)[0].decode("latin-1").lower()
        assert head.startswith("http/1.0 403") or head.startswith("http/1.1 403"), head
        assert "connection: close" in head, head
        assert took < 2.0, took


def test_g2_a_cross_process_revoke_during_a_steady_event_flow_ends_the_stream_within_one_heartbeat(src) -> None:
    """G2: the DB liveness re-check runs on its own deadline on EVERY pass, the data path included: events flowing
    faster than the heartbeat no longer starve it. Since levain#231 THIS revoke (``revoke_devices`` writes the
    journal directly, no epoch bump, but does move the revision) ends the stream through the revision compare on its
    next frame, well inside one heartbeat; the heartbeat re-check itself, which covers a revoke that moves no
    revision, is pinned by ``test_231_a_revoke_that_moves_no_revision_ends_the_stream_at_the_heartbeat``."""
    from datetime import datetime, timezone

    from levain.cockpit.firejournal import CockpitDB

    with _serve(src) as (port, httpd):
        httpd.stream_heartbeat_s = 0.4
        _device, tok = _pair(port)
        s, chunks, pump, done = _open_stream(port, tok)
        w = httpd.stream_watcher
        stop = threading.Event()

        def publish() -> None:
            while not stop.is_set():
                with w._lock:
                    w._publish_locked("panel-changed", {"id": "tray", "etag": str(time.monotonic())})
                time.sleep(0.02)

        pub = threading.Thread(target=publish, daemon=True)
        deadline = time.monotonic() + 5
        while w.stream_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        pub.start()
        try:
            time.sleep(0.3)
            assert len(_ids(chunks)) > 3, "the flow is steady before the revoke"
            CockpitDB(httpd.broker.data_root).revoke_devices(None, by="box", now=datetime.now(timezone.utc))
            t0 = time.monotonic()
            pump.join(3)
            assert "at" in done, "the stream did not end under a steady event flow"
            assert done["at"] - t0 < 0.4 + 0.6, done["at"] - t0
        finally:
            stop.set()
            pub.join(2)
            s.close()


def test_g4_a_synchronous_start_in_progress_is_waited_on(src, monkeypatch) -> None:
    """G4: one start-in-progress signal. A synchronous start() still before ACTIVATING (its install check running)
    is waited on, not answered STANDBY at once; and once it ends the signal is clear."""
    from levain.cockpit import broker as broker_mod

    real = broker_mod.decide_install
    entered, release = threading.Event(), threading.Event()

    def slow(*a: Any, **kw: Any) -> Any:
        entered.set()
        release.wait(5)
        return real(*a, **kw)

    monkeypatch.setattr(broker_mod, "decide_install", slow)
    with _serve(src) as (_port, httpd):
        b = httpd.broker
        t = threading.Thread(target=b.start, daemon=True)
        t.start()
        assert entered.wait(5)
        threading.Timer(0.3, release.set).start()
        assert b.wait_settled(5) is Lifecycle.ACTIVE
        t.join(5)
        assert b._starting is False


def test_g5_a_starter_thread_that_cannot_start_is_a_503_and_is_retried(src, monkeypatch) -> None:
    """G5: Thread.start() failing in start_async clears the starter and the start signal, records starter_error,
    and the pair route answers the controlled 503 (broker_unavailable, Retry-After: 2); the next request retries."""
    from types import SimpleNamespace

    from levain.cockpit import broker as broker_mod

    class NoThread(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    with _serve(src) as (port, httpd):
        b = httpd.broker
        monkeypatch.setattr(broker_mod, "threading", SimpleNamespace(Thread=NoThread))
        st, out, hdrs = _call(port, "GET", "/cockpit/pair/devices.json", token=T)
        assert (st, out.get("error"), hdrs.get("retry-after")) == (503, "broker_unavailable", "2"), (st, out, hdrs)
        assert b._starter is None and b._starting is False
        assert isinstance(b.starter_error, RuntimeError)
        monkeypatch.undo()
        st, out, _ = _call(port, "GET", "/cockpit/pair/devices.json", token=T)
        assert st == 200 and out["devices"] == [], out


def test_rq6_the_point_of_use_check_raises_without_assert(src) -> None:
    """RQ-6 at the point of use: a paired-namespace name reaching the token loop is refused by a raise, so
    ``python -O`` cannot strip it."""
    with _serve(src) as (port, httpd):
        httpd.surface_tokens["paired-d-0123456789ab"] = "x" * 40
        handler = httpd.RequestHandlerClass
        h = handler.__new__(handler)
        h.server = httpd
        h.headers = {}
        with pytest.raises(RuntimeError, match="paired namespace"):
            h._resolve_credential()
