"""ONESURFACE OS-4: web push on the cockpit's own origin. The service worker, manifest and icons are token-free (they
carry no data, and a browser fetches them without the page's credential); every ``/webpush/*`` data route needs the
cockpit's credential on every bind; only a PAIRED device may subscribe, so every subscription has a revoke that stops
it; revoking the device in the cockpit retires its subscriptions; the phone port's own pairing is not served here."""
from __future__ import annotations

import base64
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from levain.consent.sign import Signer
from levain.cockpit.firejournal import PAIRED_PREFIX
from levain.web_server import make_server
from levain.webpush.store import PushStore
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call, _pair, _serve
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import _free_port

NS = "flow-needs-phill"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _sub(n: int = 1) -> dict[str, Any]:
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": f"https://fcm.googleapis.com/fcm/send/dev{n}", "keys": {"p256dh": _b64(point),
                                                                                "auth": _b64(b"a" * 16)}}


@pytest.fixture
def rig(tmp_path: Path) -> tuple[Any, Path]:
    qdir = tmp_path / "signq"
    qdir.mkdir(mode=0o700)
    return _install(tmp_path)[1], qdir


@contextmanager
def _signed(src: Any, qdir: Path) -> Iterator[tuple[int, Any]]:
    """The cockpit served with a Signer over ``qdir`` (no passkeys enrolled), pinned to this server's own page."""
    port = _free_port()
    signer = Signer(qdir, (NS,), "localhost", lambda: {})
    signer.page_origin = f"http://localhost:{port}"
    httpd = make_server(src, host="127.0.0.1", port=port, surface_tokens={"browser": T}, install_class="reported",
                        signer=signer)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_without_a_signer_no_push_route_is_served(rig) -> None:
    src, _ = rig
    with _serve(src) as (port, _):
        for path in ("/sw.js", "/app.webmanifest", "/webpush/key"):
            assert _call(port, "GET", path, token=T)[0] == 404, path
        assert _call(port, "POST", "/webpush/subscribe", _sub(), token=T)[0] == 404


def test_the_worker_manifest_and_icons_are_token_free_and_the_manifest_pairs_nothing(rig) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        for path in ("/sw.js", "/app.webmanifest", "/icon-192.png", "/apple-touch-icon.png"):
            for hdrs in (None, PROXY):
                assert _call(port, "GET", path, headers=hdrs)[0] == 200, (path, hdrs)
        st, manifest, _ = _call(port, "GET", "/app.webmanifest?pair=abcdefgh12", headers=PROXY)
        assert st == 200 and manifest["start_url"] == "/"     # the cockpit pairs through /pair (OS-5), never here


def test_every_push_data_route_needs_the_cockpits_credential_on_every_bind(rig) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        for hdrs in (None, PROXY):
            assert _call(port, "GET", "/webpush/key", headers=hdrs)[0] == 403
            assert _call(port, "GET", "/webpush/record", headers=hdrs)[0] == 403
            for route in ("/webpush/subscribe", "/webpush/unsubscribe", "/webpush/opened"):
                assert _call(port, "POST", route, {"ping": 1}, headers=hdrs)[0] == 403, (route, hdrs)
        st, key, _ = _call(port, "GET", "/webpush/key", token=T)
        assert st == 200 and len(key["key"]) > 40
        # a page from another site cannot read the key or the record even with a token
        assert _call(port, "GET", "/webpush/record", token=T, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403


def test_the_phone_ports_own_pairing_is_not_served_on_the_cockpit(rig) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        assert _call(port, "POST", "/webpush/pair", {"label": "x"}, token=T)[0] == 404
        assert _call(port, "POST", "/webpush/pair/redeem", {"short": "abcd1234"})[0] in (403, 404)
        assert _call(port, "POST", "/webpush/pair/redeem", {"short": "abcd1234"}, token=T)[0] == 404


def test_only_a_paired_device_subscribes_and_its_revoke_stops_its_pings(rig) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        st, out, _ = _call(port, "POST", "/webpush/subscribe", _sub(1), token=T)
        assert st == 403 and out["error"] == "device_required", out      # a surface token has no revoke
        dev, tok = _pair(port)
        other_dev, other_tok = _pair(port, label="phone")
        st, out, _ = _call(port, "POST", "/webpush/subscribe", _sub(2), token=tok, headers=PROXY)
        assert st == 200 and isinstance(out["id"], int) and "token" not in out, out
        assert _call(port, "POST", "/webpush/subscribe", _sub(3), token=other_tok)[0] == 200
        live = {s["endpoint"]: s["device_id"] for s in PushStore(qdir).live_subscriptions()}
        assert live == {_sub(2)["endpoint"]: PAIRED_PREFIX + dev, _sub(3)["endpoint"]: PAIRED_PREFIX + other_dev}
        st, out, _ = _call(port, "POST", "/cockpit/pair/revoke", {"id": dev}, token=T)
        assert st == 200 and dev in out["revoked"], out
        live = {s["endpoint"]: s["device_id"] for s in PushStore(qdir).live_subscriptions()}
        assert live == {_sub(3)["endpoint"]: PAIRED_PREFIX + other_dev}
        assert _call(port, "GET", "/webpush/key", token=tok)[0] == 403


def test_an_open_is_recorded_under_the_device_that_opened_it(rig) -> None:
    src, qdir = rig
    store = PushStore(qdir)
    ping = store.add_ping("sign", "np-x", "t", "b")
    with _signed(src, qdir) as (port, _):
        dev, tok = _pair(port)
        st, out, _ = _call(port, "POST", "/webpush/opened", {"ping": ping}, token=tok)
        assert st == 200 and out == {"recorded": True}, out
        st, rec, _ = _call(port, "GET", "/webpush/record", token=tok)
    assert st == 200
    opened = [o for p in rec["pings"] if p.get("id") == ping for o in p.get("opens", [])]
    assert [o.get("device_id") for o in opened] == [PAIRED_PREFIX + dev], rec


def test_the_worker_sends_the_credential_under_the_header_its_page_named() -> None:
    sw = (Path(__file__).resolve().parents[1] / "levain" / "webpush" / "static" / "sw.js").read_text()
    assert "'X-Levain-Write-Token'" in sw and "'X-Levain-Phone-Token'" in sw


# --- fix round 1: one liveness rule, checked against the cockpit's pairing journal at send time ---------------------

@pytest.fixture
def pushes(monkeypatch) -> list[str]:
    """The consent notification's real path with two seams: the detached child runs in this process (``spawn`` only
    detaches) and the network POST is recorded instead of sent. Returns the endpoints each push went to."""
    from levain.webpush import presence, sender

    sent: list[str] = []

    def spawn(queue_dir, **kw):
        sender.send(queue_dir, **kw)
        return True

    monkeypatch.setattr(sender, "spawn", spawn)
    monkeypatch.setattr(sender, "_post", lambda req: sent.append(req.full_url) or 201)
    monkeypatch.setattr(presence, "should_push", lambda *a, **k: presence.Decision(True, "pushing", "test"))
    return sent


def _notify(qdir: Path) -> bool:
    from levain.consent.adapters import claude_code as cc

    return cc.notify(cc.AdapterSettings(), "levain: needs you", cwd=qdir, kind="consent", ref="request:x")


def test_a_cockpit_paired_subscription_is_live_to_the_gate_and_the_consent_ping_reaches_it(rig, pushes) -> None:
    from levain.webpush import live

    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        _dev, tok = _pair(port)
        assert _call(port, "POST", "/webpush/subscribe", _sub(1), token=tok)[0] == 200
        assert live(qdir) is True
        assert _notify(qdir) is True
    assert pushes == [_sub(1)["endpoint"]]


def test_a_revoke_whose_tidy_up_fails_still_stops_every_ping(rig, pushes, monkeypatch) -> None:
    import sqlite3

    from levain.webpush import live, store as push_store

    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        dev, tok = _pair(port)
        _keep, keep_tok = _pair(port, label="phone")
        assert _call(port, "POST", "/webpush/subscribe", _sub(1), token=tok)[0] == 200
        assert _call(port, "POST", "/webpush/subscribe", _sub(2), token=keep_tok)[0] == 200

        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(push_store, "retire_device_subscriptions", locked)
        st, out, _ = _call(port, "POST", "/cockpit/pair/revoke", {"id": dev}, token=T)
        assert st == 200 and dev in out["revoked"] and "push_note" in out, out
        assert "again" not in out["push_note"], out          # a retry finds nothing to revoke: never advised
        # the row was never retired, yet no reader counts it and no ping reaches it
        assert {s["endpoint"] for s in PushStore(qdir).live_subscriptions()} == {_sub(2)["endpoint"]}
        assert live(qdir) is True and _notify(qdir) is True
    assert pushes == [_sub(2)["endpoint"]]


def test_revoke_all_from_the_box_stops_every_ping_with_no_push_call(rig, pushes) -> None:
    from levain.cockpit.firejournal import CockpitDB
    from levain.webpush import live

    src, qdir = rig
    with _signed(src, qdir) as (port, httpd):
        _dev, tok = _pair(port)
        assert _call(port, "POST", "/webpush/subscribe", _sub(1), token=tok)[0] == 200
        root = httpd.broker.data_root
    from datetime import datetime, timezone

    db = CockpitDB(root)
    try:
        assert db.revoke_devices(None, by="box", now=datetime.now(timezone.utc))
    finally:
        close = getattr(db, "close", None)
        if close is not None:
            close()
    assert PushStore(qdir).live_subscriptions() == [] and live(qdir) is False
    assert _notify(qdir) is False and pushes == []


def test_a_subscribe_authenticated_before_a_revoke_and_committed_after_it_is_refused(rig, pushes) -> None:
    """The two steps in order: the subscribe's headers (its credential checked and cached), the revoke, then the
    subscribe's body. The subscribe re-checks the device inside its write and refuses; nothing is stored or sent."""
    import json
    import socket

    from levain.webpush import live

    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        dev, tok = _pair(port)
        body = json.dumps(_sub(1)).encode()
        s = socket.create_connection(("127.0.0.1", port), timeout=20)
        try:
            s.sendall(("POST /webpush/subscribe HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\n"
                       "X-Levain-Write-Token: %s\r\nContent-Length: %d\r\n\r\n" % (port, tok, len(body))).encode()
                      + body[:5])
            st, out, _ = _call(port, "POST", "/cockpit/pair/revoke", {"id": dev}, token=T)
            assert st == 200 and dev in out["revoked"], out
            s.sendall(body[5:])
            reply = b""
            while b"\r\n\r\n" not in reply:
                chunk = s.recv(4096)
                if not chunk:
                    break
                reply += chunk
        finally:
            s.close()
        assert reply.startswith(b"HTTP/1.1 403") or reply.startswith(b"HTTP/1.0 403"), reply[:200]
        assert live(qdir) is False and PushStore(qdir).live_subscriptions() == []
        assert _notify(qdir) is False
    assert pushes == []


def test_a_subscription_stored_for_a_revoked_device_is_never_sent_to(rig, pushes) -> None:
    """Whatever wrote it, a row under a revoked paired device is not live: the send asks the journal."""
    from levain.webpush import live

    src, qdir = rig
    with _signed(src, qdir) as (port, httpd):
        dev, tok = _pair(port)
        assert _call(port, "POST", "/webpush/subscribe", _sub(1), token=tok)[0] == 200
        assert _call(port, "POST", "/cockpit/pair/revoke", {"id": dev}, token=T)[0] == 200
        store = PushStore(qdir)
        with store.write() as c:     # a row a tidy-up never reached, revived as a stalled subscribe would
            c.execute("UPDATE subscriptions SET retired_at = NULL")
        assert store.live_subscriptions() == [] and live(qdir) is False
        assert _notify(qdir) is False
    assert pushes == []


def test_the_push_store_names_the_journal_the_way_the_journal_does() -> None:
    from levain.cockpit import firejournal
    from levain.webpush import store

    assert store.PAIRED_PREFIX == firejournal.PAIRED_PREFIX and store._JOURNAL_DB == firejournal.DB_NAME


def test_a_paired_subscription_without_its_journal_is_refused_and_an_unreadable_journal_is_not_live(tmp_path) -> None:
    from levain.webpush.store import paired_device_live

    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    s = _sub(1)
    with pytest.raises(ValueError):
        PushStore(q).add_subscription(s["endpoint"], s["keys"]["p256dh"], s["keys"]["auth"], PAIRED_PREFIX + "d-1")
    assert paired_device_live(tmp_path / "nowhere", PAIRED_PREFIX + "d-1") is False
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "cockpit.db").write_bytes(b"not a database")
    assert paired_device_live(tmp_path / "bad", PAIRED_PREFIX + "d-1") is False


# --- the cockpit page's own control (fix round 1, P4): the Devices panel turns pings on for this browser ------------

def _fake_push(sub: dict[str, Any]) -> str:
    """An in-page stand-in for the browser's push service only: the permission is granted and ``subscribe`` hands back
    ``sub`` (a test reaches no real push service). The page code, the worker and the server are real."""
    import json

    return ("(() => { const s = " + json.dumps(sub) + "; let held = null;"
            " const fake = { endpoint: s.endpoint, toJSON: () => s, unsubscribe: async () => { held = null; return true; } };"
            " Notification.requestPermission = async () => 'granted';"
            " PushManager.prototype.subscribe = async function () { held = fake; return fake; };"
            " PushManager.prototype.getSubscription = async function () { return held; };"
            " return 1; })()")


_ROW = "document.querySelector('.dev-pings')"
_WORKER_KV = ("new Promise((ok) => { const o = indexedDB.open('levain-sw', 1); o.onerror = () => ok('no db');"
              " o.onsuccess = () => { try { const st = o.result.transaction('kv').objectStore('kv'); const a = st.get('header');"
              " const b = st.get('token'); b.onsuccess = () => ok([a.result, b.result]); } catch (e) { ok(String(e)); } }; })")


def _panel(tok: str, sub: dict[str, Any], extra: "list[dict]") -> "list[dict]":
    return [{"until": f"{_ROW} && !{_ROW}.hidden", "ms": 20000}, {"eval": _fake_push(sub)},
            {"eval": f"{_ROW}.querySelector('button').click() || 1"}, *extra]


@needs_browser
def test_the_devices_panel_turns_pings_on_for_a_paired_browser(rig, pushes, tmp_path) -> None:
    from levain.webpush import live

    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        dev, tok = _pair(port, label="iPhone")
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={tok}", _panel(tok, _sub(7), [
            {"until": f"{_ROW}.querySelector('button').textContent === 'Turn off'", "ms": 20000},
            {"eval": f"{_ROW}.querySelector('.dev-meta').textContent"},
            # the worker writes what it was told asynchronously: wait until it holds it
            {"until": f"{_WORKER_KV}.then((r) => Array.isArray(r) && r[1] === {tok!r} && r)", "ms": 20000},
        ]))
        assert any(p.startswith("/webpush/subscribe ") for p in out["posts"]), out["posts"]
        assert [s["device_id"] for s in PushStore(qdir).live_subscriptions()] == [PAIRED_PREFIX + dev]
        assert live(qdir) is True
        assert _notify(qdir) is True
    v = out["values"]
    assert v[4].startswith("on:"), v
    assert v[5] == ["X-Levain-Write-Token", tok], v              # the worker sends the open under the cockpit's header
    assert pushes == [_sub(7)["endpoint"]]


@needs_browser
def test_the_cockpits_own_key_is_told_to_pair_first_and_keeps_nothing(rig, tmp_path) -> None:

    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={T}", _panel(T, _sub(8), [
            {"until": "document.querySelector('.dev-note') && !document.querySelector('.dev-note').hidden"
                      " && document.querySelector('.dev-note').textContent", "ms": 20000},
            {"eval": f"{_ROW}.querySelector('button').textContent"},
        ]))
        assert PushStore(qdir).live_subscriptions() == []
    v = out["values"]
    assert v[3].startswith("Pings need this browser's own key"), v
    assert v[4] == "Turn on", v                                  # the browser's subscription was dropped too


@needs_browser
def test_a_failed_key_read_is_asked_again_and_the_pings_row_appears(rig, tmp_path, monkeypatch) -> None:
    """A first GET /webpush/key that fails (not a 200, not a 404) is not "this cockpit serves no web push": the panel asks
    again on its next read and shows the row."""
    from levain.webpush import routes, vapid

    real, calls = vapid.ensure_keys, []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("key store busy")
        return real(*a, **k)

    monkeypatch.setattr(routes.vapid, "ensure_keys", flaky)
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        _dev, tok = _pair(port, label="iPhone")
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token={tok}", [
            {"until": f"{_ROW} && !{_ROW}.hidden && 1", "ms": 20000},
        ])
    assert len(calls) >= 2, calls
    assert out["values"][0] == 1, out
