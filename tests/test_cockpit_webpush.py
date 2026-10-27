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
