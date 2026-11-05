"""A passkey prompt is asked for by a tap and by nothing else (Phill 11-04: the installed cockpit app put up a Touch ID prompt
in a loop; a passkey sheet takes the window's focus and gives it back, so no focus, visibility, pageshow, online or
reconnect event, render or timer may reach navigator.credentials.get, and a cancelled or refused prompt is never asked again
by the page: the card says so and the next tap asks).

Over the rig and helpers of test_cockpit_sign_card_r5 (a served cockpit, a virtual platform authenticator holding the
Signer's passkey, navigator.credentials.get counted by the allowCredentials each call named)."""
from __future__ import annotations

import json

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import FACET_NOTE, OPTION_B, QRID, TOK, _Rig, needs_ssh
from tests.test_cockpit_sign_card_r5 import AFTER, CRED, OTHER, RECORD, _card, _remember
from tests.test_cockpit_sign_card_r6 import CANCEL_ALL

# what the OS does to a window around a passkey sheet, and what a phone or an installed app does around a sleep: every one
# dispatched at the window and the document, twice over, with the standalone display mode emulated
LIFECYCLE = ("(() => { const w = window, d = document; for (let i = 0; i < 2; i++) { "
             "w.dispatchEvent(new Event('blur')); d.dispatchEvent(new Event('visibilitychange')); "
             "w.dispatchEvent(new Event('focus')); w.dispatchEvent(new Event('focusin')); "
             "w.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })); "
             "w.dispatchEvent(new Event('offline')); w.dispatchEvent(new Event('online')); "
             "d.dispatchEvent(new CustomEvent('levain:resume', { detail: { hiddenAt: 1 } })); "
             "d.dispatchEvent(new CustomEvent('levain:cockpit-held')); } return 1; })()")
STANDALONE = ("(() => { const real = window.matchMedia.bind(window); window.matchMedia = (q) => "
              "/display-mode:\\s*standalone/.test(q) ? Object.assign(real('all'), { matches: true, media: q }) : real(q); return 1; })()")


@needs_browser
@needs_ssh
def test_focus_visibility_pageshow_online_and_resume_ask_for_no_passkey(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig, _remember(rig, OTHER)),
            {"eval": STANDALONE},
            {"eval": LIFECYCLE},
            {"eval": AFTER.format(ms=1500)},
            {"eval": "window.__gets"},
        ])
    assert out["values"][-1] == [], out["values"]          # a request waited the whole time: nothing asked for a passkey
    assert rig.nothing_signed(QRID)


@needs_browser
@needs_ssh
def test_one_tap_asks_once_and_the_focus_that_follows_asks_for_nothing(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 90000},
            {"eval": LIFECYCLE},
            {"eval": AFTER.format(ms=1500)},
            {"eval": "window.__gets"},
        ])
    v = out["values"]
    assert str(v[-4]).startswith("signed (b)"), v
    assert v[-1] == [0], v                                  # exactly one ask, for the one tap
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_a_cancelled_prompt_is_not_asked_again_and_the_next_tap_asks_once(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig, _remember(rig, OTHER)),
            {"eval": CANCEL_ALL},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/nothing was signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": LIFECYCLE},                            # the sheet's focus coming back
            {"eval": AFTER.format(ms=1500)},
            {"eval": "window.__gets"},                      # the cancel and its focus: ONE ask, the remembered id's
            {"eval": FACET_NOTE},
            {"eval": f"{OPTION_B}.disabled"},
            {"eval": f"{OPTION_B}.click() || 1"},           # the retry is a tap, and asks for the OS's own list
            {"until": "window.__gets.length >= 2 && 1", "ms": 30000},
            {"eval": AFTER.format(ms=1000)},
            {"eval": "window.__gets"},
            {"eval": RECORD},
        ])
    v = out["values"]
    assert v[-8] == [1], v
    assert "tap to try again" in v[-7], v[-7]
    assert v[-6] is False, v                                # the card gave its buttons back
    assert v[-2] == [1, 0], v                               # one more ask for the one more tap, with no id
    assert json.loads(v[-1]) == {rig.pk.key.fingerprint: OTHER}, v[-1]    # and a cancel keeps the remembered passkey
    assert rig.nothing_signed(QRID)


@needs_browser
@needs_ssh
def test_a_refused_remembered_id_is_retried_by_the_next_tap_and_signs(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig, _remember(rig, OTHER)),
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/nothing was signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 90000},
            {"eval": "window.__gets"},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 90000},
            {"eval": "window.__gets"},
            {"eval": RECORD},
        ])
    v = out["values"]
    assert v[-5] == [1], v                                  # the remembered id, refused, and no second ask by the page
    assert v[-2] == [1, 0], v                               # the next tap: none
    assert json.loads(v[-1]) == {rig.pk.key.fingerprint: CRED}, v[-1]
    assert rig.verifies(QRID)
