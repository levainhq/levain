"""ONESURFACE OS-1 through the sheet itself: headless Chrome (its own profile, tests/_page_drive_run.py through ``_drive``)
opens ``/sign/<id>`` on a real cockpit server (make_server + a Signer on rpId localhost pinned to the page), and Chrome's
virtual authenticator holds the passkey the Signer's keys name, so the sheet's challenge, ``navigator.credentials.get``
and answer write a ``.sig`` that ``ssh-keygen -Y verify`` accepts. The fragment's ``v=`` / ``choice=`` follow phone.js's
rules, and with no key that may sign the request the sheet starts no ceremony."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import DESKTOP, QRID, STRIP_FACET, TOK, _Rig, needs_ssh

STATE = "document.getElementById('sheet').dataset.state"
NOTE = "document.getElementById('sheet-note').textContent"
# counts every navigator.credentials.get the page makes from now on
SPY = ("(() => { window.__gets = 0; const real = navigator.credentials.get.bind(navigator.credentials); "
       "navigator.credentials.get = (o) => { window.__gets++; return real(o); }; return 1; })()")


def _open(rig: _Rig, frag: str = "", *, token: bool = True) -> list[dict]:
    """The steps that put the cockpit's token in this browser's storage (as the cockpit's own link does) and then open
    the sheet top-level: a full navigation from another page of the origin, so the sheet's script reads the fragment."""
    keep = f"window.localStorage.setItem('levain_write_token', {json.dumps(TOK)}); " if token else ""
    return [{"eval": f"(() => {{ {keep}location.href = '/sign/{QRID}{frag}'; return 1; }})()"},
            {"until": f"location.pathname === '/sign/{QRID}' && {STATE} && {STATE} !== 'reading' && {STATE}", "ms": 30000}]


def _sha16(rig: _Rig) -> str:
    return rig.signer.row(QRID, [rig.pk.key.fingerprint])["sha256"][:16]


def _button(choice: str) -> str:
    return f"document.querySelector('#sheet-actions [data-choice=\"{choice}\"]')"


@needs_browser
@needs_ssh
def test_the_sheet_signs_through_the_cockpits_own_ceremony(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig),
            {"eval": "document.getElementById('sheet-title').textContent"},
            {"eval": "document.getElementById('sheet-signers').textContent"},
            {"eval": "document.getElementById('sheet-home').getAttribute('href')"},
            {"eval": f"{_button('b')}.click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": STATE},
        ])
    v = out["values"]
    assert v[1] == "open" and v[2] == "t", v                      # the rig's request title
    assert v[3].startswith("May sign: " + rig.pk.key.fingerprint[:18]), v
    assert v[4] == "/"                                           # the way back is the cockpit, never the link's
    assert str(v[6]).startswith("signed (b)") and v[7] == "signed", v
    assert rig.verifies(QRID)
    posts = [p.split(" ", 1)[0] for p in out["posts"]]
    assert posts == [f"/cockpit/sign/{QRID}/challenge", f"/cockpit/sign/{QRID}/answer"], posts


@needs_browser
@needs_ssh
def test_the_links_pick_is_one_tap_on_its_own_version_and_nothing_on_load(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig, f"#v={_sha16(rig)}&choice=b&sign=np-another-request"),
            {"eval": SPY},
            {"eval": "location.hash"},
            {"eval": "document.getElementById('sheet-picked').textContent"},
            {"eval": "document.getElementById('sheet-pick').textContent"},
            {"eval": "window.__gets"},
            {"eval": "document.getElementById('sheet-picked').click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": "window.__gets"},
        ])
    v = out["values"]
    assert v[3] == "" and v[4] == "Sign (b)", v                   # the bar is scrubbed; the pick is one tap
    assert v[5].startswith("Your pick: (b)")
    assert v[6] == 0                                             # nothing asked of the passkey on load
    assert str(v[8]).startswith("signed (b)") and v[9] == 1, v
    assert rig.verifies(QRID)
    assert out["posts"][0].startswith(f"/cockpit/sign/{QRID}/challenge ")      # the path's id, never the fragment's
    assert '"choice":"b"' in out["posts"][0]


@needs_browser
def test_a_pick_on_another_version_or_without_one_draws_only_a_line(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    cases = {"#v=0000000000000000&choice=b": "This item changed since your tap",
             "#choice=b": "The link does not say which version",
             f"#v={_sha16(rig)}&choice=zz": "The option in the link (zz) is not on this item"}
    with rig.serving():
        for frag, said in cases.items():
            out = _drive(tmp_path, f"{rig.origin}/nope", [
                DESKTOP, *_open(rig, frag),
                {"eval": "!!document.getElementById('sheet-picked')"},
                {"eval": "document.getElementById('sheet-pick-miss').textContent"},
                {"eval": "document.querySelectorAll('#sheet-actions [data-choice]').length"},
            ])
            v = out["values"]
            assert v[2] is False and v[3].startswith(said) and v[4] == 2, (frag, v)
    assert rig.nothing_signed(QRID)


@needs_browser
def test_with_no_key_that_may_sign_the_sheet_escalates_and_starts_no_ceremony(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    rig.signer._keys = lambda: {}          # the cockpit holds no active passkey for this name
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig, f"#v={_sha16(rig)}&choice=b"),
            {"eval": SPY},
            {"eval": "document.getElementById('sheet-escalate').textContent"},
            {"eval": "document.querySelectorAll('#sheet button').length"},
            {"eval": "document.getElementById('sheet-actions').hidden"},
            {"eval": "[...document.querySelectorAll('button')].forEach((b) => b.click()); 1"},
            {"eval": "new Promise((r) => setTimeout(() => r(window.__gets), 500))"},
        ])
    v = out["values"]
    assert v[1] == "escalate", v
    assert v[3].startswith("No passkey can sign this here: it needs an active passkey enrolled for localhost."), v
    assert v[4] == 0 and v[5] is True, v
    assert v[7] == 0                                             # navigator.credentials.get was never called
    assert out["posts"] == [] and rig.nothing_signed(QRID)       # no challenge was asked for either


@needs_browser
def test_a_browser_holding_no_cockpit_key_reads_nothing_and_says_so(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, *_open(rig, token=False),
            {"eval": "document.getElementById('sheet-title').textContent"},
            {"eval": "window.localStorage.setItem('levain_write_token', 'not-the-token'); location.reload(); 1"},
            {"until": f"{STATE} === 'unpaired' && /not accepted/.test(document.getElementById('sheet-title').textContent)"
                      " && document.getElementById('sheet-title').textContent", "ms": 30000},
        ])
    v = out["values"]
    assert v[1] == "unpaired" and v[2] == "This device is not paired", v
    assert v[4] == "This device's key was not accepted", v


@needs_browser
@needs_ssh
def test_a_refused_user_verification_signs_nothing_and_the_option_signs_again(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), {"user_verified": False}, *_open(rig),
            {"eval": f"{_button('a')}.click() || 1"},
            {"until": f"/did not sign/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"until": f"!{_button('a')}.disabled", "ms": 5000},
        ])
    assert "nothing was signed" in out["values"][3]
    assert rig.nothing_signed(QRID)


# --- the overlay (Q2): the same sheet, a same-origin iframe over the cockpit ---------------------------------------

FRAME = "document.getElementById('levain-sign-sheet-frame')"
IN = f"({FRAME} && {FRAME}.contentDocument)"
OPEN_SHEET = "document.querySelector('#ck-needs .needs-question .sign-facet .sign-sheet-open')"


@needs_browser
@needs_ssh
def test_the_cockpit_opens_the_sheet_over_itself_and_it_signs_there(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    version = _sha16(rig)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": f"{OPEN_SHEET}.click() || 1"},
            {"until": f"{IN} && {IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state", "ms": 30000},
            {"eval": f"{FRAME}.getAttribute('src')"},
            {"eval": f"{IN}.location.pathname + '|' + {IN}.location.hash"},
            {"eval": f"{IN}.querySelector('#sheet-actions [data-choice=\"b\"]').click() || 1"},
            {"until": f"/^signed/.test({IN}.getElementById('sheet-note').textContent) && "
                      f"{IN}.getElementById('sheet-note').textContent", "ms": 30000},
            {"eval": f"!!{IN}.getElementById('sheet-close') && !{IN}.getElementById('sheet-home')"},
            {"eval": f"{IN}.getElementById('sheet-close').click() || 1"},
            {"until": f"!{FRAME} && 1", "ms": 5000},
        ])
    v = out["values"]
    assert v[2] == "open", v
    assert v[3] == f"/sign/{QRID}#v={version}", v           # the card's listed version rides the fragment
    assert v[4] == f"/sign/{QRID}|", v                           # and the sheet took it out of its bar
    assert str(v[6]).startswith("signed (b)"), v
    assert v[7] is True                                         # framed: a Close in place of the link
    assert rig.verifies(QRID)
    posts = [p.split(" ", 1)[0] for p in out["posts"]]
    assert posts[-2:] == [f"/cockpit/sign/{QRID}/challenge", f"/cockpit/sign/{QRID}/answer"], posts


@needs_browser
def test_the_overlay_closes_on_its_own_button_and_on_escape(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP,
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": f"{OPEN_SHEET}.click() || 1"},
            {"until": f"{IN} && {IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state", "ms": 30000},
            {"eval": "document.querySelector('#levain-sign-sheet .modal-close').click() || 1"},
            {"eval": f"!{FRAME}"},
            {"eval": "window.LevainSignSheet.open('np-card-question-01') && 1"},
            {"eval": "document.getElementById('levain-sign-sheet').dispatchEvent(new KeyboardEvent('keydown', "
                     "{key: 'Escape', bubbles: true})) && 1"},
            {"eval": f"!{FRAME}"},
            {"eval": "window.LevainSignSheet.open('../../evil') === null && !document.getElementById('levain-sign-sheet')"},
            # a message from anything but the sheet's own frame closes nothing
            {"eval": "window.LevainSignSheet.open('np-card-question-01') && 1"},
            {"eval": "window.postMessage({levainSheet: 'close'}, location.origin); "
                     "new Promise((r) => setTimeout(() => r(!!document.getElementById('levain-sign-sheet')), 300))"},
        ])
    v = out["values"]
    assert v[4] is True and v[7] is True and v[8] is True and v[10] is True, v
    assert rig.nothing_signed(QRID)
