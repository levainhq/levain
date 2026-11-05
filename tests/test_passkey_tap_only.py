"""A passkey prompt is asked for by a tap and by nothing else (Phill 11-04: the installed cockpit app put up a Touch ID prompt
in a loop; a passkey sheet takes the window's focus and gives it back, so no focus, visibility, pageshow, online or
reconnect event, render or timer may reach navigator.credentials.get, and a cancelled or refused prompt is never asked again
by the page: the card says so and the next tap asks).

Over the rig and helpers of test_cockpit_sign_card_r5 (a served cockpit, a virtual platform authenticator holding the
Signer's passkey, navigator.credentials.get counted by the allowCredentials each call named)."""
from __future__ import annotations

import json
import re
from pathlib import Path

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
# (a plain MediaQueryList-shaped object: a real one's matches and media are read-only, so Object.assign over one throws)
STANDALONE = ("(() => { const real = window.matchMedia.bind(window); window.matchMedia = (q) => "
              "/display-mode:\\s*standalone/.test(q) ? { matches: true, media: q, onchange: null, addEventListener() {}, "
              "removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent() { return false; } } : real(q); "
              "return 1; })()")
# a stalled read: the first POST whose path ends with `tail` is held `ms` before it is sent
STALL = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
         "if (armed && String(u).endsWith({tail})) {{ armed = false; const a = arguments, t = this; "
         "return new Promise((r) => setTimeout(r, {ms})).then(() => real.apply(t, a)); }} return real.apply(this, arguments); }}; "
         "return 1; }})()")
WRITE_IN = "document.querySelector('#levain-modal .needs-question .sign-write-in')"
NOTE_TEXT = "document.querySelector('#levain-modal .needs-question .sign-note')"
BOX = "document.querySelector('[data-ck-compose=recipient]').closest('.tray-dump')"
BOX_MSG = f"({BOX}.querySelector('.edit-msg') || {{}}).textContent"
WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"


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
            {"tap": OPTION_B},
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
            {"tap": OPTION_B},
            {"until": f"/nothing was signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": LIFECYCLE},                            # the sheet's focus coming back
            {"eval": AFTER.format(ms=1500)},
            {"eval": "window.__gets"},                      # the cancel and its focus: ONE ask, the remembered id's
            {"eval": FACET_NOTE},
            {"eval": f"{OPTION_B}.disabled"},
            {"tap": OPTION_B},           # the retry is a tap, and asks for the OS's own list
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
            {"tap": OPTION_B},
            {"until": f"/nothing was signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 90000},
            {"eval": "window.__gets"},
            {"tap": OPTION_B},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 90000},
            {"eval": "window.__gets"},
            {"eval": RECORD},
        ])
    v = out["values"]
    assert v[-5] == [1], v                                  # the remembered id, refused, and no second ask by the page
    assert v[-2] == [1, 0], v                               # the next tap: none
    assert json.loads(v[-1]) == {rig.pk.key.fingerprint: CRED}, v[-1]
    assert rig.verifies(QRID)


# --- L3 r1 (project_memory/1104-8/L3_passkey_r1.txt): the door's own refusal, the refusal's end, what arms the door ---


@needs_browser
@needs_ssh
def test_an_evaluated_click_asks_for_no_passkey_and_keeps_the_remembered_one(tmp_path, monkeypatch) -> None:
    """A script's .click() is not a person's tap: no prompt, the card says a tap tries again, and the door's own refusal is
    not an authenticator's, so the next real tap still asks for the remembered passkey by its id."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig, _remember(rig, CRED)),
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/no passkey prompt was shown/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": AFTER.format(ms=1000)},
            {"eval": "window.__gets"},
            {"tap": OPTION_B},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 90000},
            {"eval": "window.__gets"},
        ])
    v = out["values"]
    assert "nothing was signed: tap to try again" in v[-6], v[-6]
    assert v[-4] == [], v                                   # the evaluated click asked for nothing
    assert v[-1] == [1], v                                  # the tap asked once, for the remembered passkey by its id
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_typing_in_a_text_box_and_a_tap_off_any_control_arm_nothing(tmp_path, monkeypatch) -> None:
    """Space typed into the write-in, Enter typed into the compose box and a tap on the card's words are a person's, but
    none presses a control: an evaluated click after each still asks for no passkey."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            {"eval": f"{WRITE_IN}.focus() || 1"},
            {"key": " "},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"eval": AFTER.format(ms=1500)},
            {"eval": f"{BOX}.querySelector('.tray-dump-ta').focus() || 1"},
            {"key": "Enter"},
            {"until": f"!{OPTION_B}.disabled && 1", "ms": 30000},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"eval": AFTER.format(ms=1500)},
            {"tap": NOTE_TEXT},
            {"until": f"!{OPTION_B}.disabled && 1", "ms": 30000},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"eval": AFTER.format(ms=1500)},
            {"eval": "window.__gets"},
            {"eval": FACET_NOTE},
        ])
    v = out["values"]
    assert v[-2] == [], v
    assert "no passkey prompt was shown" in v[-1], v[-1]
    assert rig.nothing_signed(QRID)


@needs_browser
@needs_ssh
def test_a_tap_whose_challenge_stalls_past_the_arm_asks_for_nothing_and_the_next_tap_signs(tmp_path, monkeypatch) -> None:
    """The arm lapses a few seconds after its tap (SIGN_ARM_MS): a challenge read held 11.5 s reaches the door with no
    tap behind it, and no prompt comes up long after the tap; the card says a tap tries again, and that tap signs."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            {"eval": STALL.format(tail=json.dumps("/challenge"), ms=11500)},
            {"tap": OPTION_B},
            {"until": f"/no passkey prompt was shown/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 40000},
            {"eval": "window.__gets"},
            {"tap": OPTION_B},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 90000},
            {"eval": "window.__gets.length"},
        ])
    v = out["values"]
    assert v[-4] == [], v
    assert v[-1] == 1, v
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_space_on_the_focused_option_is_a_tap_and_signs(tmp_path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            {"eval": f"{OPTION_B}.focus() || 1"},
            {"key": " "},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 90000},
            {"eval": "window.__gets.length"},
        ])
    assert str(out["values"][-2]).startswith("signed (b)"), out["values"]
    assert out["values"][-1] == 1, out["values"]
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_a_refusal_ends_when_a_passkey_signs_and_the_next_ceremony_asks_by_id(tmp_path, monkeypatch) -> None:
    """The remembered id refused (one ask, by id), the next tap asks the OS's own list and signs, which records the passkey
    that signed and ends the refusal: the ceremony after it (a signed message from the compose box) asks by id again."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig, _remember(rig, OTHER)),
            {"tap": OPTION_B},
            {"until": f"/nothing was signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 90000},
            {"tap": OPTION_B},
            {"until": f"/^signed/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 90000},
            {"eval": "window.__gets"},
            {"tap": "document.querySelector('#levain-modal .modal-close')"},    # the focus view closed, as a person does
            {"until": BOX + " && 1"},
            {"eval": f"(() => {{ const t = {BOX}.querySelector('.tray-dump-ta'); t.value = 'after the cancel'; "
                     "t.dispatchEvent(new Event('input')); return 1; })()"},
            {"tap": f"{BOX}.querySelector('.tray-dump-btn')"},
            {"eval": f"(() => {{ const y = {BOX}.querySelector('.verb-confirm'); if (y) y.click(); return 1; }})()"},
            {"until": f"{BOX} && {BOX}.querySelector('.tray-dump-ta').value === '' && 1", "ms": 30000},
            {"eval": "window.__gets"},
            {"eval": RECORD},
        ])
    v = out["values"]
    assert v[-9] == [1, 0], v
    assert v[-2] == [1, 0, 1], v                            # the third ceremony asked for the recorded passkey by its id
    assert json.loads(v[-1]) == {rig.pk.key.fingerprint: CRED}, v[-1]
    assert rig.verifies(QRID) and rig.verifies("np-compose-0001")


def test_the_two_doors_arm_by_one_rule() -> None:
    """dashboard_core.js and sign.js each hold their own door (two pages), and tapArmable is one rule written twice: the
    two copies are the same text."""
    def rule(name: str) -> str:
        src = (WEB / name).read_text(encoding="utf-8")
        m = re.search(r"// tapArmable:begin\n(.*?)// tapArmable:end", src, re.S)
        assert m, name
        return m.group(1)
    assert rule("dashboard_core.js") == rule("sign.js")
    assert "ev.isTrusted" in rule("sign.js")
