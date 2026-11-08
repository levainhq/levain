"""A signed sign sheet marks its own card decided (levain 75, key 1108-41, lane sgc). The cockpit card behind the sheet kept
live sign buttons and an empty sign note after the sheet signed (the strip keeps a card whose item did not change, so the
board read the sheet asks for never redraws it). Browser tests through the shared harness at phone width (390x844)."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_band_pages import PHONE
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import QRID, STRIP_FACET, TOK, _Rig, needs_ssh

FRAME = "document.getElementById('levain-sign-sheet-frame')"
IN = f"({FRAME} && {FRAME}.contentDocument)"
OPEN_SHEET = "document.querySelector('#levain-modal .needs-question .sign-facet .sign-sheet-open')"
CARD = "document.querySelector('#levain-modal .needs-question .sign-facet')"
STATE = (f"JSON.stringify({{note: {CARD}.querySelector('.sign-note').textContent, "
         f"live: [...{CARD}.querySelectorAll('.sign-option, .sign-sheet-open, .sign-write-in')].filter(b => !b.disabled && !b.readOnly).length}})")


def _sign_in_the_sheet(rig: _Rig, tmp_path: Path, then: list[dict]) -> dict:
    with rig.serving():
        return _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            PHONE, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"tap": OPEN_SHEET},
            {"until": f"{IN} && {IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state", "ms": 30000},
            {"tap": f"{IN}.querySelector('#sheet-actions [data-choice=\"a\"]')"},
            {"until": f"/^signed/.test({IN}.getElementById('sheet-note').textContent) && 1", "ms": 30000},
            {"tap": f"{IN}.getElementById('sheet-close')"},
            {"until": f"!{FRAME} && (window.__t = Date.now())", "ms": 5000},
            *then,
        ])


@needs_browser
@needs_ssh
def test_the_card_behind_a_signed_sheet_is_decided_at_once(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    out = _sign_in_the_sheet(rig, tmp_path, [{"eval": STATE}])
    got = json.loads(out["values"][-1])
    assert got["note"].startswith("signed (a)"), got       # the card says what the sheet signed
    assert got["live"] == 0, got                            # no sign button, sheet button or write-in is live on it


@needs_browser
@needs_ssh
def test_a_signed_card_cannot_sign_a_second_time(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    signed_file = rig.qdir / "sign" / f"{QRID}.signed.json"
    out = _sign_in_the_sheet(rig, tmp_path, [
        {"eval": f"(() => {{ const b = {CARD}.querySelector('.sign-option'); b.click(); {CARD}.querySelector('.sign-sheet-open').click(); return 1; }})()"},
        {"eval": f"!!{FRAME}"},                              # a tap on the dead card opened no sheet
        {"eval": STATE},
    ])
    assert out["values"][-2] is False, out["values"]
    # the tap on the dead card asked for no second challenge and posted no second answer: the sheet's own ceremony is the
    # one challenge and the one answer the page made
    route = f"/cockpit/sign/{QRID}/"
    assert len([p for p in out["posts"] if p.startswith(route + "challenge")]) == 1, out["posts"]
    assert len([p for p in out["posts"] if p.startswith(route + "answer")]) == 1, out["posts"]
    assert json.loads(out["values"][-1])["note"].startswith("signed (a)"), out["values"]
    assert signed_file.exists()

# --- the fix round (key 1108-42, lane scfix) ---------------------------------------------------------------------------

# the sheet frame's "signed" message with no ceremony behind it: delivered as the frame's own (its window as source, this origin)
TELL_SIGNED = ("(window.dispatchEvent(new MessageEvent('message', {data: {levainSheet: 'signed', id: '%s', choice: 'a'}, "
               f"origin: location.origin, source: {FRAME}.contentWindow}})), 1)")
SHEET_UP = f"{IN} &&{IN}.getElementById('sheet') && {IN}.getElementById('sheet').dataset.state && 1"
CLOSE_SHEET = "(window.LevainSignSheet.close(), 1)"
CLOSE_FOCUS = "(document.querySelector('#levain-modal .modal-close').click(), 1)"
ALL_CONTROLS = (f"JSON.stringify({{note: {CARD}.querySelector('.sign-note').textContent, "
                f"live: [...{CARD}.querySelectorAll('button, input, textarea, select')].filter(b => !b.disabled && !b.readOnly).length}})")


@needs_browser
@needs_ssh
def test_a_signed_message_decides_a_card_still_being_built(tmp_path: Path, monkeypatch) -> None:
    """The signed message arrives while no card's controls exist (the focus view builds its card each time it opens, and the
    server still lists the request to a read taken before its state shows it): the card built next says signed with every
    control dead, and keeps that note although the listing still names the request."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            PHONE, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"tap": OPEN_SHEET},
            {"until": SHEET_UP, "ms": 30000},
            # the card behind the sheet is closed and opened again: its facet is built anew, and only after the message
            {"eval": CLOSE_FOCUS},
            {"eval": TELL_SIGNED % QRID},
            {"eval": CLOSE_SHEET},
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": ALL_CONTROLS},
        ])
    got = json.loads(out["values"][-1])
    assert got["note"].startswith("signed (a)"), got
    assert got["live"] == 0, got


@needs_browser
@needs_ssh
def test_a_decided_card_disables_every_form_control(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    add = (f"(() => {{ const w = {CARD}; for (const t of ['textarea', 'select', 'button']) {{ const e = document.createElement(t); "
           "e.className = 'extra-' + t; w.appendChild(e); } return 1; })()")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            PHONE, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"tap": OPEN_SHEET},
            {"until": SHEET_UP, "ms": 30000},
            {"eval": add},
            {"eval": TELL_SIGNED % QRID},
            {"eval": ALL_CONTROLS},
        ])
    got = json.loads(out["values"][-1])
    assert got["note"].startswith("signed (a)"), got
    assert got["live"] == 0, got


@needs_browser
@needs_ssh
def test_the_card_shows_the_choice_the_cockpit_confirmed_not_the_sheets_own(tmp_path: Path, monkeypatch) -> None:
    """The sheet's answer reply is lost; the cockpit's attempt read confirms it signed, and says the choice it recorded: the
    card shows that one."""
    rig = _Rig(tmp_path, monkeypatch)
    # in the sheet's own window: the answer's reply is lost once; the attempt read comes back naming choice "b"
    rig_fetch = (f"(() => {{ const w = {FRAME}.contentWindow; const real = w.fetch.bind(w); let lost = false; "
                 "w.fetch = function (u, o) { const url = String(u); "
                 "if (url.endsWith('/answer') && !lost) { lost = true; return real(u, o).then(() => { throw new TypeError('lost'); }); } "
                 "if (url.endsWith('/attempt')) return real(u, o).then((r) => r.json().then((j) => "
                 "new Response(JSON.stringify(Object.assign({}, j, {choice: 'b'})), {status: r.status}))); "
                 "return real(u, o); }; return 1; })()")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            PHONE, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"tap": OPEN_SHEET},
            {"until": SHEET_UP, "ms": 30000},
            {"eval": rig_fetch},
            {"tap": f"{IN}.querySelector('#sheet-actions [data-choice=\"a\"]')"},
            {"until": f"/^signed/.test({CARD}.querySelector('.sign-note').textContent) && 1", "ms": 30000},
            {"eval": STATE},
        ])
    assert json.loads(out["values"][-1])["note"].startswith("signed (b)"), out["values"]


@needs_browser
@needs_ssh
def test_a_signed_message_for_another_request_decides_nothing(tmp_path: Path, monkeypatch) -> None:
    """The cockpit takes a "signed" message only for the request it opened the sheet for."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            PHONE, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"tap": OPEN_SHEET},
            {"until": SHEET_UP, "ms": 30000},
            # the sheet now open is for another request; a "signed" naming the card's request comes from it
            {"eval": CLOSE_SHEET},
            {"eval": "(window.LevainSignSheet.open('np-another-request-01'), 1)"},
            {"until": f"!!{FRAME} && 1", "ms": 5000},
            {"eval": TELL_SIGNED % QRID},
            {"eval": ALL_CONTROLS},
        ])
    got = json.loads(out["values"][-1])
    assert not got["note"].startswith("signed"), got
    assert got["live"] > 0, got


@needs_browser
@needs_ssh
def test_a_refused_settle_after_the_sheet_signed_does_not_reopen_the_card(tmp_path: Path, monkeypatch) -> None:
    """A card frozen on an unsettled answer (its reply lost, the attempt read unanswered) while the sheet signs the same
    request: a later attempt read that says refused gives nothing back, and the note stays signed."""
    rig = _Rig(tmp_path, monkeypatch)
    stub = ("(() => { const real = window.fetch; let lost = false; window.__att = 'down'; window.fetch = function (u, o) { "
            "const url = String(u); "
            "if (url.endsWith('/answer') && !lost) { lost = true; return real.apply(this, arguments).then(() => { throw new TypeError('lost'); }); } "
            "if (url.endsWith('/attempt')) return window.__att === 'down' ? Promise.reject(new TypeError('down')) "
            ": Promise.resolve(new Response(JSON.stringify({state: 'refused', reason: 'stub'}), {status: 200})); "
            "return real.apply(this, arguments); }; return 1; })()")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            PHONE, rig.authenticator(),
            {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": stub},
            {"tap": f"{CARD}.querySelector('.sign-option')"},
            {"until": f"{CARD}.querySelector('.sign-option').disabled && {CARD}.querySelector('.sign-note').textContent && 1", "ms": 30000},
            {"until": f"(window.LevainSignSheet.open('{QRID}'), !!{FRAME}) && 1", "ms": 60000},      # once the card's ceremony lets go
            {"eval": TELL_SIGNED % QRID},
            {"eval": "(window.__att = 'refused', window.__t0 = Date.now(), window.LevainCockpitTransport.reload(), 1)"},
            {"until": "Date.now() - window.__t0 > 2500 && 1", "ms": 10000},
            {"eval": ALL_CONTROLS},
        ])
    got = json.loads(out["values"][-1])
    assert got["note"].startswith("signed (a)"), got
    assert got["live"] == 0, got
