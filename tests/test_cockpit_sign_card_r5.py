"""The cockpit card's own ceremony (dashboard_core.js signFacet / signWho / signCeremony) against the classes the OS-1
sheet's L3 r1 named (project_memory/1025-40/os1/RULINGS_L3_R1.md H2, H4, H7, H8), over the rig of test_cockpit_sign_card:
headless Chrome on ``http://localhost:<port>`` with a virtual authenticator holding the passkey the Signer's keys name.

H2: an answer whose reply did not come back ("not sure") never re-arms the card on a read of the request: the cockpit is
asked about the answer's attempt, and the card comes back only when it says that attempt was refused. H4: a challenge that is not
base64url is named and no passkey is asked. H4/H7: a remembered credential id that does not decode is dropped (the OS's
own list), never a throw that blocks every tap; a remembered id the authenticator refuses (NotAllowedError) is asked once
more without it, and the record then names the passkey that signed. H8: a reply missing its fields never prints
"undefined"."""
from __future__ import annotations

import json

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import DESKTOP, FACET_NOTE, OPEN, OPTION_B, QRID, STRIP_FACET, TOK, _Rig, needs_ssh

CRED = "bGV2YWluLWNhcmQtY3JlZA"        # base64url of the rig's credential id (b"levain-card-cred")
OTHER = "c29tZS1vdGhlci1jcmVk"         # base64url of a credential id the authenticator does not hold
RECORD = "window.localStorage.getItem('levain-passkeys:localhost')"
# counts every navigator.credentials.get the page makes from now on, and the allowCredentials each one named
SPY = ("(() => { window.__gets = []; const real = navigator.credentials.get.bind(navigator.credentials); "
       "navigator.credentials.get = (o) => { window.__gets.push(((o.publicKey || {}).allowCredentials || []).length); "
       "return real(o); }; return 1; })()")
# the board's reload is counted and does nothing, so the card that ran the ceremony stays on the page to be read
HOLD_RELOAD = ("(() => { const t = window.LevainCockpitTransport; window.__reloads = 0; "
               "t.reload = function () { window.__reloads++; }; return 1; })()")
# the first POST whose path ends with `tail` reaches the server, and its reply is lost on the way back
LOSE_REPLY = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
              "const p = real.apply(this, arguments); if (armed && String(u).endsWith({tail})) {{ armed = false; "
              "return p.then(() => {{ throw new TypeError('the reply was lost'); }}); }} return p; }}; return 1; }})()")
# the first POST whose path ends with `tail` never leaves the page
NEVER_SENT = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
              "if (armed && String(u).endsWith({tail})) {{ armed = false; return Promise.reject(new TypeError('lost')); }} "
              "return real.apply(this, arguments); }}; return 1; }})()")
# the first POST whose path ends with `tail` reaches the server, and the page reads `patch` merged over its JSON reply
PATCH_REPLY = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
               "const p = real.apply(this, arguments); if (armed && String(u).endsWith({tail})) {{ armed = false; "
               "return p.then((r) => r.json()).then((b) => new Response(JSON.stringify({replace} ? {patch} : "
               "Object.assign(b, {patch})), {{ status: 200, headers: {{ 'Content-Type': 'application/json' }} }})); }} "
               "return p; }}; return 1; }})()")
BUTTONS = "[...document.querySelectorAll('#levain-modal .needs-question .sign-option')].map((b) => b.disabled)"
AFTER = "new Promise((r) => setTimeout(() => r(1), {ms}))"


def _patch(tail: str, patch: dict, *, replace: bool = False) -> dict:
    return {"eval": PATCH_REPLY.format(tail=json.dumps(tail), patch=json.dumps(patch), replace=json.dumps(replace))}


def _remember(rig: _Rig, raw_id: str) -> dict:
    return {"eval": f"window.localStorage.setItem('levain-passkeys:localhost', "
                    f"{json.dumps(json.dumps({rig.pk.key.fingerprint: raw_id}))}) || 1"}


def _card(rig: _Rig, *steps: dict) -> list:
    return [DESKTOP, rig.authenticator(), *steps, {"until": STRIP_FACET + " && 1", "ms": 30000}, {"eval": SPY},
            {"eval": HOLD_RELOAD}]


# --- H2: "not sure" never re-arms ---------------------------------------------------------------------------------------


@needs_browser
@needs_ssh
def test_h2_an_answer_that_landed_with_its_reply_lost_is_confirmed_signed_by_its_attempt(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            {"eval": LOSE_REPLY.format(tail=json.dumps("/answer"))},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/the cockpit confirmed it/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": AFTER.format(ms=2000)},
            {"eval": BUTTONS},
            {"eval": "window.__reloads"},
        ])
    v = out["values"]
    assert str(v[5]).startswith("signed (b) · by " + rig.pk.key.fingerprint), v[5]
    assert v[7] == [True, True, True], v[7]               # frozen: the request it signed is closed
    assert v[8] >= 1, v[8]                                # and the board is told to read again
    assert rig.verifies(QRID)                             # the answer did land


@needs_browser
@needs_ssh
def test_h2_an_answer_that_never_left_is_refused_by_its_attempt_and_only_then_the_card_comes_back(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            {"eval": NEVER_SENT.format(tail=json.dumps("/answer"))},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"!{OPTION_B}.disabled && {FACET_NOTE}", "ms": 30000},
            {"eval": OPEN % QRID},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
        ])
    v = out["values"]
    assert "never reached the cockpit" in v[5] and "sign it again" in v[5], v[5]   # the cockpit's word, not a read
    assert v[6] is True                                   # it was open: nothing had been signed
    assert rig.verifies(QRID)                             # and the card signed it on the next tap


# --- H4: a malformed challenge asks no passkey ------------------------------------------------------------------------


@needs_browser
def test_h4_a_malformed_challenge_is_named_and_no_passkey_is_asked(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            _patch("/challenge", {"challenge": ""}),
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/nothing was signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"until": f"!{OPTION_B}.disabled && 1", "ms": 5000},
            _patch("/challenge", {"challenge": "no+such/text="}),
            {"eval": f"(() => {{ document.querySelector('#levain-modal .needs-question .sign-note').textContent = ''; "
                     f"{OPTION_B}.click(); return 1; }})()"},
            {"until": f"/nothing was signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": "window.__gets.length"},
        ])
    v = out["values"]
    for said in (v[5], v[9]):
        assert said.startswith("the cockpit sent a malformed challenge"), said
    assert v[10] == 0, v[10]                              # navigator.credentials.get was never called
    assert rig.nothing_signed(QRID)


# --- H4/H7: a remembered credential id that does not decode, or that the authenticator refuses -------------------------


@needs_browser
@needs_ssh
def test_h7_a_remembered_id_that_does_not_decode_is_dropped_and_the_card_signs(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig, _remember(rig, "%%% not an id")),
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/^signed|nothing was signed|not sure/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": "window.__gets"},
            {"eval": RECORD},
        ])
    v = out["values"]
    assert str(v[5]).startswith("signed (b)"), v[5]
    assert v[6] == [0], v[6]                              # one prompt, asking for no id (the OS's own list)
    assert json.loads(v[7]) == {rig.pk.key.fingerprint: CRED}, v[7]   # the record now names the passkey that signed
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_h7_a_remembered_id_the_authenticator_refuses_is_asked_once_more_without_it(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig, _remember(rig, OTHER)),
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/^signed|nothing was signed|not sure/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 90000},
            {"eval": "window.__gets"},
            {"eval": RECORD},
        ])
    v = out["values"]
    assert str(v[5]).startswith("signed (b)"), v[5]
    assert v[6] == [1, 0], v[6]                           # the remembered id, refused; then once with none
    assert json.loads(v[7]) == {rig.pk.key.fingerprint: CRED}, v[7]
    assert rig.verifies(QRID)


# --- H8: a reply missing its fields ------------------------------------------------------------------------------------


@needs_browser
@needs_ssh
def test_h8_a_signed_reply_missing_its_fields_prints_what_was_sent_and_never_undefined(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            _patch("/answer", {}, replace=True),
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
        ])
    said = out["values"][5]
    assert "undefined" not in said, said
    assert said.startswith("signed (b) · by " + rig.pk.key.fingerprint + ": levain-sign-choice/1 chosen (b)"), said
    assert rig.verifies(QRID)


# --- the focus view closed and opened again while an attempt is in flight ---------------------------------------------


@needs_browser
@needs_ssh
def test_a_focus_view_opened_again_while_the_cockpit_verifies_shows_the_frozen_card_and_tells_it_the_answer(tmp_path, monkeypatch) -> None:
    """An answer whose reply was lost, that the cockpit is still verifying, is asked about by its attempt, never by a read
    of the request. The card is built again when the focus view is closed and opened: while the attempt is unsettled it
    is drawn frozen, never with live buttons, and it is told the cockpit's word with the first card (signed here: it
    stays frozen, the request it signed being closed)."""
    from tests.test_sign_attempt_page import CARD_SENT_THEN_LOST, _hold_verify

    rig = _Rig(tmp_path, monkeypatch)
    close = "(document.querySelector('#levain-modal .modal-close').click(), 1)"
    reopen = "(document.querySelector('#ck-needs .needs-row.needs-question').click(), 1)"
    with rig.serving():
        go = _hold_verify(monkeypatch, 6.0)
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), {"until": STRIP_FACET + " && 1", "ms": 30000},   # the board reads go on
            {"eval": CARD_SENT_THEN_LOST.format(tail=json.dumps("/answer"))},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/still verifying/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": close},
            {"eval": reopen},
            {"until": "document.querySelector('#levain-modal .needs-question .sign-option') && 1", "ms": 20000},
            {"eval": BUTTONS},
            {"eval": AFTER.format(ms=7000)},                  # the verify is let through meanwhile
            {"eval": "window.LevainCockpitTransport.reload() && 1"},                 # the next board read asks again
            {"until": f"/the cockpit confirmed it/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 45000},
            {"eval": BUTTONS},
        ])
        go.set()
    v = out["values"]
    assert v[7] == [True, True, True], v               # drawn frozen while the attempt is unsettled
    assert v[10].startswith("signed (b)"), v          # told the cockpit's word
    assert v[11] == [True, True, True], v             # signed: the request is closed, the card stays frozen
    assert rig.verifies(QRID)
