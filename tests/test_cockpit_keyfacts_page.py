"""ONESURFACE OS-K through the cockpit page itself (headless Chrome, a virtual authenticator): a card signed in the
cockpit records the credential that signed, and a browser with no record of its own asks for the credential the
cockpit holds."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from levain.cockpit import keyfacts
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import DESKTOP, FACET_NOTE, OPTION_B, QRID, STRIP_FACET, TOK, _Rig, needs_ssh

CRED = base64.urlsafe_b64encode(b"levain-card-cred").decode().rstrip("=")   # _Rig.authenticator's credentialId

# records the credential ids every passkey get() of this page names in allowCredentials
SPY = ("(() => { window.__allow = []; const real = navigator.credentials.get.bind(navigator.credentials); "
       "navigator.credentials.get = (o) => { const a = (o && o.publicKey && o.publicKey.allowCredentials) || []; "
       "window.__allow.push(a.map((c) => btoa(String.fromCharCode(...new Uint8Array(c.id))).replace(/\\+/g, '-')"
       ".replace(/\\//g, '_').replace(/=+$/, ''))); return real(o); }; return 1; })()")


def _rig(tmp_path: Path, monkeypatch) -> tuple[_Rig, Path]:
    rig = _Rig(tmp_path, monkeypatch)
    facts = tmp_path / "facts"
    facts.mkdir()
    rig.signer._facts_root = lambda: facts
    return rig, facts


@needs_browser
@needs_ssh
def test_a_card_signed_in_the_cockpit_records_the_credential_that_signed(tmp_path, monkeypatch) -> None:
    rig, facts = _rig(tmp_path, monkeypatch)
    with rig.serving() as (_url, _httpd):
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": SPY},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}"},
            {"eval": "JSON.stringify(window.__allow)"},
        ])
    v = out["values"]
    assert str(v[3]).startswith("signed (b)"), v
    assert json.loads(v[4]) == [[]]                  # a fresh browser and an empty store: the OS's own list
    assert rig.verifies(QRID)
    held = keyfacts.read(facts)[rig.pk.key.fingerprint]
    assert held.credential_id == CRED and held.source == "signature"
    answers = [p for p in out["posts"] if p.startswith(f"/cockpit/sign/{QRID}/answer")]
    assert answers and f'"raw_id":"{CRED}"' in answers[0], out["posts"]


@needs_browser
@needs_ssh
def test_a_fresh_browser_asks_for_the_credential_the_cockpit_holds(tmp_path, monkeypatch) -> None:
    rig, facts = _rig(tmp_path, monkeypatch)
    assert keyfacts.learn(facts, rig.pk.key.fingerprint, b"levain-card-cred", now="t")
    with rig.serving() as (_url, _httpd):
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": "Object.keys(window.localStorage).filter((k) => k.startsWith('levain-passkeys:')).length"},
            {"eval": SPY},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}"},
            {"eval": "JSON.stringify(window.__allow)"},
        ])
    v = out["values"]
    assert v[1] == 0                                 # this browser holds no record of which passkey it used
    assert str(v[4]).startswith("signed (b)"), v
    assert json.loads(v[5]) == [[CRED]]              # allowCredentials came from the cockpit alone
    assert rig.verifies(QRID)
