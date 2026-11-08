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
from datetime import datetime, timedelta, timezone
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
from tests.test_envelope_revoke import EID, EID2, _seed, _signer
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

    def __init__(self, real: EnvelopeRevoke, fp: str, *, bad: "list[tuple[str, str]] | None" = None,
                 fail_refill: bool = False, expires_in: "float | None" = None) -> None:
        self.real, self.fp, self.steps = real, fp, []
        self.bad = list(bad or [])              # (line, namespace) answered by each challenge in turn
        self.fail_refill = fail_refill          # the first listing after a revoke fails
        self.expires_in = expires_in            # seconds from now, written over every envelope's expiry
        self.lines: list[str] = []

    def listing(self) -> dict[str, Any]:
        if self.fail_refill and any(a == "revoke" for _e, a, _r in self.steps):
            self.fail_refill = False
            raise RuntimeError("the refill's read fails once")
        got = self.real.listing()
        got["signers"], got["note"] = [self.fp], ""
        if self.expires_in is not None:
            at = datetime.now(timezone.utc) + timedelta(seconds=self.expires_in)
            for e in got["envelopes"]:
                e["expires"] = at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return got

    def step(self, envelope_id: str, action: str, req: Any) -> dict[str, Any]:
        self.steps.append((envelope_id, action, req))
        if action == "revoke-challenge":
            line, ns = self.bad.pop(0) if self.bad else (
                revoke_message("5" * 32, envelope_id, "2026-11-08T20:00:00.000000Z", "e" * 32), NAMESPACE)
            self.lines.append(line)
            blob = challenge(line.encode(), NAMESPACE)
            return {"envelope_id": envelope_id, "rp_id": "localhost", "signer_fp": req["signer_fp"], "line": line,
                    "namespace": ns, "challenge": base64.urlsafe_b64encode(blob).decode().rstrip("=")}
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
            {"until": _LANDED},       # the jump landed on the heading, not only drew it
            {"eval": "document.querySelector('#levain-modal .envelope-empty').textContent"},
            *_shot("envelopes-empty-390.png"),
        ])
    values = [v for v in out["values"] if not (isinstance(v, dict) and "shot" in v)]
    assert values[-1] == "No live envelopes", values


# --- L3 r1 fix round ---------------------------------------------------------------------------------------------------

_ENV_HEAD = "[...document.querySelectorAll('#levain-modal .ext-grouphead')].find(h => h.textContent.trim() === 'Envelopes')"
# the jump LANDED: the heading is drawn and inside the screen
_LANDED = ("(() => { const h = " + _ENV_HEAD + "; if (!h) return false; const r = h.getBoundingClientRect();"
           " return r.height > 0 && r.top >= 0 && r.bottom <= window.innerHeight; })()")
# every passkey prompt the page opens records what the signed text on the screen was at that moment
_WRAP_GET = ("(() => { window.__envGets = []; const c = navigator.credentials; const g = c.get.bind(c);"
             " c.get = (o) => { const n = document.querySelector('#levain-modal .envelope-line');"
             " const r = n ? n.getBoundingClientRect() : null;"
             " window.__envGets.push(n ? {text: n.textContent, shown: n.getClientRects().length > 0, top: r.top,"
             " bottom: r.bottom, vh: window.innerHeight} : null); return g(o); }; return 1; })()")
_GOOD = revoke_message("5" * 32, EID, "2026-11-08T20:00:00.000000Z", "e" * 32)


def _served(world, tmp_path, port: int):
    pk = _Passkey("localhost")
    _root, src, _ids = _install(tmp_path)
    signer = _signer(world, rp="localhost", origin=f"http://localhost:{port}")
    return pk, src, signer


class _Failing(_Recording):
    def listing(self) -> dict[str, Any]:
        raise RuntimeError("the envelopes cannot be read")


@needs_browser
def test_the_signed_text_is_on_the_screen_when_the_passkey_asks(world, tmp_path) -> None:
    """codex MED-2: the sheet shows the exact text signed, before the passkey prompt opens."""
    _seed(world.qdir)
    port = _free_port()
    pk, src, signer = _served(world, tmp_path, port)
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        rec = _Recording(httpd.envelope_revoke, pk.key.fingerprint)
        httpd.envelope_revoke = rec
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            _authenticator(pk), NARROW, {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD} && {_CARD}.querySelector('.envelope-revoke').title === 'revoke with your passkey'"},
            {"eval": _WRAP_GET},
            {"tap": f"{_CARD}.querySelector('.envelope-revoke')"},
            {"posts": 2},
            {"eval": "window.__envGets"},
        ])
    gets = [v for v in out["values"] if isinstance(v, list)][-1]
    assert len(gets) == 1 and gets[0] is not None, gets
    assert gets[0]["text"] == rec.lines[0] == _GOOD, gets
    assert gets[0]["shown"] and gets[0]["top"] >= 0 and gets[0]["bottom"] <= gets[0]["vh"], gets


@needs_browser
def test_a_line_with_an_extra_or_altered_field_is_never_put_to_the_passkey(world, tmp_path) -> None:
    """codex MED-2: the page checks the whole line's grammar, the namespace and the envelope id before WebAuthn."""
    _seed(world.qdir)
    bad = [(_GOOD + "extra: x\n", NAMESPACE),
           (_GOOD.replace("at: 2026-11-08T20:00:00.000000Z", "at: tomorrow"), NAMESPACE),
           (_GOOD.replace("levain-envelope-revoke/1", "levain-envelope-revoke/2"), NAMESPACE),
           (revoke_message("5" * 32, EID2, "2026-11-08T20:00:00.000000Z", "e" * 32), NAMESPACE),
           (_GOOD, "levain-grant-revoke")]
    port = _free_port()
    pk, src, signer = _served(world, tmp_path, port)
    note = f"{_CARD}.querySelector('.sign-note')"
    steps: list[dict[str, Any]] = []
    for _ in bad:
        steps += [{"until": f"!{_CARD}.querySelector('.envelope-revoke').disabled"},
                  {"eval": f"{note}.textContent = ''; 1"},
                  {"tap": f"{_CARD}.querySelector('.envelope-revoke')"},
                  {"until": f"{note}.textContent.startsWith('not revoked')"},
                  {"eval": f"{note}.textContent"}]
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        rec = _Recording(httpd.envelope_revoke, pk.key.fingerprint, bad=bad)
        httpd.envelope_revoke = rec
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            _authenticator(pk), NARROW, {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD} && {_CARD}.querySelector('.envelope-revoke').title === 'revoke with your passkey'"},
            {"eval": _WRAP_GET}, *steps, {"eval": "window.__envGets"}])
    said = [v for v in out["values"] if isinstance(v, str) and v.startswith("not revoked")]
    assert len(said) == len(bad) and all("nothing was signed" in v for v in said), said
    assert [v for v in out["values"] if isinstance(v, list)][-1] == [], "a bad line was put to the passkey"
    assert [a for _e, a, _r in rec.steps] == ["revoke-challenge"] * len(bad), rec.steps


@needs_browser
def test_a_failed_envelope_read_still_draws_the_heading_and_the_jump_lands_on_it(world, tmp_path) -> None:
    """deepseek MED-4 / complement LOW-5: the envelopes' slot says it could not read, and the grants jump lands there."""
    port = _free_port()
    pk, src, signer = _served(world, tmp_path, port)
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        httpd.envelope_revoke = _Failing(httpd.envelope_revoke, pk.key.fingerprint)
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            NARROW, {"until": TILE},
            {"eval": "document.querySelector('[data-phone-jump=grants]').click(); 1"},
            {"until": _LANDED},
            {"eval": "document.querySelector('#levain-modal .envelope-unavailable').textContent"},
        ])
    assert out["values"][-1] == "The envelopes cannot be read here now", out["values"]


@needs_browser
def test_under_a_minute_left_reads_less_than_a_minute(world, tmp_path) -> None:
    _seed(world.qdir)
    port = _free_port()
    pk, src, signer = _served(world, tmp_path, port)
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        httpd.envelope_revoke = _Recording(httpd.envelope_revoke, pk.key.fingerprint, expires_in=40)
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            NARROW, {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD}"},
            {"eval": f"{_CARD}.querySelector('.consent-head').textContent"},
        ])
    assert out["values"][-1].endswith("ends in <1 min"), out["values"]


@needs_browser
def test_a_revoke_said_over_a_failed_refill_is_said_once(world, tmp_path) -> None:
    """deepseek LOW-7: a failed refill says the revoke once and clears it; a later read never repeats it."""
    _seed(world.qdir)
    port = _free_port()
    pk, src, signer = _served(world, tmp_path, port)
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        httpd.envelope_revoke = _Recording(httpd.envelope_revoke, pk.key.fingerprint, fail_refill=True)
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            _authenticator(pk), NARROW, {"until": TILE}, {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD} && {_CARD}.querySelector('.envelope-revoke').title === 'revoke with your passkey'"},
            {"tap": f"{_CARD}.querySelector('.envelope-revoke')"},
            {"posts": 2},
            {"until": "!!document.querySelector('#levain-modal .envelope-unavailable')"},
            {"eval": "(document.querySelector('#levain-modal .envelope-said') || {}).textContent || ''"},
            {"eval": "document.querySelector('#levain-modal .modal-close').click(); 1"},
            {"until": "!document.getElementById('levain-modal')"},
            {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD}"},
            {"eval": "!!document.querySelector('#levain-modal .envelope-said')"},
        ])
    values = [v for v in out["values"] if not (isinstance(v, dict) and "shot" in v)]
    assert values[-1] is False, values
    assert any(isinstance(v, str) and v.startswith("Revoked ") for v in values), values


_FAIL_GRANTS = ("(() => { const f = window.fetch.bind(window);"
                " window.fetch = (u, o) => String(u).includes('/cockpit/consent/grants.json')"
                " ? Promise.resolve(new Response('{}', {status: 500})) : f(u, o); return 1; })()")


@needs_browser
def test_a_failed_grants_read_still_marks_its_slot_filled_so_the_jump_lands(world, tmp_path) -> None:
    """L3 r2 (codex, complement, deepseek): the grants read answering null while the envelope read answers must not leave
    the grants slot unfilled, or the phone Grants-tab jump waits on it and lands nowhere."""
    _seed(world.qdir)
    port = _free_port()
    pk, src, signer = _served(world, tmp_path, port)
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        httpd.envelope_revoke = _Recording(httpd.envelope_revoke, pk.key.fingerprint)
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            NARROW, {"until": TILE}, {"eval": _FAIL_GRANTS},
            {"eval": "document.querySelector('[data-phone-jump=grants]').click(); 1"},
            {"until": _LANDED},
            {"eval": "document.querySelector('#levain-modal .grant-slot').dataset.filled"},
        ])
    assert out["values"][-1] == "1", out["values"]
