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
            {"eval": f"new Promise((r) => setTimeout(() => r(!{FRAME}), 1500))"},       # the close is asked of the sheet first
            {"eval": "window.LevainSignSheet.open('np-card-question-01') && 1"},
            {"eval": "document.getElementById('levain-sign-sheet').dispatchEvent(new KeyboardEvent('keydown', "
                     "{key: 'Escape', bubbles: true})) && 1"},
            {"eval": f"new Promise((r) => setTimeout(() => r(!{FRAME}), 1500))"},       # the close is asked of the sheet first
            {"eval": "window.LevainSignSheet.open('../../evil') === null && !document.getElementById('levain-sign-sheet')"},
            # a message from anything but the sheet's own frame closes nothing
            {"eval": "window.LevainSignSheet.open('np-card-question-01') && 1"},
            {"eval": "window.postMessage({levainSheet: 'close'}, location.origin); "
                     "new Promise((r) => setTimeout(() => r(!!document.getElementById('levain-sign-sheet')), 300))"},
        ])
    v = out["values"]
    assert v[4] is True and v[7] is True and v[8] is True and v[10] is True, v
    assert rig.nothing_signed(QRID)


# --- L3 r1 (H1-H8): what the sheet does when the text, the network or the passkey record misbehave -------------------

WRITE_IN_BOX = "document.getElementById('sheet-write-in')"
WRITE_IN_BTN = "[...document.querySelectorAll('#sheet-actions button')].find((b) => /Sign my words/.test(b.textContent))"
SIGNING = "(document.getElementById('sheet-signing') || {}).textContent"
ENABLED = f"!{_button('a')}.disabled && !{_button('b')}.disabled"


def _stub(win: str = "window") -> dict:
    """A fetch wrapper in ``win`` (the sheet's window): a path ending in a key of ``<win>.__stub`` is answered by that
    function ``(real, url, opts)``; every other call goes to the real fetch. ``<win>.__reads`` counts the request
    reads (GET ...json) made from now on."""
    return {"eval": f"(() => {{ const w = {win}; const real = w.fetch.bind(w); w.__reads = 0; w.__stub = {{}}; "
                    "w.fetch = (u, o) => { const url = String(u); if (/\\.json$/.test(url)) w.__reads++; "
                    "for (const k of Object.keys(w.__stub)) if (url.endsWith(k)) return w.__stub[k](real, u, o || {}); "
                    "return real(u, o); }; return 1; })()"}


# a reply that never comes: settles only by the AbortSignal the caller passed (none: never)
HANG = ("(real, u, o) => new Promise((res, rej) => { if (o.signal) o.signal.addEventListener('abort', "
        "() => rej(new DOMException('aborted', 'AbortError'))); })")
# headers that arrive and a body that never does (same rule)
BODY_HANG = ("(real, u, o) => Promise.resolve({ ok: true, status: 200, json: () => new Promise((res, rej) => { "
             "if (o.signal) o.signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError'))); }) })")
# the answer reaches the cockpit, which records the signature, and its reply is lost on the way back
LOST_AFTER_SEND = "(real, u, o) => real(u, o).then(() => { throw new TypeError('the reply was lost'); })"
# the answer never left the page
LOST_BEFORE_SEND = "(real, u, o) => Promise.reject(new TypeError('the network went away'))"


def _signed_message(rig: _Rig) -> str:
    return json.loads((rig.qdir / "sign" / f"{QRID}.signed.json").read_text())["signed_message"]


@needs_browser
@needs_ssh
def test_h1_a_write_in_is_frozen_and_normalised_at_the_click_and_the_text_signed_is_shown(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig),
            {"eval": f"{WRITE_IN_BOX}.value = '  approve $10  '; 1"},
            # the click and what the page shows in the same turn, before any reply could come back
            {"eval": f"(() => {{ {WRITE_IN_BTN}.click(); return [{WRITE_IN_BOX}.disabled, {WRITE_IN_BOX}.value, "
                     f"{SIGNING} || '']; }})()"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": SIGNING},
            {"eval": "document.getElementById('sheet-signing').dataset.source"},
        ])
    v = out["values"]
    disabled, value, shown = v[3]
    assert disabled is True and value == "approve $10", v           # frozen at the click, the text normalised in place
    assert shown.endswith("approve $10"), v                          # what is being signed, shown as it is signed
    assert v[5].endswith("(*): approve $10") and v[6] == "server", v   # after the signature: the server's own line
    assert _signed_message(rig).endswith("(*): approve $10")
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_h2_unsure_after_the_answer_landed_re_reads_and_shows_it_closed_never_re_arms(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": "(() => { window.__reloads = 0; const t = window.LevainCockpitTransport; const r = t.reload.bind(t); "
                     "t.reload = () => { window.__reloads++; return r(); }; return 1; })()"},
            {"eval": f"{OPEN_SHEET}.click() || 1"},
            {"until": f"{IN} && {IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state === 'open'", "ms": 30000},
            _stub(f"{FRAME}.contentWindow"),
            {"eval": f"{FRAME}.contentWindow.__stub['/answer'] = {LOST_AFTER_SEND}; 1"},
            {"eval": f"{IN}.getElementById('sheet-write-in').value = 'approve $10'; "
                     f"[...{IN}.querySelectorAll('#sheet-actions button')].find((b) => /Sign my words/.test(b.textContent)).click() || 1"},
            {"until": f"{IN}.getElementById('sheet').dataset.state === 'closed' && 1", "ms": 30000, "may_timeout": True},
            {"eval": f"{FRAME}.contentWindow.__reads"},
            {"eval": f"[...{IN}.querySelectorAll('#sheet-actions button, #sheet-actions input')].filter((b) => !b.disabled).length"},
            {"eval": f"{IN}.getElementById('sheet-note').textContent"},
            {"eval": "new Promise((r) => setTimeout(() => r(window.__reloads), 2000))"},
        ])
    v = out["values"]
    assert not isinstance(v[7], dict), v                            # the sheet reached "closed"
    assert v[8] >= 1, v                                              # by reading the request again
    assert v[9] == 0, v                                              # nothing re-armed
    assert "no longer open" in v[10], v
    assert v[11] >= 1, v                                             # the cockpit under it was told to read again
    assert rig.verifies(QRID)                                        # the answer had landed


@needs_browser
def test_h2_unsure_with_the_identical_version_still_open_unlocks_after_the_re_read(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(),
            {"eval": f"window.__stub['/answer'] = {LOST_BEFORE_SEND}; {_button('a')}.click() || 1"},
            {"until": f"window.__reads >= 1 && {ENABLED} && {WRITE_IN_BOX} && !{WRITE_IN_BOX}.disabled && 1", "ms": 30000},
            {"eval": STATE},
            {"eval": NOTE},
        ])
    v = out["values"]
    assert v[5] == "open", v
    assert "still open" in v[6], v
    assert rig.nothing_signed(QRID)


@needs_browser
def test_h3_a_challenge_that_never_answers_ends_unsigned_at_the_deadline(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(), {"eval": SPY},
            {"eval": f"window.__stub['/challenge'] = {HANG}; {_button('a')}.click() || 1"},
            {"until": f"/did not answer in time/.test({NOTE}) && {ENABLED} && {NOTE}", "ms": 25000, "may_timeout": True},
            {"eval": "window.__gets"},
            {"eval": "window.__reads"},
        ])
    v = out["values"]
    assert isinstance(v[5], str) and "nothing was signed" in v[5], v   # unsigned: before the answer, so the options re-arm
    assert v[6] == 0 and v[7] == 0, v                                  # no passkey asked, no re-read needed
    assert rig.nothing_signed(QRID)


@needs_browser
def test_h3_an_answer_whose_body_never_arrives_is_unsure_and_re_reads(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(),
            {"eval": f"window.__stub['/answer'] = {BODY_HANG}; {_button('a')}.click() || 1"},
            {"until": f"window.__reads >= 1 && {ENABLED} && {NOTE}", "ms": 25000, "may_timeout": True},
            {"eval": NOTE},
        ])
    v = out["values"]
    assert isinstance(v[4], str), v                                   # the deadline fired and the re-read ran
    assert "still open" in v[5], v                                    # the answer never reached the cockpit here
    assert rig.nothing_signed(QRID)


@needs_browser
def test_h4_a_malformed_challenge_is_named_and_no_passkey_is_asked(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    bad = ("(real, u, o) => Promise.resolve(new Response(JSON.stringify({challenge: window.__bad, rp_id: 'localhost'}), "
           "{status: 200, headers: {'Content-Type': 'application/json'}}))")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(), {"eval": SPY},
            {"eval": f"window.__bad = 'not base64url!'; window.__stub['/challenge'] = {bad}; {_button('a')}.click() || 1"},
            {"until": f"{ENABLED} && /challenge/.test({NOTE}) && {NOTE}", "ms": 10000, "may_timeout": True},
            {"eval": f"window.__bad = ''; {_button('a')}.click() || 1"},
            {"until": f"{ENABLED} && /challenge/.test({NOTE}) && {NOTE}", "ms": 10000, "may_timeout": True},
            {"eval": "window.__gets"},
        ])
    v = out["values"]
    for said in (v[5], v[7]):
        assert isinstance(said, str) and "malformed challenge" in said and "nothing was signed" in said, v
    assert v[8] == 0, v
    assert rig.nothing_signed(QRID)


@needs_browser
def test_h5_escape_inside_the_frame_closes_the_overlay_and_the_cockpit_behind_is_inert(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    behind = ("[...document.body.children].filter((n) => n.id !== 'levain-sign-sheet' && n.tagName !== 'SCRIPT')")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP,
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": f"{OPEN_SHEET}.click() || 1"},
            {"until": f"{IN} && {IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state === 'open'", "ms": 30000},
            {"eval": f"{behind}.length > 0 && {behind}.every((n) => n.inert)"},
            {"eval": "document.getElementById('levain-sign-sheet').inert === false"},
            # focus inside the sheet, then Escape there: the key event never reaches the cockpit's document
            {"eval": f"(() => {{ const b = {IN}.querySelector('#sheet-actions button'); b.focus(); "
                     f"b.dispatchEvent(new ({FRAME}.contentWindow.KeyboardEvent)('keydown', {{key: 'Escape', bubbles: true}})); "
                     f"return {IN}.activeElement === b; }})()"},
            {"until": f"!{FRAME} && 1", "ms": 3000, "may_timeout": True},
            {"eval": f"{behind}.some((n) => n.inert)"},
        ])
    v = out["values"]
    assert v[3] is True and v[4] is True, v                          # open: everything behind it inert, the sheet not
    assert v[5] is True, v
    assert v[6] == 1, v                                              # Escape in the frame closed the overlay
    assert v[7] is False, v                                          # and the cockpit is live again


# every navigator.credentials.get from now on, by the credential id it asked for (null: none, the OS's own list)
ASKED = ("(() => { window.__asked = []; const real = navigator.credentials.get.bind(navigator.credentials); "
         "const b64u = (b) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g, '-').replace(/\\//g, '_')"
         ".replace(/=+$/, ''); navigator.credentials.get = (o) => { const a = o.publicKey.allowCredentials; "
         "window.__asked.push(a && a.length ? b64u(a[0].id) : null); return real(o); }; return 1; })()")
CARD_CRED = "bGV2YWluLWNhcmQtY3JlZA"        # b64u(b"levain-card-cred"): the credential the virtual authenticator holds


def _seed(rp: str, record: dict) -> dict:
    return {"eval": f"window.localStorage.setItem('levain-passkeys:{rp}', {json.dumps(json.dumps(record))}); 1"}


@needs_browser
def test_h6_the_sheet_reads_its_passkey_record_under_the_reads_rp_id(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    fp = rig.pk.key.fingerprint
    with rig.serving():
        # the Signer names an rpId other than this page's hostname (after the server checked its pin: the passkey
        # cannot sign there, and nothing here needs it to; the id the page asks for is the measurement)
        rig.signer.rp_id = "rp.example"
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), _seed("rp.example", {fp: "cnAtZXhhbXBsZQ"}), _seed("localhost", {fp: "aG9zdG5hbWU"}),
            *_open(rig), {"eval": ASKED},
            {"eval": f"{_button('a')}.click() || 1"},
            {"until": "window.__asked.length >= 1 && window.__asked", "ms": 10000},
        ])
    assert out["values"][-1][0] == "cnAtZXhhbXBsZQ", out["values"]
    assert rig.nothing_signed(QRID)


@needs_browser
@needs_ssh
def test_h7_a_credential_id_that_does_not_decode_is_dropped_and_the_sheet_still_signs(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), _seed("localhost", {rig.pk.key.fingerprint: "%%% not base64url"}),
            *_open(rig), {"eval": ASKED},
            {"eval": f"{_button('b')}.click() || 1"},
            {"until": f"/^signed|failed|did not/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": "window.__asked"},
        ])
    v = out["values"]
    assert str(v[-2]).startswith("signed (b)"), v
    assert v[-1] == [None], v
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_h7_a_stale_remembered_credential_retries_once_without_it_and_is_replaced(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    fp = rig.pk.key.fingerprint
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), _seed("localhost", {fp: "c3RhbGUtY3JlZA"}),     # b64u(b"stale-cred")
            *_open(rig), {"eval": ASKED},
            {"eval": f"{_button('b')}.click() || 1"},
            {"until": f"/^signed|nothing was signed/.test({NOTE}) && {NOTE}", "ms": 90000},
            {"eval": "window.__asked"},
            {"eval": "window.localStorage.getItem('levain-passkeys:localhost')"},
        ])
    v = out["values"]
    assert str(v[-3]).startswith("signed (b)"), v
    assert v[-2] == ["c3RhbGUtY3JlZA", None], v
    assert json.loads(v[-1]) == {fp: CARD_CRED}, v
    assert rig.verifies(QRID)


@needs_browser
def test_h8_the_signed_line_never_prints_undefined(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    bare = ("(real, u, o) => Promise.resolve(new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}}))")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(),
            {"eval": f"window.__stub['/answer'] = {bare}; {_button('b')}.click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
        ])
    said = out["values"][-1]
    assert "undefined" not in said and rig.pk.key.fingerprint in said, said


# --- L3 verify r2 (H9-H14) ---------------------------------------------------------------------------------------------

COUNT_RELOADS = ("(() => { window.__reloads = 0; const t = window.LevainCockpitTransport; const r = t.reload.bind(t); "
                 "t.reload = () => { window.__reloads++; return r(); }; return 1; })()")
OPEN_FRAMED = [{"until": STRIP_FACET + " && 1", "ms": 30000},
               {"eval": COUNT_RELOADS},
               {"eval": f"{OPEN_SHEET}.click() || 1"},
               {"until": f"{IN} && {IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state === 'open'", "ms": 30000}]
CLOSE_IN_FRAME = f"{IN}.getElementById('sheet-close').click() || 1"
WAIT_RELOAD = {"eval": "new Promise((r) => setTimeout(() => r(window.__reloads), 2500))"}
# a reply the cockpit refuses the device for
UNPAIRED_READ = "(real, u, o) => Promise.resolve({ ok: false, status: 403, json: () => Promise.resolve({}) })"


@needs_browser
def test_h9_a_close_from_the_sheet_alone_schedules_no_board_read(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, *OPEN_FRAMED,
            {"eval": CLOSE_IN_FRAME},
            {"until": f"!{FRAME} && 1", "ms": 5000},
            WAIT_RELOAD,
        ])
    assert out["values"][-1] == 0, out["values"]                     # a close alone read nothing again


@needs_browser
def test_h9_a_close_while_a_ceremony_is_in_flight_shows_it_unsure_until_the_re_read_settles_it(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), *OPEN_FRAMED,
            _stub(f"{FRAME}.contentWindow"),
            {"eval": f"{FRAME}.contentWindow.__stub['/challenge'] = {HANG}; "
                     f"{IN}.querySelector('#sheet-actions [data-choice=\"b\"]').click() || 1"},
            {"eval": CLOSE_IN_FRAME},
            {"until": f"{FRAME}.contentWindow.__reads >= 1 && /still open/.test({IN}.getElementById('sheet-note').textContent) && 1",
             "ms": 30000, "may_timeout": True},
            {"eval": f"!!{FRAME}"},                                   # the overlay stayed: a ceremony was in flight
            {"eval": f"{IN}.getElementById('sheet-note').textContent"},
            {"eval": f"{IN}.getElementById('sheet-signing').textContent"},
            WAIT_RELOAD,
            {"eval": CLOSE_IN_FRAME},                                  # a second close leaves
            {"until": f"!{FRAME} && 1", "ms": 5000},
        ])
    v = out["values"]
    assert not isinstance(v[7], dict), v                               # the re-read ran
    assert v[8] is True, v
    assert "still open" in v[9], v                                     # what the re-read found, the ceremony still pending
    assert v[10].startswith("Not confirmed:"), v
    assert v[11] >= 1, v


@needs_browser
def test_h10_and_h13_an_unsigned_line_is_relabelled_and_the_notice_has_no_double_stop(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    refused = ("(real, u, o) => Promise.resolve(new Response(JSON.stringify({message: 'The request expired.'}), "
               "{status: 403, headers: {'Content-Type': 'application/json'}}))")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(),
            {"eval": f"window.__stub['/challenge'] = {refused}; {_button('b')}.click() || 1"},
            {"until": f"/nothing was signed/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": SIGNING},
        ])
    note, shown = out["values"][-2:]
    assert ".;" not in note and ".." not in note, note               # H13
    assert note == "Not signed: The request expired; nothing was signed.", note
    assert shown.startswith("Not signed:"), shown                    # H10: never "Signing:" after the end


@needs_browser
def test_h10_after_unsure_the_signing_line_reads_not_confirmed(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(),
            {"eval": f"window.__stub['/answer'] = {LOST_BEFORE_SEND}; {_button('b')}.click() || 1"},
            {"until": f"window.__reads >= 1 && {ENABLED} && 1", "ms": 30000},
            {"eval": SIGNING},
        ])
    # the read finds the same version open: "Not signed:" now, since that read settled it
    assert out["values"][-1].startswith("Not signed:"), out["values"]


@needs_browser
def test_h10_while_the_re_read_is_pending_the_line_is_not_confirmed(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(),
            {"eval": f"window.__stub['/answer'] = {LOST_BEFORE_SEND}; window.__stub['.json'] = {HANG}; "
                     f"{_button('b')}.click() || 1"},
            {"until": f"window.__reads >= 1 && 1", "ms": 30000},
            {"eval": SIGNING},
            {"eval": NOTE},
        ])
    shown = out["values"][-2]
    assert shown.startswith("Not confirmed:"), out["values"]


@needs_browser
def test_h11_a_refused_re_read_names_the_unpaired_device_not_no_reply(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(),
            {"eval": f"window.__stub['/answer'] = {LOST_BEFORE_SEND}; window.__stub['.json'] = {UNPAIRED_READ}; "
                     f"{_button('b')}.click() || 1"},
            {"until": f"{STATE} === 'closed' && {NOTE}", "ms": 30000},
        ])
    note = out["values"][-1]
    assert "no reply" not in note and "not accepted" in note, note


@needs_browser
def test_h12_an_escape_during_composition_does_not_close_the_overlay(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, *OPEN_FRAMED,
            {"eval": f"(() => {{ const b = {IN}.querySelector('#sheet-actions button'); "
                     f"b.dispatchEvent(new ({FRAME}.contentWindow.KeyboardEvent)('keydown', {{key: 'Escape', isComposing: true, bubbles: true}})); "
                     "return 1; })()"},
            {"eval": "new Promise((r) => setTimeout(() => r(!!document.getElementById('levain-sign-sheet-frame')), 600))"},
        ])
    assert out["values"][-1] is True, out["values"]


@needs_browser
def test_h14_no_deadline_timer_is_left_pending_after_a_fetch_settles(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    track = ("(() => { const w = window; w.__live = new Map(); const st = w.setTimeout.bind(w), ct = w.clearTimeout.bind(w); "
             "w.setTimeout = (f, ms, ...a) => { const id = st(() => { w.__live.delete(id); f(...a); }, ms); w.__live.set(id, ms); return id; }; "
             "w.clearTimeout = (id) => { w.__live.delete(id); return ct(id); }; return 1; })()")
    refused = ("(real, u, o) => Promise.resolve(new Response('{}', {status: 403, headers: {'Content-Type': 'application/json'}}))")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(), {"eval": track},
            # a real challenge post, answered by the real server's refusal path is not needed: a stubbed refusal is a whole fetch
            {"eval": f"window.__stub['/challenge'] = {refused}; {_button('b')}.click() || 1"},
            {"until": f"/nothing was signed/.test({NOTE}) && 1", "ms": 30000},
            {"eval": "[...window.__live.values()].filter((ms) => ms >= 15000).length"},
        ])
    assert out["values"][-1] == 0, out["values"]


# --- L3 verify r3 (H15-H19) --------------------------------------------------------------------------------------------

FRAME_STILL_UP = "new Promise((r) => setTimeout(() => r(!!document.getElementById('levain-sign-sheet-frame')), 1500))"
# a reply that waits for the test to let it through: `window.<name>()` (on the cockpit's window, where the stub runs) sends the real request on
def _hold(name: str) -> str:
    return "(real, u, o) => new Promise((res) => { window." + name + " = () => res(real(u, o)); })"

# a request that fails when the test says so
HOLD_THEN_FAIL = "(real, u, o) => new Promise((res, rej) => { window.__fail = () => rej(new TypeError('the network went away')); })"
CHOICE_IN = lambda c: f"{IN}.querySelector('#sheet-actions [data-choice=\"{c}\"]')"  # noqa: E731
NOTE_IN = f"{IN}.getElementById('sheet-note').textContent"
SIGNING_IN = f"({IN}.getElementById('sheet-signing') || {{}}).textContent"


@needs_browser
@needs_ssh
def test_h15_a_close_after_the_re_read_settled_leaves_at_once(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), *OPEN_FRAMED,
            _stub(f"{FRAME}.contentWindow"),
            {"eval": f"{FRAME}.contentWindow.__stub['/answer'] = {LOST_AFTER_SEND}; {CHOICE_IN('b')}.click() || 1"},
            {"until": f"{IN}.getElementById('sheet').dataset.state === 'closed' && 1", "ms": 30000},
            {"eval": CLOSE_IN_FRAME},
            {"eval": FRAME_STILL_UP},
        ])
    assert out["values"][-1] is False, out["values"]                  # the ceremony had ended: the close left


@needs_browser
@needs_ssh
def test_h16_a_signed_ceremony_with_no_server_line_still_reads_signed(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    # the line is stripped from the CHALLENGE's reply: the sheet's signed line is the challenge's own `line` (the answer's
    # reply carries none it reads), so stripping it from /answer leaves the line in place and passes without the fix
    # [measured by lane-os2, 1026+1: stubbed at /answer this passed against a sign.js with H16 reverted; at /challenge it fails there]
    no_line = ("(real, u, o) => real(u, o).then((r) => r.json().then((j) => { delete j.line; return new Response("
               "JSON.stringify(j), {status: r.status, headers: {'Content-Type': 'application/json'}}); }))")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(),
            {"eval": f"window.__stub['/challenge'] = {no_line}; {_button('b')}.click() || 1"},
            {"until": f"/^signed/.test({NOTE}) && 1", "ms": 30000},
            {"eval": SIGNING},
        ])
    assert out["values"][-1].startswith("Signed:"), out["values"]    # never "Signing:" once signed


@needs_browser
@needs_ssh
def test_h17_the_close_read_back_never_overwrites_a_signed_outcome(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), *OPEN_FRAMED,
            _stub(f"{FRAME}.contentWindow"),
            {"eval": f"{FRAME}.contentWindow.__stub['/answer'] = {_hold('__answer')}; "
                     f"{FRAME}.contentWindow.__stub['.json'] = {_hold('__read')}; {CHOICE_IN('b')}.click() || 1"},
            {"until": f"typeof window.__answer === 'function' && 1", "ms": 30000},
            {"eval": CLOSE_IN_FRAME},                                  # the read-back is now held
            {"until": f"typeof window.__read === 'function' && 1", "ms": 30000},
            {"eval": f"window.__answer() || 1"},
            {"until": f"/^signed/.test({NOTE_IN}) && 1", "ms": 30000},
            {"eval": f"window.__read() || 1"},          # now the read-back comes back
            {"eval": f"new Promise((r) => setTimeout(() => r({NOTE_IN}), 1500))"},
        ])
    assert out["values"][-1].startswith("signed"), out["values"]


@needs_browser
@needs_ssh
def test_h18_the_close_read_back_names_a_changed_request(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    changed = ("(real, u, o) => real(u, o).then((r) => r.json().then((j) => { j.request.sha256 = 'f'.repeat(64); "
               "return new Response(JSON.stringify(j), {status: r.status, headers: {'Content-Type': 'application/json'}}); }))")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), *OPEN_FRAMED,
            _stub(f"{FRAME}.contentWindow"),
            {"eval": f"{FRAME}.contentWindow.__stub['/challenge'] = {HANG}; {CHOICE_IN('b')}.click() || 1"},
            {"eval": f"{FRAME}.contentWindow.__stub['.json'] = {changed}; 1"},
            {"eval": CLOSE_IN_FRAME},
            {"until": f"{FRAME}.contentWindow.__reads >= 1 && /changed|still open/.test({NOTE_IN}) && {NOTE_IN}", "ms": 30000},
        ])
    assert "changed" in out["values"][-1], out["values"]


@needs_browser
@needs_ssh
def test_h19_a_close_asked_in_one_ceremony_does_not_leave_the_next_one(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), *OPEN_FRAMED,
            _stub(f"{FRAME}.contentWindow"),
            {"eval": f"{FRAME}.contentWindow.__stub['/challenge'] = {HOLD_THEN_FAIL}; {CHOICE_IN('b')}.click() || 1"},
            {"eval": CLOSE_IN_FRAME},
            {"until": f"/still open/.test({NOTE_IN}) && 1", "ms": 30000},
            {"eval": f"window.__fail() || 1"},          # the first ceremony ends, unsigned
            {"until": f"!{CHOICE_IN('b')}.disabled && 1", "ms": 30000},
            {"eval": f"{FRAME}.contentWindow.__stub['/challenge'] = {HANG}; {CHOICE_IN('b')}.click() || 1"},
            {"until": f"/^Signing:/.test({SIGNING_IN}) && 1", "ms": 30000},
            {"eval": CLOSE_IN_FRAME},                                  # a first close of the second ceremony
            {"eval": FRAME_STILL_UP},
            {"eval": SIGNING_IN},
        ])
    v = out["values"]
    assert v[-2] is True and v[-1].startswith("Not confirmed:"), v


# --- H20: one open read after an unsure answer is not proof nothing was signed -----------------------------------------

# the answer is told to the page as lost at once, and reaches the server 900 ms later
LATE_COMMIT = ("(real, u, o) => { setTimeout(() => real(u, o), 900); "
               "return Promise.reject(new TypeError('the reply was lost')); }")
AFTER_MS = lambda ms: f"new Promise((r) => setTimeout(r, {ms}))"  # noqa: E731


@needs_browser
@needs_ssh
def test_h20_one_open_read_while_the_answer_is_still_landing_keeps_the_sheet_frozen_until_a_second_read(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(),
            {"eval": f"window.__stub['/answer'] = {LATE_COMMIT}; {_button('b')}.click() || 1"},
            {"until": f"window.__reads >= 1 && 1", "ms": 30000},
            {"eval": AFTER_MS(300)},
            {"eval": f"[{_button('a')}.disabled, {_button('b')}.disabled]"},
            {"eval": NOTE},
            {"until": f"{STATE} === 'closed' && 1", "ms": 8000, "may_timeout": True},
            {"eval": f"[{_button('a')}.disabled, {_button('b')}.disabled]"},
            {"eval": NOTE},
        ])
    v = out["values"]
    assert v[-5] == [True, True], v                       # one open read: still frozen
    assert "not confirmed" in v[-4] and "Not signed" not in v[-4], v
    assert v[-2] == [True, True], v                      # the second read found it closed: the controls stay frozen
    assert "no longer open" in v[-1], v
    assert rig.verifies(QRID)                            # the answer had landed after the first read


# --- landing integration fixes (I1-I7) ------------------------------------------------------------------------------

CARD_OPTION_A = "document.querySelector('#ck-needs .needs-question .sign-facet .sign-option')"
CARD_NOTE = "document.querySelector('#ck-needs .needs-question .sign-facet .sign-note').textContent"
PARENT_X = "document.querySelector('#levain-sign-sheet .modal-close')"


@needs_browser
def test_i1_a_card_ceremony_in_flight_locks_the_sheet_button_and_the_sheet_cannot_open(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            _stub(),
            {"eval": f"window.__stub['/challenge'] = {HANG}; {CARD_OPTION_A}.click() || 1"},
            {"until": f"{OPEN_SHEET}.disabled && 1", "ms": 10000, "may_timeout": True},
            {"eval": f"{OPEN_SHEET}.disabled"},
            {"eval": f"window.LevainSignSheet.open('{QRID}') === null && !document.getElementById('levain-sign-sheet')"},
        ])
    v = out["values"]
    assert v[-2] is True and v[-1] is True, v


@needs_browser
def test_i1_an_open_sheet_holds_the_lease_so_a_card_tap_asks_for_no_challenge(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), *OPEN_FRAMED,
            {"eval": f"{CARD_OPTION_A}.click() || 1"},
            {"eval": CARD_NOTE},
            {"eval": PARENT_X + ".click() || 1"},
            {"until": f"!{FRAME} && 1", "ms": 5000},
            {"eval": f"{CARD_OPTION_A}.click() || 1"},
            # a settle only: the virtual authenticator answers at once, so the note passes through "asking" and on to its
            # final text between polls on a slow runner (run 40685220828); the proof the tap ran is the challenge POST below
            {"until": f"/asking|confirm|challenge/.test({CARD_NOTE}) && {CARD_NOTE}", "ms": 5000, "may_timeout": True},
        ])
    v = out["values"]
    assert "already being asked for" in v[-5], v                   # while the sheet is up the tap does nothing
    challenges = [p for p in out["posts"] if p.split(" ", 1)[0].endswith("/challenge")]
    assert len(challenges) == 1, out["posts"]


@needs_browser
@needs_ssh
def test_i2_the_parents_close_while_the_answer_is_held_stays_open_then_the_second_close_leaves(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), *OPEN_FRAMED,
            _stub(f"{FRAME}.contentWindow"),
            {"eval": f"{FRAME}.contentWindow.__stub['/answer'] = {_hold('__answer')}; {CHOICE_IN('b')}.click() || 1"},
            {"until": "typeof window.__answer === 'function' && 1", "ms": 30000},
            {"eval": PARENT_X + ".click() || 1"},
            {"eval": AFTER_MS(2500)},                                    # past the parent's acknowledgement window
            {"eval": f"[!!{FRAME}, {SIGNING_IN}]"},
            {"eval": PARENT_X + ".click() || 1"},
            {"until": f"!{FRAME} && 1", "ms": 5000},
        ])
    v = out["values"]
    assert v[-3][0] is True and v[-3][1].startswith("Not confirmed:"), v         # the sheet applied its own rule: it stayed, and said so


@needs_browser
def test_i2_a_sheet_that_never_answers_is_closed_by_the_parent_and_the_board_is_read_more_than_once(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, *OPEN_FRAMED,
            # the frame stops listening: the sheet's message handler is the thing under test
            {"eval": f"{FRAME}.contentWindow.postMessage = () => {{}}; 1"},
            {"eval": "window.__reloads = 0; 1"},
            {"eval": PARENT_X + ".click() || 1"},
            {"until": f"!{FRAME} && 1", "ms": 5000},
            {"eval": "new Promise((r) => setTimeout(() => r(window.__reloads), 11000))"},
        ])
    assert out["values"][-1] >= 2, out["values"]                      # a latched read: one early reload is not all


@needs_browser
@needs_ssh
def test_i3_a_cancelled_retry_keeps_the_remembered_credential(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    fp = rig.pk.key.fingerprint
    cancel = ("(() => { navigator.credentials.get = (o) => Promise.reject(new DOMException('cancelled', 'NotAllowedError')); "
              "return 1; })()")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), _seed("localhost", {fp: CARD_CRED}),
            *_open(rig), {"eval": cancel},
            {"eval": f"{_button('b')}.click() || 1"},
            {"until": f"/nothing was signed/.test({NOTE}) && 1", "ms": 30000},
            {"eval": "window.localStorage.getItem('levain-passkeys:localhost')"},
        ])
    assert json.loads(out["values"][-1]) == {fp: CARD_CRED}, out["values"]
    assert rig.nothing_signed(QRID)


@needs_browser
def test_i4_a_node_the_board_adds_to_the_body_while_the_sheet_is_open_is_inert(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, *OPEN_FRAMED,
            {"eval": "(() => { const d = document.createElement('div'); d.id = 'redrawn'; const b = document.createElement('button'); "
                     "d.appendChild(b); document.body.appendChild(d); return new Promise((r) => setTimeout(() => r(d.inert), 100)); })()"},
            {"eval": PARENT_X + ".click() || 1"},
            {"until": f"!{FRAME} && 1", "ms": 5000},
            {"eval": "document.getElementById('redrawn').inert"},
        ])
    v = out["values"]
    assert v[-4] is True, v
    assert v[-1] is False, v                                           # given back on close


@needs_browser
def test_i5_the_signer_picker_is_not_a_ceremony_in_flight(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch, two_keys=True)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, *OPEN_FRAMED,
            {"eval": f"{CHOICE_IN('b')}.click() || 1"},
            {"until": f"{IN}.querySelector('#sheet-actions .sheet-key') && 1", "ms": 30000},
            {"eval": CLOSE_IN_FRAME},
            {"until": f"!{FRAME} && 1", "ms": 3000, "may_timeout": True},
            {"eval": f"!!{FRAME}"},
        ])
    assert out["values"][-1] is False, out["values"]                   # nothing was posted: the close leaves at once
    assert not [p for p in out["posts"] if p.split(" ", 1)[0].endswith("/challenge")], out["posts"]


# --- the lease and the inert set are given back on every close path (L3 r2 of the landing) ---------------------------

SHEET_OPENS = "(() => { const v = window.LevainSignSheet.open('%s'); const ok = !!v; window.LevainSignSheet.close(); return ok; })()" % QRID


@needs_browser
def test_j1_a_veil_removed_from_under_the_sheet_still_gives_back_the_lease_and_the_inert_set(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, *OPEN_FRAMED,
            {"eval": "document.getElementById('levain-sign-sheet').remove() || 1"},
            {"eval": "window.LevainSignSheet.close() || 1"},
            {"eval": "[...document.body.children].filter((n) => n.inert).length"},
            {"eval": SHEET_OPENS},
        ])
    v = out["values"]
    assert v[-2] == 0, v                                               # nothing the sheet made inert stays inert
    assert v[-1] is True, v                                            # the lease is back: the sheet opens again


@needs_browser
def test_j2_a_build_that_throws_leaves_no_lease_and_no_inert_node(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": "(() => { const real = document.body.appendChild.bind(document.body); let once = true; "
                     "document.body.appendChild = (n) => { if (once && n.id === 'levain-sign-sheet') { once = false; throw new Error('boom'); } return real(n); }; "
                     "try { window.LevainSignSheet.open('%s'); return 'no throw'; } catch (e) { return 'threw'; } })()" % QRID},
            {"eval": "[...document.body.children].filter((n) => n.inert).length"},
            {"eval": SHEET_OPENS},
        ])
    v = out["values"]
    assert v[-2] == 0, v
    assert v[-1] is True, v


@needs_browser
def test_j3_the_x_closes_an_overlay_whose_frame_is_gone(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, *OPEN_FRAMED,
            {"eval": FRAME + ".remove() || 1"},
            {"eval": PARENT_X + ".click() || 1"},
            {"eval": "!!document.getElementById('levain-sign-sheet')"},
            {"eval": SHEET_OPENS},
        ])
    v = out["values"]
    assert v[-2] is False, v                                           # it left at once, with no frame to ask
    assert v[-1] is True, v


@needs_browser
def test_j4_the_apps_own_modal_opened_while_the_sheet_is_up_is_not_made_inert(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, *OPEN_FRAMED,
            {"eval": "(() => { const d = document.createElement('div'); d.id = 'levain-modal'; document.body.appendChild(d); "
                     "return new Promise((r) => setTimeout(() => r(d.inert), 100)); })()"},
        ])
    assert out["values"][-1] is False, out["values"]


@needs_browser
def test_the_real_sheet_reads_the_shape_deep_link_and_the_old_ports_redirect_emit(tmp_path: Path, monkeypatch) -> None:
    """ONESURFACE OS-4 end to end: the link ``deep_link`` builds, and an old phone link sent through the real redirect
    page (``phone_redirect.js``), each land on this cockpit's sheet with the pick drawn and nothing asked on load."""
    import threading

    from levain.consent.sign import deep_link
    from levain.phone_server import make_redirect_server

    rig = _Rig(tmp_path, monkeypatch)
    sha = _sha16(rig)
    link = deep_link(rig.origin, QRID, choice="b", version=sha)
    assert link == f"{rig.origin}/sign/{QRID}#v={sha}&choice=b"
    redirect = make_redirect_server("localhost", rig.origin, port=0)
    t = threading.Thread(target=redirect.serve_forever, daemon=True)
    t.start()
    old = (f"http://localhost:{redirect.server_address[1]}/#phone_token=OLDTOKEN{'x' * 32}&sign={QRID}&v={sha}"
           "&choice=b")
    ready = f"location.pathname === '/sign/{QRID}' && {STATE} && {STATE} !== 'reading' && {STATE}"
    keep = f"window.localStorage.setItem('levain_write_token', {json.dumps(TOK)}); "
    seen = ["location.origin", "location.hash", "document.getElementById('sheet-picked').textContent",
            "document.getElementById('sheet-pick').textContent", "window.__gets"]
    try:
        with rig.serving():
            out = _drive(tmp_path, f"{rig.origin}/nope", [
                DESKTOP, rig.authenticator(),
                {"eval": f"(() => {{ {keep}location.href = {json.dumps(link)}; return 1; }})()"},
                {"until": ready, "ms": 30000}, {"eval": SPY}, *({"eval": e} for e in seen),
                {"eval": f"(() => {{ location.href = {json.dumps(old)}; return 1; }})()"},
                {"until": f"location.origin === {json.dumps(rig.origin)} && {ready}", "ms": 30000},
                {"eval": SPY}, *({"eval": e} for e in seen),
            ])
    finally:
        redirect.shutdown()
        redirect.server_close()
        t.join(timeout=5)
    v = out["values"]
    for got in (v[3:8], v[11:16]):                   # the built link, then the redirected old one
        assert got[0] == rig.origin and got[1] == "", v     # on the cockpit, the bar scrubbed
        assert got[2] == "Sign (b)" and got[3].startswith("Your pick: (b)"), v
        assert got[4] == 0, v                               # nothing asked of the passkey on load
    assert not any("phone_token" in p or "OLDTOKEN" in p for p in out["posts"]), out["posts"]
