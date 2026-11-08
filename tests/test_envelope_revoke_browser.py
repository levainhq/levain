"""Board row 153, the page: the Decisions view draws an "Envelopes" card beside the standing grants, each live envelope with a
Revoke button that runs the grant revoke's passkey ceremony. Headless Chrome with a virtual authenticator on
``http://localhost`` (the real server half signs only on an https name, see test_envelope_revoke.py), a recording
EnvelopeRevoke in the real one's place, and a SCRATCH envelope store. ``LEVAIN_ENVREVOKE_SHOTS`` names a folder to leave
screenshots in."""
from __future__ import annotations

import base64
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization

from levain.consent.grant_phone import GrantRefused
from levain.consent.passkey import challenge
from levain.coordinator.envelope_revoke import NAMESPACE, EnvelopeRevoke, revoke_message
from tests.test_cockpit_consent_rows import OPEN_DECISIONS, TILE
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_sign_origin import _Passkey
from tests.test_envelope_revoke import EID, _seed, _signer
from tests.test_cockpit_grant_revoke import _free_port, _serve_on
from tests.test_grant_phone import world  # noqa: F401  (world is a fixture)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

NARROW = {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}
WIDE = {"emulate": {"w": 1280, "h": 900, "dpr": 1, "mobile": False}}
_CARD = f"document.querySelector('#levain-modal [data-envelope=\"{EID}\"]')"
_SHOTS = os.environ.get("LEVAIN_ENVREVOKE_SHOTS")
# the modal fades in (opacity 0 -> 1): a shot waits until the fade has finished
_FADED = "getComputedStyle(document.querySelector('#levain-modal .modal') || document.querySelector('#levain-modal')).opacity === '1'"


class _Recording:
    """The real listing with the localhost passkey as its one signer, and a step that records what the page sent and
    answers the challenge the real ceremony would (the SSHSIG blob over the message text)."""

    def __init__(self, real: EnvelopeRevoke, fp: str) -> None:
        self.real, self.fp, self.steps = real, fp, []

    def listing(self) -> dict[str, Any]:
        got = self.real.listing()
        got["signers"], got["note"] = [self.fp], ""
        return got

    def step(self, envelope_id: str, action: str, req: Any) -> dict[str, Any]:
        self.steps.append((envelope_id, action, req))
        if action == "revoke-challenge":
            line = revoke_message("5" * 32, envelope_id, "2026-11-08T20:00:00.000000Z", "n" * 32)
            blob = challenge(line.encode(), NAMESPACE)
            return {"envelope_id": envelope_id, "rp_id": "localhost", "signer_fp": req["signer_fp"], "line": line,
                    "namespace": NAMESPACE, "challenge": base64.urlsafe_b64encode(blob).decode().rstrip("=")}
        if action == "revoke":
            return {"revoked": envelope_id, "warning": ""}
        raise GrantRefused(404, "not_found", "no such route")


def _shot(name: str) -> list[dict[str, Any]]:
    return [{"shot": str(Path(_SHOTS) / name)}] if _SHOTS else []


def _authenticator(pk: _Passkey) -> dict[str, Any]:
    pkcs8 = pk.priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
    return {"authenticator": {"rpId": "localhost", "privateKey": base64.b64encode(pkcs8).decode(),
                              "credentialId": base64.b64encode(b"levain-envelope-cred").decode(),
                              "userHandle": base64.b64encode(b"levain-envelope-user").decode()}}


_METRICS = ("(() => { const b = " + _CARD + ".querySelector('.envelope-revoke').getBoundingClientRect();"
            " return {sideways: document.documentElement.scrollWidth > window.innerWidth + 1,"
            " bodySideways: document.querySelector('#levain-modal').scrollWidth > window.innerWidth + 1,"
            " h: b.height, w: b.width, left: b.left, right: b.right, vw: window.innerWidth}; })()")


@needs_browser
@pytest.mark.parametrize("viewport,tag", [(NARROW, "390"), (WIDE, "desktop")])
def test_the_card_lists_a_scratch_envelope_and_revoke_runs_the_ceremony(world, tmp_path, viewport, tag) -> None:
    _seed(world.qdir)
    port = _free_port()
    pk = _Passkey("localhost")
    _root, src, _ids = _install(tmp_path)
    signer = _signer(world, rp="localhost", origin=f"http://localhost:{port}")
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        rec = _Recording(httpd.envelope_revoke, pk.key.fingerprint)
        httpd.envelope_revoke = rec
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            _authenticator(pk), viewport, {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD} && {_CARD}.querySelector('.envelope-revoke').title === 'revoke with your passkey'"},
            {"eval": f"{_CARD}.querySelector('.consent-head').textContent"},
            {"eval": f"{_CARD}.querySelector('.envelope-revoke').textContent"},
            {"eval": _METRICS},
            {"eval": f"{_CARD}.querySelector('.consent-facts').textContent"},
            {"until": _FADED},
            *_shot(f"envelopes-{tag}.png"),
            {"tap": f"{_CARD}.querySelector('.envelope-revoke')"},
            {"posts": 2},
            {"until": "!!document.querySelector('#levain-modal .envelope-said')"},
            {"eval": "document.querySelector('#levain-modal .envelope-said').textContent"},
            {"until": _FADED},
            *_shot(f"envelopes-{tag}-revoked.png"),
        ])
    values = [v for v in out["values"] if not (isinstance(v, dict) and "shot" in v)]
    m = next(v for v in values if isinstance(v, dict) and "sideways" in v)
    head = next(v for v in values if isinstance(v, str) and "morning seats" in v)
    button = next(v for v in values if v == "Revoke")
    facts = next(v for v in values if isinstance(v, str) and EID[:11] in v)
    said = next(v for v in values if isinstance(v, str) and v.startswith("Revoked "))
    # the expiry is plain words in local time, never the raw stamp
    assert "until" not in facts and "000000Z" not in facts, facts
    assert said == f"Revoked {EID[:11]}. Work it authorised stops at its next step.", said
    assert "run the morning seats on flow" in head and "ends in" in head, head
    assert button == "Revoke", button
    # the phone width: nothing scrolls sideways and the button is a full-size target inside the screen
    assert m["sideways"] is False and m["bodySideways"] is False, m
    assert m["h"] >= 43.9 and m["w"] >= 43.9 and m["left"] >= 0 and m["right"] <= m["vw"], m
    paths = [p.split(" ", 1)[0] for p in out["posts"] if p.startswith("/cockpit/envelope/")]   # the page's own trace posts are not the ceremony
    assert paths == [f"/cockpit/envelope/{EID}/revoke-challenge", f"/cockpit/envelope/{EID}/revoke"], out["posts"]
    (e1, a1, r1), (e2, a2, r2) = rec.steps
    assert (e1, a1) == (EID, "revoke-challenge") and r1["signer_fp"] == pk.key.fingerprint
    assert r1["instance"] == {e["id"]: e for e in rec.real.listing()["envelopes"]}[EID]["instance"]
    assert (e2, a2) == (EID, "revoke") and set(r2) == {"authenticator_data", "client_data_json", "signature"}
    cdj = json.loads(base64.urlsafe_b64decode(r2["client_data_json"] + "==="))
    assert cdj["type"] == "webauthn.get" and cdj["origin"] == f"http://localhost:{port}", cdj


@needs_browser
def test_an_empty_store_says_no_live_envelopes_and_the_grants_jump_lands_on_it(world, tmp_path) -> None:
    port = _free_port()
    _root, src, _ids = _install(tmp_path)
    signer = _signer(world, rp="localhost", origin=f"http://localhost:{port}")
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer):
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            NARROW, {"until": TILE},
            {"eval": "document.querySelector('[data-phone-jump=grants]').click(); 1"},
            {"until": "[...document.querySelectorAll('#levain-modal .ext-grouphead')].some(h => h.textContent.trim() === 'Envelopes')"},
            {"eval": "document.querySelector('#levain-modal .envelope-empty').textContent"},
            *_shot("envelopes-empty-390.png"),
        ])
    values = [v for v in out["values"] if not (isinstance(v, dict) and "shot" in v)]
    assert values[-1] == "No live envelopes", values
