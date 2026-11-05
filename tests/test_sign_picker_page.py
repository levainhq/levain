"""The multi-passkey picker (1105-7): a plain sign request names its signer in the ANSWER, so the page never draws a pick list
for it, remembers the key the cockpit's reply NAMES, and a bound ceremony (a consent answer, a grant revoke) that still has
to name its key before the assertion draws a pick list whose buttons say which passkey is which.

Headless Chrome with virtual authenticators on ``http://localhost:<port>`` (rpId localhost, the Signer pinned to that page),
the rigs of test_cockpit_sign_card (the dashboard's needs card), test_sign_sheet_page (the sheet), test_cockpit_consent_sheet_page
(a bound consent answer) and test_cockpit_grant_revoke (a bound revoke). The server half is test_sign_key_late."""
from __future__ import annotations

import base64
import dataclasses
import json
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization

from levain.cockpit.keys import fingerprint_of
from tests._phone_fixtures import _enrol
from tests.test_cockpit_consent_sheet_page import FRAME as CONSENT_FRAME  # noqa: F401  (the sheet's frame, for reference)
from tests.test_cockpit_consent_sheet_page import _ConsentRig
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import DESKTOP, OPTION_B, QRID, STRIP_FACET, TOK, FACET_NOTE, _Rig, needs_ssh
from tests.test_cockpit_sign_origin import NS, _Passkey
from tests.test_sign_sheet_page import NOTE, _button, _open

LS = "window.localStorage.getItem('levain-passkeys:localhost')"
PICKS = "document.querySelectorAll('#levain-modal .needs-question .sign-pick').length"


def _auth(pk: _Passkey, name: bytes) -> dict:
    """A virtual authenticator step holding ``pk``'s credential, whose id is ``name``."""
    pkcs8 = pk.priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
    return {"authenticator": {"rpId": "localhost", "privateKey": base64.b64encode(pkcs8).decode(),
                              "credentialId": base64.b64encode(name).decode(),
                              "userHandle": base64.b64encode(b"user-" + name).decode()}}


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _signed_by(rig: _Rig, rid: str = QRID) -> str:
    return json.loads((rig.qdir / "sign" / f"{rid}.signed.json").read_text())["signer_fp"]


def _verifies_under(rig: _Rig, pk: _Passkey, tmp_path: Path, rid: str = QRID) -> bool:
    rec = json.loads((rig.qdir / "sign" / f"{rid}.signed.json").read_text())
    allowed = tmp_path / "allowed_one"
    allowed.write_text(f'x namespaces="{NS}" {pk.line}\n')
    return subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "x", "-n", NS, "-s",
                           str(rig.qdir / "sign" / f"{rid}.sig")], input=rec["signed_message"].encode(),
                          capture_output=True).returncode == 0


def _challenge_bodies(out: dict) -> list[dict]:
    return [json.loads(p.split(" ", 1)[1]) for p in out["posts"] if p.split(" ", 1)[0].endswith("/challenge")]


# --- B1: the dashboard's needs card ---------------------------------------------------------------------------------


@needs_browser
@needs_ssh
def test_b1_two_enrolled_and_this_device_holds_one_the_card_signs_with_no_pick_and_remembers_the_key_that_signed(
        tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch, two_keys=True)
    other = rig.other
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, _auth(other, b"cred-b"),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"tap": f"{OPTION_B}"},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": PICKS},
            {"eval": LS},
        ])
    v = out["values"]
    assert v[-2] == 0, v                                        # no pick list was ever drawn (one would have stalled the tap)
    assert _signed_by(rig) == other.key.fingerprint and _verifies_under(rig, other, tmp_path)
    assert json.loads(v[-1]) == {other.key.fingerprint: _b64u(b"cred-b")}, v    # the key the REPLY named, not one asked for
    assert all("signer_fp" not in b for b in _challenge_bodies(out)), out["posts"]


@needs_browser
@needs_ssh
def test_b5_a_device_holding_both_credentials_signs_with_no_page_pick(tmp_path: Path, monkeypatch) -> None:
    """The synced-passkey case: the OS lists every levain passkey this device holds and either one signs."""
    rig = _Rig(tmp_path, monkeypatch, two_keys=True)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, _auth(rig.pk, b"cred-a"), _auth(rig.other, b"cred-b"),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"tap": f"{OPTION_B}"},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": PICKS},
            {"eval": LS},
        ])
    v = out["values"]
    assert v[-2] == 0, v
    signer = _signed_by(rig)
    assert signer in (rig.pk.key.fingerprint, rig.other.key.fingerprint)
    by = rig.pk if signer == rig.pk.key.fingerprint else rig.other
    assert _verifies_under(rig, by, tmp_path)
    assert list(json.loads(v[-1])) == [signer], v               # it remembers the one that signed


# --- B4: the sheet ----------------------------------------------------------------------------------------------------


@needs_browser
@needs_ssh
def test_b4_the_sheets_plain_request_signs_with_no_pick_either(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch, two_keys=True)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, _auth(rig.other, b"cred-b"), *_open(rig),
            {"tap": f"{_button('b')}"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": "document.querySelectorAll('#sheet-actions .sheet-key').length"},
            {"eval": LS},
        ])
    v = out["values"]
    assert v[-2] == 0, v
    assert _signed_by(rig) == rig.other.key.fingerprint and _verifies_under(rig, rig.other, tmp_path)
    assert json.loads(v[-1]) == {rig.other.key.fingerprint: _b64u(b"cred-b")}, v
    assert all("signer_fp" not in b for b in _challenge_bodies(out)), out["posts"]


# --- B3: a bound ceremony keeps the pick list, and the labels say which passkey is which ---------------------------------


def _consent_rig_with_two_passkeys(tmp_path: Path, monkeypatch) -> "tuple[_ConsentRig, _Passkey]":
    rig = _ConsentRig(tmp_path, monkeypatch)
    laptop = tmp_path / "laptop"
    lpub = " ".join((tmp_path / "laptop.pub").read_text().split()[:2])
    second = _Passkey("localhost")
    conn = sqlite3.connect(tmp_path / "ck" / "cockpit.db", isolation_level=None)
    _enrol(conn, tmp_path / "ck", laptop, fingerprint_of(lpub), {
        "op": "enrol", "fingerprint": second.key.fingerprint, "public_key": second.line, "class": "display-phone",
        "custody": "passkey"})
    conn.close()
    return rig, second


@needs_browser
@needs_ssh
def test_b3_a_bound_consent_answer_draws_a_pick_whose_labels_say_which_passkey_is_which(tmp_path: Path, monkeypatch) -> None:
    rig, second = _consent_rig_with_two_passkeys(tmp_path, monkeypatch)
    keys = rig.signer.signers()
    assert len(keys) == 2
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *rig.open_top(),
            {"tap": "document.querySelector('#sheet-actions [data-choice=\"allow\"]')"},
            {"until": "document.querySelectorAll('#sheet-actions .sheet-key').length === 3 && 1", "ms": 30000},
            {"eval": "[...document.querySelectorAll('#sheet-actions .sheet-key')].map(b => [b.textContent, b.title])"},
            {"eval": "document.getElementById('sheet-note').textContent"},
            {"eval": "[...document.querySelectorAll('#sheet-actions .sheet-key')].find(b => b.textContent === 'Dismiss').click() || 1"},
        ])
    buttons, note = out["values"][-3], out["values"][-2]
    labelled = [b for b in buttons if b[0] != "Dismiss"]
    assert sorted(t for _, t in labelled) == sorted(keys), buttons                      # the title is the whole fingerprint
    for text, fp in labelled:
        assert re.fullmatch(r"provider not recorded · enrolled \d\d-\d\d \d\d:\d\d \(#\d+\)", text), text
        assert not text.startswith("SHA256")
    assert len({t for t, _ in labelled}) == 2, buttons                                   # and the two read differently (the number)
    assert "any one on this device works" in note, note
    assert rig.state() == "pending"                                                      # nothing was signed


@needs_browser
@needs_ssh
def test_b3b_a_bound_consent_answer_takes_the_newest_of_the_keys_this_browser_remembers(tmp_path: Path, monkeypatch) -> None:
    rig, second = _consent_rig_with_two_passkeys(tmp_path, monkeypatch)
    older, newer = rig.pk, second          # enrolled in that order: the second has the higher number in the log
    record = {older.key.fingerprint: "b2xk", newer.key.fingerprint: _b64u(b"levain-consent-cred")}
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(),
            {"eval": f"window.localStorage.setItem('levain-passkeys:localhost', {json.dumps(json.dumps(record))}); 1"},
            *rig.open_top(),
            {"tap": "document.querySelector('#sheet-actions [data-choice=\"allow\"]')"},
            {"until": f"/^signed|^Not signed|^Not sure/.test({NOTE_C}) && {NOTE_C}", "ms": 30000},
            {"eval": "document.querySelectorAll('#sheet-actions .sheet-key').length"},
        ])
    bodies = [json.loads(p.split(" ", 1)[1]) for p in out["posts"] if p.split(" ", 1)[0].endswith("/challenge")]
    assert [b["signer_fp"] for b in bodies] == [newer.key.fingerprint], bodies           # asked for the newest, no pick
    assert out["values"][-1] == 0


NOTE_C = "document.getElementById('sheet-note').textContent"


# --- B2, R4, R9: the grant revoke, a bound ceremony ----------------------------------------------------------------------

from tests.test_cockpit_consent_rows import OPEN_DECISIONS, TILE  # noqa: E402
from tests.test_cockpit_grant_revoke import GID, _CARD, _Recording, _free_port, _serve_on  # noqa: E402
from tests.test_cockpit_k2a import _install  # noqa: E402
from tests.test_grant_phone import world  # noqa: E402,F401  (a fixture)
from tests._grant_helpers import _grant  # noqa: E402
from levain.consent.sign import Signer  # noqa: E402

_REVOKE = f"{_CARD}.querySelector('.grant-revoke')"
_GRANT_PICKS = f"{_CARD}.querySelectorAll('.sign-pick').length"
_CLOSE = "(document.querySelector('#levain-modal .modal-close') || {click() {}}).click() || 1"


class _TwoSigners(_Recording):
    """The real listing with two passkeys as its signers (the listing the page reads), and the recording step."""

    def __init__(self, real: Any, fps: "list[str]") -> None:
        super().__init__(real, fps[0])
        self.fps = fps

    def listing(self) -> dict[str, Any]:
        got = self.real.listing()
        for g in got["grants"]:
            g["signers"] = sorted(self.fps)
        return got


def _grant_rig(world, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    _grant(world, GID, "Write")
    port = _free_port()
    older, newer = _Passkey("localhost"), _Passkey("localhost")
    older.key = dataclasses.replace(older.key, enrolled_seq=2, enrolled_at="2026-11-02T09:30:00+00:00")
    newer.key = dataclasses.replace(newer.key, enrolled_seq=5, enrolled_at="2026-11-05T12:13:00+00:00")
    keys = {k.key.fingerprint: k.key for k in (older, newer)}
    _root, src, _ids = _install(tmp_path)
    signer = Signer(world.qdir, (NS,), "localhost", lambda: dict(keys))
    signer.page_origin = f"http://localhost:{port}"
    return port, src, signer, older, newer


@needs_browser
@needs_ssh
def test_b2_a_bound_revoke_asks_for_the_key_this_browser_remembers_and_draws_no_pick(world, tmp_path, monkeypatch) -> None:
    port, src, signer, older, newer = _grant_rig(world, tmp_path, monkeypatch)
    record = {newer.key.fingerprint: _b64u(b"cred-new")}
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        rec = _TwoSigners(httpd.grant_phone, [older.key.fingerprint, newer.key.fingerprint])
        httpd.grant_phone = rec
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            _auth(newer, b"cred-new"), {"until": TILE},
            {"eval": f"window.localStorage.setItem('levain-passkeys:localhost', {json.dumps(json.dumps(record))}); 1"},
            {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD} && !!{_REVOKE} && 1"},
            {"tap": _REVOKE},
            {"posts": 2},
            {"eval": _GRANT_PICKS},
        ])
    (_g1, a1, r1), (_g2, a2, _r2) = rec.steps
    assert (a1, a2) == ("revoke-challenge", "revoke")
    assert r1["signer_fp"] == newer.key.fingerprint, r1                 # it asked for the remembered key, no pick drawn


@needs_browser
@needs_ssh
def test_b2b_a_bound_revoke_with_several_remembered_keys_takes_the_newest_enrolled(world, tmp_path, monkeypatch) -> None:
    port, src, signer, older, newer = _grant_rig(world, tmp_path, monkeypatch)
    record = {older.key.fingerprint: _b64u(b"cred-old"), newer.key.fingerprint: _b64u(b"cred-new")}
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        # the older key listed first: taking the first remembered key would ask for it
        rec = _TwoSigners(httpd.grant_phone, [older.key.fingerprint, newer.key.fingerprint])
        httpd.grant_phone = rec
        _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            _auth(newer, b"cred-new"), {"until": TILE},
            {"eval": f"window.localStorage.setItem('levain-passkeys:localhost', {json.dumps(json.dumps(record))}); 1"},
            {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD} && !!{_REVOKE} && 1"},
            {"tap": _REVOKE},
            {"posts": 2},
        ])
    assert rec.steps[0][2]["signer_fp"] == newer.key.fingerprint, rec.steps


@needs_browser
@needs_ssh
def test_b3_r4_r9_the_revokes_pick_labels_the_keys_survives_a_redraw_and_a_dismiss_posts_nothing(
        world, tmp_path, monkeypatch) -> None:
    """B3: a fresh browser, two passkeys: the pick's buttons carry the provider the enrolment recorded (here none: said so),
    the enrol date and number, with the whole fingerprint as the title. R4: a pick list a redraw detached settles and
    releases the lease, so the new card's tap draws a new pick list. R9: a dismissed pick posts nothing."""
    port, src, signer, older, newer = _grant_rig(world, tmp_path, monkeypatch)
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        rec = _TwoSigners(httpd.grant_phone, [older.key.fingerprint, newer.key.fingerprint])
        httpd.grant_phone = rec
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            _auth(newer, b"cred-new"), {"until": TILE},
            {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD} && !!{_REVOKE} && 1"},
            {"tap": _REVOKE},
            {"until": f"{_GRANT_PICKS} === 3 && 1"},
            {"eval": f"[...{_CARD}.querySelectorAll('.sign-pick')].map(b => [b.textContent, b.title])"},
            {"eval": f"{_CARD}.querySelector('.sign-note').textContent"},
            # R4: the card is drawn again while the pick is open
            {"eval": _CLOSE},
            {"eval": "new Promise((r) => setTimeout(() => r(1), 800))"},
            {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD} && !!{_REVOKE} && 1"},
            {"tap": _REVOKE},
            {"until": f"{_GRANT_PICKS} === 3 && 1"},
            # R9: dismissed
            {"eval": f"[...{_CARD}.querySelectorAll('.sign-pick')].find(b => b.textContent === 'dismiss').click() || 1"},
            {"eval": f"{_CARD}.querySelector('.sign-note').textContent"},
            {"eval": _GRANT_PICKS},
        ])
    v = out["values"]
    buttons = {title: text for text, title in v[5] if text != "dismiss"}
    assert set(buttons) == {older.key.fingerprint, newer.key.fingerprint}, v[5]
    for fp, text in buttons.items():
        assert re.fullmatch(r"provider not recorded · enrolled \d\d-\d\d \d\d:\d\d \(#[25]\)", text), text
    assert buttons[older.key.fingerprint].endswith("(#2)") and buttons[newer.key.fingerprint].endswith("(#5)"), buttons
    assert "any one on this device works" in v[6], v[6]
    assert "nothing was revoked" in v[-2], v[-2]
    assert v[-1] == 0, v[-1]                                              # the pick is gone after a dismiss
    assert rec.steps == [] and out["posts"] == [], (rec.steps, out["posts"])


@needs_browser
@needs_ssh
def test_i5_the_consent_sheets_signer_picker_is_not_a_ceremony_in_flight(tmp_path: Path, monkeypatch) -> None:
    """Moved from test_sign_sheet_page (a plain request draws no picker now): the picker of a bound consent answer, opened in
    the cockpit's overlay frame, is closed at once by the sheet's close, and nothing was posted."""
    rig, _second = _consent_rig_with_two_passkeys(tmp_path, monkeypatch)
    frame = "document.getElementById('levain-sign-sheet-frame')"
    inside = f"({frame} && {frame}.contentDocument)"
    allow = "document.querySelector('#levain-modal .needs-consent [data-consent-sheet=\"allow\"]')"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": "document.querySelector('#ck-needs .needs-consent')", "ms": 30000},
            {"eval": "document.querySelector('#ck-needs .needs-consent').click() || 1"},
            {"until": f"{allow} && 1", "ms": 30000},
            {"eval": f"{allow}.click() || 1"},
            {"until": f"{inside} && {inside}.getElementById('sheet') && {inside}.getElementById('sheet').dataset.state === 'open'",
             "ms": 30000},
            {"tap": f"{inside}.getElementById('sheet-picked')"},
            {"until": f"{inside}.querySelector('#sheet-actions .sheet-key') && 1", "ms": 30000},
            {"eval": f"{inside}.getElementById('sheet-close').click() || 1"},
            {"until": f"!{frame} && 1", "ms": 3000, "may_timeout": True},
            {"eval": f"!!{frame}"},
        ])
    assert out["values"][-1] is False, out["values"]                     # the close leaves at once
    assert not [p for p in out["posts"] if p.split(" ", 1)[0].endswith("/challenge")], out["posts"]


# --- L3 r1 fix (project_memory/1105-7/L3_picker_r1.txt): a remembered key this device no longer holds -------------------

import hashlib  # noqa: E402

from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from levain.consent.grant_phone import GrantRefused  # noqa: E402


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class _Verifying(_TwoSigners):
    """The two-signer listing, and a revoke that checks the assertion under the key its challenge was bound to, as the
    real ceremony does: a signature by any other key is refused (a 4xx with the server's error word)."""

    def __init__(self, real: Any, keys: "list[_Passkey]") -> None:
        super().__init__(real, [k.key.fingerprint for k in keys])
        self.pubs = {k.key.fingerprint: k.priv.public_key() for k in keys}
        self.bound: "str | None" = None

    def step(self, grant_id: str, action: str, req: Any) -> dict[str, Any]:
        if action != "revoke":
            if action == "revoke-challenge":
                self.bound = req.get("signer_fp")
            return super().step(grant_id, action, req)
        self.steps.append((grant_id, action, req))
        signed = _unb64u(req["authenticator_data"]) + hashlib.sha256(_unb64u(req["client_data_json"])).digest()
        try:
            self.pubs[self.bound].verify(_unb64u(req["signature"]), signed, ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature, KeyError):
            raise GrantRefused(400, "bad_signature", "the passkey's signature does not verify over this message") from None
        return {"revoked": grant_id, "seq": 7, "warning": ""}


_GRANT_NOTE = f"((({_CARD}) || document).querySelector('.sign-note') || {{}}).textContent"


@needs_browser
@needs_ssh
def test_b6_a_bound_revoke_whose_newest_remembered_key_is_gone_asks_the_next_remembered_key(
        world, tmp_path, monkeypatch) -> None:
    """The deadlock (gemini HIGH, codex MED): two remembered keys, the device holds only the older. The first tap asks for
    the newest by its id and the authenticator refuses it; the next tap must bind the challenge to the OTHER remembered key
    (never the refused one with the OS's whole list, which signs with a key the challenge is not bound to), sign, and
    forget the refused record so the tap after it does not fail first."""
    port, src, signer, older, newer = _grant_rig(world, tmp_path, monkeypatch)
    record = {older.key.fingerprint: _b64u(b"cred-old"), newer.key.fingerprint: _b64u(b"cred-new")}
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        rec = _Verifying(httpd.grant_phone, [older, newer])
        httpd.grant_phone = rec
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            _auth(older, b"cred-old"), {"until": TILE},
            {"eval": f"window.localStorage.setItem('levain-passkeys:localhost', {json.dumps(json.dumps(record))}); 1"},
            {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD} && !!{_REVOKE} && 1"},
            {"tap": _REVOKE},
            {"until": f"/nothing was revoked/.test({_GRANT_NOTE}) && !{_REVOKE}.disabled && {_GRANT_NOTE}", "ms": 30000},
            {"tap": _REVOKE},
            {"until": f"/^revoked|^not revoked|^result unknown/.test({_GRANT_NOTE}) && {_GRANT_NOTE}", "ms": 30000},
            {"eval": _GRANT_PICKS},
            {"eval": LS},
        ])
    v = out["values"]
    asked = [r["signer_fp"] for _g, a, r in rec.steps if a == "revoke-challenge"]
    assert asked == [newer.key.fingerprint, older.key.fingerprint], asked      # the second tap moved to the next key
    assert v[-3].startswith("revoked"), v[-3]
    assert v[-2] == 0, v                                                       # and needed no pick
    assert json.loads(v[-1]) == {older.key.fingerprint: _b64u(b"cred-old")}, v[-1]    # the refused record is forgotten


@needs_browser
@needs_ssh
def test_b7_a_bound_consent_answer_whose_newest_remembered_key_is_gone_asks_the_next_remembered_key(
        tmp_path: Path, monkeypatch) -> None:
    """The same deadlock on the sheet (sign.js whom/pkGet): the consent answer is verified for real."""
    rig, second = _consent_rig_with_two_passkeys(tmp_path, monkeypatch)
    older, newer = rig.pk, second
    held = _b64u(b"levain-consent-cred")                       # the one credential rig.authenticator() holds: the older key
    record = {older.key.fingerprint: held, newer.key.fingerprint: _b64u(b"cred-new")}
    allow = "document.querySelector('#sheet-actions [data-choice=\"allow\"]')"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(),
            {"eval": f"window.localStorage.setItem('levain-passkeys:localhost', {json.dumps(json.dumps(record))}); 1"},
            *rig.open_top(),
            {"tap": allow},
            {"until": f"/cancelled/.test({NOTE_C}) && {allow} && !{allow}.disabled && {NOTE_C}", "ms": 30000},
            {"tap": allow},
            {"until": f"/^signed|^Not signed|^Not sure/.test({NOTE_C}) && {NOTE_C}", "ms": 30000},
            {"eval": "document.querySelectorAll('#sheet-actions .sheet-key').length"},
            {"eval": LS},
        ])
    v = out["values"]
    bodies = [json.loads(p.split(" ", 1)[1]) for p in out["posts"] if p.split(" ", 1)[0].endswith("/challenge")]
    assert [b["signer_fp"] for b in bodies] == [newer.key.fingerprint, older.key.fingerprint], bodies
    assert v[-3].startswith("signed"), v[-3]
    assert v[-2] == 0, v
    assert json.loads(v[-1]) == {older.key.fingerprint: held}, v[-1]
    assert rig.state() != "pending"


@needs_browser
@needs_ssh
def test_b8_a_plain_sign_after_a_refused_remembered_key_forgets_it(tmp_path: Path, monkeypatch) -> None:
    """Late mode (gemini MED): the only remembered key is not on this device; the first tap asks for it and is refused, the
    next asks the OS's list and the key this device holds signs. The stale record goes, so the next plain sign does not
    fail on its first tap again."""
    rig = _Rig(tmp_path, monkeypatch, two_keys=True)
    record = {rig.other.key.fingerprint: _b64u(b"cred-gone")}
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, _auth(rig.pk, b"cred-a"),
            {"eval": f"window.localStorage.setItem('levain-passkeys:localhost', {json.dumps(json.dumps(record))}); 1"},
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"tap": f"{OPTION_B}"},
            {"until": f"/nothing was signed/.test({FACET_NOTE}) && !{OPTION_B}.disabled && {FACET_NOTE}", "ms": 30000},
            {"tap": f"{OPTION_B}"},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": LS},
        ])
    assert _signed_by(rig) == rig.pk.key.fingerprint
    assert json.loads(out["values"][-1]) == {rig.pk.key.fingerprint: _b64u(b"cred-a")}, out["values"][-1]


@needs_browser
@needs_ssh
def test_b9_an_undecodable_remembered_id_is_forgotten_by_a_plain_sign_too(tmp_path: Path, monkeypatch) -> None:
    """complement LOW 4, gemini LOW 3: a record that cannot be an id is forgotten before it decides who signs, in late mode
    as in bound, on the card and on the sheet; kept, it would shadow the key this device holds."""
    rig = _Rig(tmp_path, monkeypatch, two_keys=True)
    record = json.dumps(json.dumps({rig.other.key.fingerprint: "a"}))      # one character: no base64url decodes to it
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, _auth(rig.pk, b"cred-a"),
            {"eval": f"window.localStorage.setItem('levain-passkeys:localhost', {record}); 1"},
            *_open(rig),
            {"tap": f"{_button('b')}"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": LS},
        ])
    assert json.loads(out["values"][-1]) == {rig.pk.key.fingerprint: _b64u(b"cred-a")}, out["values"][-1]


@needs_browser
@needs_ssh
def test_b9b_the_card_forgets_an_undecodable_remembered_id_on_a_plain_sign(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch, two_keys=True)
    record = json.dumps(json.dumps({rig.other.key.fingerprint: "a"}))
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, _auth(rig.pk, b"cred-a"),
            {"eval": f"window.localStorage.setItem('levain-passkeys:localhost', {record}); 1"},
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"tap": f"{OPTION_B}"},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": LS},
        ])
    assert json.loads(out["values"][-1]) == {rig.pk.key.fingerprint: _b64u(b"cred-a")}, out["values"][-1]


@needs_browser
@needs_ssh
def test_b10_a_picked_key_is_asked_for_by_its_id_even_after_a_refusal(world, tmp_path, monkeypatch) -> None:
    """The one remembered key is refused, so the next tap draws the pick (two keys enrolled). Picking that same key asks the
    authenticator for it by its id again: the challenge is bound to it, and the OS's whole list would sign with the other
    key, which the revoke refuses. So it is refused again by the authenticator, and nothing is posted to the revoke."""
    port, src, signer, older, newer = _grant_rig(world, tmp_path, monkeypatch)
    record = {newer.key.fingerprint: _b64u(b"cred-new")}
    pick_newer = (f"[...{_CARD}.querySelectorAll('.sign-pick')].find(b => b.title === {json.dumps(newer.key.fingerprint)})"
                  ".click() || 1")
    with _serve_on(port, src, surface_tokens={"browser": "T"}, install_class="reported", signer=signer) as httpd:
        rec = _Verifying(httpd.grant_phone, [older, newer])
        httpd.grant_phone = rec
        out = _drive(tmp_path, f"http://localhost:{port}/#write_token=T", [
            _auth(older, b"cred-old"), {"until": TILE},
            {"eval": f"window.localStorage.setItem('levain-passkeys:localhost', {json.dumps(json.dumps(record))}); 1"},
            {"eval": OPEN_DECISIONS},
            {"until": f"!!{_CARD} && !!{_REVOKE} && 1"},
            {"tap": _REVOKE},
            {"until": f"/nothing was revoked/.test({_GRANT_NOTE}) && !{_REVOKE}.disabled && {_GRANT_NOTE}", "ms": 30000},
            {"eval": f"{_CARD}.querySelector('.sign-note').textContent = ''; 1"},
            {"tap": _REVOKE},
            {"until": f"{_GRANT_PICKS} === 3 && 1", "ms": 30000},
            {"tap": pick_newer.replace(".click() || 1", "")},
            {"until": f"/nothing was revoked|^revoked|^not revoked/.test({_GRANT_NOTE}) && {_GRANT_NOTE}", "ms": 30000},
        ])
    assert "the passkey did not sign (NotAllowedError); nothing was revoked" in out["values"][-1], out["values"][-1]
    asked = [r["signer_fp"] for _g, a, r in rec.steps if a == "revoke-challenge"]
    assert asked == [newer.key.fingerprint, newer.key.fingerprint], asked
    assert not [s for s in rec.steps if s[1] == "revoke"], rec.steps        # the OS's list never signed with the other key


@needs_browser
@needs_ssh
def test_b8s_the_sheets_plain_sign_after_a_refused_remembered_key_forgets_it(tmp_path: Path, monkeypatch) -> None:
    """b8 on the sheet (sign.js whom/pkGet/pkSettle): the refusal of the one remembered id is kept under the key it was
    remembered for, so the next tap passes over that key, and the key that signs replaces the stale record."""
    rig = _Rig(tmp_path, monkeypatch, two_keys=True)
    record = json.dumps(json.dumps({rig.other.key.fingerprint: _b64u(b"cred-gone")}))
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, _auth(rig.pk, b"cred-a"),
            {"eval": f"window.localStorage.setItem('levain-passkeys:localhost', {record}); 1"},
            *_open(rig),
            {"tap": f"{_button('b')}"},
            {"until": f"/cancelled/.test({NOTE}) && {_button('b')} && !{_button('b')}.disabled && {NOTE}", "ms": 30000},
            {"tap": f"{_button('b')}"},
            {"until": f"/^signed|^Not signed|^Not sure/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": LS},
        ])
    assert out["values"][-2].startswith("signed"), out["values"][-2]
    assert _signed_by(rig) == rig.pk.key.fingerprint
    assert json.loads(out["values"][-1]) == {rig.pk.key.fingerprint: _b64u(b"cred-a")}, out["values"][-1]
