"""PLAN M17 "9a", the cockpit half (lane 1024+3 d7): the cockpit's grant "revoke" button signs with the passkey on the
https cockpit, through the phone's own ceremony (:class:`levain.consent.grant_phone.GrantPhone`), behind the D5 sign
routes' gate. ``make_server`` builds ONE GrantPhone from the caller's Signer (its queue, rpId, pinned page origin and
keys; the root the queue's consent hook names) and serves ``GET /cockpit/grant/list.json`` and
``POST /cockpit/grant/<id>/{revoke-challenge,revoke}``; without a Signer none of them exists, and the page keeps the
copy-the-command button.

The server half runs a real consent grant signed by a real laptop key and a software passkey (``world``); the page half
drives headless Chrome with a virtual authenticator on ``http://localhost`` and a recording GrantPhone in the real one's
place, since the authority log takes a passkey's revoke only from an https name (a localhost page cannot make one)."""
from __future__ import annotations

import base64
import json
import shutil
import socket
import threading
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest
from cryptography.hazmat.primitives import serialization

from levain import authority as auth
from levain.consent.answering import passkey_keys
from levain.consent.grant_phone import GrantPhone, GrantRefused
from levain.consent.grants import grant_listing
from levain.consent.passkey import challenge
from levain.consent.queue import authority_source
from levain.consent.sign import Signer
from levain.web_server import make_server
from tests.test_cockpit_consent_rows import OPEN_DECISIONS, TILE
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_sign_origin import _Passkey
from tests.test_cockpit_sign_routes import _call, _serving
from tests.test_grant_phone import RP, world  # noqa: F401  (world is a fixture)
from tests.test_phone_grants_tab import _grant

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

NS = "flow-needs-phill"
# the replay takes a passkey's revoke only from the key's own https name exactly, no port (levain.consent.passkey)
PAGE = f"https://{RP}"
PORTED = f"https://{RP}:8443"
GID = "cg-0000000000d7"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _signer(world, rp: str = RP, origin: str = PAGE) -> Signer:
    source = authority_source(world.root / "cockpit.db", anchor=auth.anchor_path(world.root))
    signer = Signer(world.qdir, (NS,), rp, lambda: {k.fingerprint: k for k in passkey_keys(source(), rp)})
    signer.page_origin = origin
    return signer


def test_the_grant_routes_ride_the_sign_gate_and_revoke_a_real_grant(world, tmp_path) -> None:
    """Reproduced red before the build: ``/cockpit/grant/list.json`` and ``.../revoke-challenge`` were 404 with a
    Signer attached."""
    _grant(world, GID, "Write")
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (url, _httpd):
        q = urllib.parse.quote(GID, safe="")
        # the D5 gate: no token, a wrong token, a cross-site page, a non-JSON body: refused before the ceremony runs
        assert _call(url, "/cockpit/grant/list.json", token=None)[0] == 403
        assert _call(url, "/cockpit/grant/list.json", sfs="cross-site")[0] == 403
        for token in (None, "wrong-token"):
            status, out = _call(url, f"/cockpit/grant/{q}/revoke-challenge", {"signer_fp": world.phone.fp,
                                                                              "instance": "x"}, token=token)
            assert (status, out.get("error")) == (403, "credential_required"), (token, status, out)
        assert _call(url, f"/cockpit/grant/{q}/revoke-challenge", {"signer_fp": "x", "instance": "x"},
                     ctype="text/plain")[0] == 415
        status, listed = _call(url, "/cockpit/grant/list.json")
        assert status == 200 and listed["origin"] == PAGE, listed
        row = {g["id"]: g for g in listed["grants"]}[GID]
        assert row["signers"] == [world.phone.fp] and "revoke" not in row, row
        # the phone's refusal words, as the page shows them: an instance that is not this grant's
        status, out = _call(url, f"/cockpit/grant/{q}/revoke-challenge",
                            {"signer_fp": world.phone.fp, "instance": "not-this-one"})
        assert (status, out["error"]) == (409, "grant_changed") and "nothing was signed" in out["message"], out
        status, c = _call(url, f"/cockpit/grant/{q}/revoke-challenge",
                          {"signer_fp": world.phone.fp, "instance": row["instance"]})
        assert status == 200 and json.loads(c["line"])["op"] == "revoke_grant", c
        assert json.loads(c["line"])["grant_id"] == GID and c["rp_id"] == RP
        status, out = _call(url, f"/cockpit/grant/{q}/revoke", world.phone.assertion(c["challenge"], origin=PAGE))
        assert status == 200 and out["revoked"] == GID, out
    # the authority log itself (a fresh replay verifies every link, the passkey's too) no longer holds the grant
    assert grant_listing(world.root, now=_now())[0] == []


def test_a_cockpit_on_a_port_offers_no_passkey_revoke_and_says_why(world, tmp_path) -> None:
    """The replay refuses a revoke signed on ``https://<name>:8443`` (reproduced: 403 "the passkey signed on a page
    that is not https://phone.example (exactly: no port); nothing was revoked", after the Face ID). So the listing
    names no signer and says why, the page keeps the copy button, and a challenge is refused before any gesture."""
    _grant(world, GID, "Write")
    with _serving(tmp_path / "inst", signer=_signer(world, origin=PORTED), extra_hosts=[RP]) as (url, _httpd):
        status, listed = _call(url, "/cockpit/grant/list.json")
        assert status == 200 and listed["origin"] == PORTED, listed
        row = {g["id"]: g for g in listed["grants"]}[GID]
        assert row["signers"] == [] and "exactly (no port)" in listed["grants_note"], listed
        status, out = _call(url, f"/cockpit/grant/{GID}/revoke-challenge",
                            {"signer_fp": world.phone.fp, "instance": row["instance"]})
        assert (status, out["error"]) == (403, "not_a_signer") and "no port" in out["message"], out
    assert [g.id for g in grant_listing(world.root, now=_now())[0]] == [GID]


def test_without_a_signer_no_grant_route_exists(world, tmp_path) -> None:
    _grant(world, GID, "Write")
    with _serving(tmp_path / "inst") as (url, _httpd):
        # the page reads "no signing here" off this 404 and keeps the copy button (the D5 listing's own channel)
        assert _call(url, "/cockpit/grant/list.json")[0] == 404
        for action in ("revoke-challenge", "revoke"):
            assert _call(url, f"/cockpit/grant/{GID}/{action}", {})[0] == 404
    assert [g.id for g in grant_listing(world.root, now=_now())[0]] == [GID]


# --- the page ----------------------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _serve_on(port: int, src: Any, **kw: Any) -> Iterator[Any]:
    """``_serve`` on a chosen port: the Signer is pinned to ``http://localhost:<port>`` before the bind."""
    httpd = make_server(src, host="127.0.0.1", port=port, **kw)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


class _Recording:
    """The real GrantPhone's listing, with the localhost passkey as its one signer, and a step that records what the
    page sent and answers the challenge the real ceremony would: the SSHSIG blob over ``prev_hash``, a newline, then
    the link's line."""

    def __init__(self, real: GrantPhone, fp: str) -> None:
        self.real, self.fp, self.steps = real, fp, []

    def listing(self) -> dict[str, Any]:
        got = self.real.listing()
        for g in got["grants"]:
            g["signers"] = [self.fp]
        return got

    def step(self, grant_id: str, action: str, req: Any) -> dict[str, Any]:
        self.steps.append((grant_id, action, req))
        if action == "revoke-challenge":
            line = json.dumps({"op": "revoke_grant", "grant_id": grant_id}, separators=(",", ":"))
            prev = "0" * 64
            blob = challenge((prev + "\n" + line).encode(), auth.NAMESPACE)
            return {"grant_id": grant_id, "rp_id": "localhost", "signer_fp": req["signer_fp"], "line": line,
                    "prev_hash": prev, "challenge": base64.urlsafe_b64encode(blob).decode().rstrip("=")}
        if action == "revoke":
            return {"revoked": grant_id, "seq": 7, "warning": ""}
        raise GrantRefused(404, "not_found", "no such route")


_CARD = f"document.querySelector('#levain-modal [data-consent-grant=\"{GID}\"]')"


@needs_browser
def test_the_cockpit_revoke_button_runs_the_phones_ceremony_with_the_passkey(world, tmp_path, monkeypatch) -> None:
    """Reproduced red before the build: the button's title said "This page signs nothing" and a click sent no POST."""
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    _grant(world, GID, "Write")
    port = _free_port()
    pk = _Passkey("localhost")
    _root, src, _ids = _install(tmp_path)
    signer = _signer(world, rp="localhost", origin=f"http://localhost:{port}")
    pkcs8 = pk.priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
    auth_step = {"authenticator": {"rpId": "localhost", "privateKey": base64.b64encode(pkcs8).decode(),
                                   "credentialId": base64.b64encode(b"levain-grant-cred").decode(),
                                   "userHandle": base64.b64encode(b"levain-grant-user").decode()}}
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        rec = _Recording(httpd.grant_phone, pk.key.fingerprint)
        httpd.grant_phone = rec
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            auth_step, {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD} && {_CARD}.querySelector('.grant-revoke').title === 'revoke with your passkey'"},
            {"eval": f"!!{_CARD}.querySelector('.consent-code')"},
            {"eval": f"{_CARD}.querySelector('.grant-revoke').click(), 1"},
            {"posts": 2},
            {"until": f"/revoked/.test(({_CARD} || {{textContent: 'revoked'}}).textContent) "
                      "|| /revoked/.test(document.querySelector('#levain-modal').textContent)"},
        ])
    # the copy line stays under the signing button
    assert out["values"][-2:] == [True, 1], out["values"]
    paths = [p.split(" ", 1)[0] for p in out["posts"]]
    assert paths == [f"/cockpit/grant/{GID}/revoke-challenge", f"/cockpit/grant/{GID}/revoke"], out["posts"]
    (g1, a1, r1), (g2, a2, r2) = rec.steps
    instance = {g["id"]: g for g in rec.real.listing()["grants"]}[GID]["instance"]
    assert (g1, a1, r1) == (GID, "revoke-challenge", {"signer_fp": pk.key.fingerprint, "instance": instance})
    assert (g2, a2) == (GID, "revoke") and set(r2) == {"authenticator_data", "client_data_json", "signature"}
    # the assertion the virtual authenticator made is over the challenge the page was sent, from this page
    cdj = json.loads(base64.urlsafe_b64decode(r2["client_data_json"] + "==="))
    assert cdj["type"] == "webauthn.get" and cdj["origin"] == f"http://localhost:{port}", cdj
