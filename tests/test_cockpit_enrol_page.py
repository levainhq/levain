"""The cockpit's own passkey-making page (``/enrol``, enrol.js), through headless Chrome and a virtual authenticator.

Why it exists: a passkey belongs to one https name, the cockpit moved to a new name, and the only page that ever made a
passkey (the old phone port's) is not served on the cockpit's origin. ``POST /cockpit/passkey/key-line`` was ported to the
cockpit (OS-2) with no page that calls it, so a device on the new name could not make a passkey at all. These tests run the
failure the operator hit: a cockpit holding no passkey for its name, a device that must make one there."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from levain.cockpit import keyfacts
from levain.consent.passkey import b64url_decode, public_key_line
from levain.web_server import _KEY_LINE_ROUTE, load_web_asset
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import DESKTOP, QRID, STRIP_FACET, TOK, _Rig, needs_ssh
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import _call, _serve

@pytest.fixture
def src(tmp_path: Path):
    return _install(tmp_path)[1]


TITLE = "document.getElementById('enrol-title').textContent"
CMDS = "[...document.querySelectorAll('#enrol-cmds .enrol-cmd code')].map((c) => c.textContent)"
MAP = "window.localStorage.getItem('levain-passkeys:localhost')"
# counts every navigator.credentials.create the page makes from now on
SPY = ("(() => { window.__creates = 0; const real = navigator.credentials.create.bind(navigator.credentials); "
       "navigator.credentials.create = (o) => { window.__creates++; return real(o); }; return 1; })()")


def _open(*, token: bool = True) -> list[dict]:
    """The steps that put the cockpit's token in this browser's storage (as the cockpit's own link does) and then open
    /enrol top-level."""
    keep = f"window.localStorage.setItem('levain_write_token', {json.dumps(TOK)}); " if token else ""
    return [{"eval": f"(() => {{ {keep}location.href = '/enrol'; return 1; }})()"},
            {"until": f"location.pathname === '/enrol' && document.querySelector('#enrol-actions button, #enrol-title') && "
                      f"document.getElementById('enrol-msg').textContent !== 'Reading this device…' && 1", "ms": 30000}]


def _rig_with_no_passkey(tmp_path: Path, monkeypatch) -> _Rig:
    rig = _Rig(tmp_path, monkeypatch)
    rig.signer._keys = lambda: {}                  # the cockpit holds no active passkey for this name
    (rig.qdir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(tmp_path / "ck")}))
    return rig


def test_the_page_and_its_script_are_served_and_name_the_gated_route(src) -> None:
    html, js = load_web_asset("enrol.html"), load_web_asset("enrol.js")
    assert '<script src="/enrol.js"></script>' in html and not re.search(r"<script(?![^>]*\bsrc=)", html)
    with _serve(src) as (port, _h):
        for path, kind in (("/enrol", "text/html"), ("/enrol.js", "text/javascript")):
            status, _body, hdrs = _call(port, "GET", path)
            assert status == 200 and hdrs["content-type"].startswith(kind), (path, status)
    # the three names the page shares with the cockpit's other pages are the same strings, so a token or a remembered
    # credential written by one is read by the others
    boot = re.search(r'WRITE_TOKEN_KEY = "([^"]+)"', load_web_asset("dashboard_boot.js")).group(1)
    assert re.search(r'WRITE_TOKEN_KEY = "%s"' % re.escape(boot), js)
    prefix = re.search(r'signMapKey = \(rp\) => "([^"]+)"', load_web_asset("dashboard_core.js")).group(1)
    assert re.search(r'MAP_KEY = \(rp\) => "%s"' % re.escape(prefix), js)
    assert re.search(r'mapKey = \(rp\) => "%s"' % re.escape(prefix), load_web_asset("sign.js"))
    assert f'KEY_LINE = "{_KEY_LINE_ROUTE}"' in js


@needs_browser
def test_a_device_with_no_passkey_for_the_name_makes_one_and_gets_commands_the_cockpit_would_accept(tmp_path: Path,
                                                                                                   monkeypatch) -> None:
    rig = _rig_with_no_passkey(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(), {"eval": SPY},
            {"eval": TITLE},
            {"tap": "document.querySelector('#enrol-actions button')"},
            {"until": "document.querySelectorAll('#enrol-cmds .enrol-cmd').length > 0 && 1", "ms": 30000},
            {"eval": TITLE},
            {"eval": "JSON.stringify(" + CMDS + ")"},
            {"eval": MAP},
            {"eval": "window.__creates"},
            {"eval": "window.sessionStorage.getItem('levain-enrol-made')"},
        ])
    v = out["values"]
    assert v[3] == "Make a passkey on this device", v
    assert v[6] == "Passkey made" and v[9] == 1, v
    posts = [p for p in out["posts"] if p.startswith(_KEY_LINE_ROUTE + " ")]
    assert len(posts) == 1, out["posts"]
    body = json.loads(posts[0].split(" ", 1)[1])
    assert set(body) == {"spki", "authenticator_data"}, body
    # the made credential is bound to the cockpit's name: the cockpit's own check of the creation passes
    line = public_key_line(b64url_decode(body["spki"], "spki"), "localhost")
    held = keyfacts.attested(b64url_decode(body["authenticator_data"], "authenticator_data"), line)
    cmds = json.loads(v[7])
    assert cmds[0].startswith("printf '%s\\n' '" + line + "'"), cmds
    assert any("levain cockpit enroll" in c and "--custody passkey" in c and "--attested=" in c for c in cmds), cmds
    # the sign sheet asks for this credential by id, so the page leaves the record it reads
    kept = json.loads(v[8])
    assert list(kept.values()) == [base64.urlsafe_b64encode(held.credential_id).decode().rstrip("=")], (kept, held)
    assert v[10] is None                                      # the kept credential is dropped once its key line is sent


@needs_browser
def test_a_credential_for_another_name_than_the_cockpits_is_refused_and_nothing_is_kept(tmp_path: Path,
                                                                                         monkeypatch) -> None:
    rig = _rig_with_no_passkey(tmp_path, monkeypatch)
    with rig.serving():
        # moved after the server's own pre-bind check: the key line reads the Signer's name at call time
        rig.signer.rp_id = "other.test"            # the cockpit's name is not the one this page makes a passkey for
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(),
            {"tap": "document.querySelector('#enrol-actions button')"},
            {"until": "/^Not accepted/.test(document.getElementById('enrol-title').textContent) && 1", "ms": 30000},
            {"eval": "document.getElementById('enrol-msg').textContent"},
            {"eval": MAP},
            {"eval": "document.querySelectorAll('#enrol-cmds .enrol-cmd').length"},
            {"eval": "window.sessionStorage.getItem('levain-enrol-made')"},
        ])
    v = out["values"]
    assert "another name" in v[4] and "Nothing was enrolled" in v[4], v
    assert v[5] is None and v[6] == 0 and v[7] is None, v


@needs_browser
def test_an_unpaired_browser_makes_no_passkey_and_sends_nothing(tmp_path: Path, monkeypatch) -> None:
    rig = _rig_with_no_passkey(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(token=False),
            {"eval": TITLE},
            {"eval": "document.getElementById('enrol-actions').hidden"},
        ])
    v = out["values"]
    assert v[2] == "This device is not paired" and v[3] is True, v
    assert out["posts"] == []


@needs_browser
@needs_ssh
def test_the_cards_with_no_passkey_to_sign_with_link_to_the_page(tmp_path: Path, monkeypatch) -> None:
    rig = _rig_with_no_passkey(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": STRIP_FACET.replace(".sign-option", ".sign-note") + " && 1", "ms": 30000},
            {"until": "(() => { const a = document.querySelector('#levain-modal .needs-question .sign-note a.sign-enrol-link');"
                      " return a ? a.getAttribute('href') : null; })()", "ms": 30000},
            {"eval": "document.querySelector('#levain-modal .needs-question .sign-note').textContent"},
        ])
        sheet = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(),
            {"eval": f"(() => {{ window.localStorage.setItem('levain_write_token', {json.dumps(TOK)}); "
                     f"location.href = '/sign/{QRID}'; return 1; }})()"},
            {"until": "document.getElementById('sheet-escalate') && 1", "ms": 30000},
            {"eval": "document.querySelector('#sheet-escalate a.sign-enrol-link').getAttribute('href')"},
        ])
    assert out["values"][1] == "/enrol", out["values"]
    assert out["values"][2].startswith("no passkey is enrolled for"), out["values"]
    assert sheet["values"][2] == "/enrol", sheet["values"]


@needs_browser
def test_a_failed_send_keeps_the_credential_and_the_retry_makes_no_second_passkey(tmp_path: Path, monkeypatch) -> None:
    rig = _rig_with_no_passkey(tmp_path, monkeypatch)
    fail_first = ("(() => { const real = window.fetch.bind(window); let n = 0; window.fetch = (u, o) => "
                  "(String(u).includes('key-line') && n++ === 0 ? Promise.resolve(new Response('{}', {status: 503})) : real(u, o)); "
                  "return 1; })()")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(), {"eval": SPY}, {"eval": fail_first},
            {"tap": "document.querySelector('#enrol-actions button')"},
            {"until": "/^Passkey made, not sent/.test(document.getElementById('enrol-title').textContent) && 1", "ms": 30000},
            {"eval": "[...document.querySelectorAll('#enrol-actions button')].map((b) => b.textContent).join('|')"},
            {"tap": "document.querySelector('#enrol-actions button')"},
            {"until": "document.querySelectorAll('#enrol-cmds .enrol-cmd').length > 0 && 1", "ms": 30000},
            {"eval": "window.__creates"},
        ])
    v = out["values"]
    assert v[6] == "Send the key again|Discard it and make another", v
    assert v[9] == 1, v                                        # one creation across the failed send and its retry


@needs_browser
@needs_ssh
def test_the_link_on_a_sheet_the_cockpit_overlay_frames_leaves_the_frame(tmp_path: Path, monkeypatch) -> None:
    # /enrol refuses to be framed, so a link that navigated the overlay's frame would draw a blank "refused to connect"
    rig = _rig_with_no_passkey(tmp_path, monkeypatch)
    frame = (f"(() => {{ const f = document.createElement('iframe'); f.id = 'enrol-probe'; f.src = '/sign/{QRID}'; "
             "document.body.appendChild(f); return 1; })()")
    link = "(document.getElementById('enrol-probe').contentDocument || {querySelector: () => null}).querySelector('#sheet-escalate a.sign-enrol-link')"
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), {"eval": frame},
            {"until": f"{link} && {link}.getAttribute('target')", "ms": 30000},
        ])
    assert out["values"][-1] == "_top", out["values"]
