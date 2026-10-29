"""The attempt-status construct through the pages (levain project_memory/1026-1/land2/RULINGS_INTEGRATION.md, "THE UNSURE
CONSTRUCT"): headless Chrome (its own profile, tests/_page_drive_run.py through ``_drive``) on a real cockpit server with a
virtual authenticator, over the rigs of test_sign_sheet_page and test_cockpit_sign_card_r5.

The class: an answer whose reply was lost may still be verifying on the server, so no number of reads of the request can
show it was refused. The sheet and the card used to re-arm after two open reads; an answer that reached the server after
both then signed under re-armed controls. Now the page asks the Signer for the attempt's outcome: only "refused" re-arms,
"signed" shows signed, and an attempt the server never saw is fenced by that read (refused, and a later arrival of it is
refused too)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import FACET_NOTE, OPTION_B, QRID, STRIP_FACET, TOK, _Rig, needs_ssh
from tests.test_cockpit_sign_card_r5 import AFTER, BUTTONS, _card
from tests.test_sign_sheet_page import DESKTOP, NOTE, STATE, _button, _open, _stub

# the answer is told to the page as lost at once, and reaches the server `ms` later (the sheet's stub form)
SHEET_LATE = ("(real, u, o) => {{ setTimeout(() => real(u, o), {ms}); "
              "return Promise.reject(new TypeError('the reply was lost')); }}")
# the card's form: the first POST ending in `tail` is lost to the page at once and reaches the server `ms` later
CARD_LATE = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
             "if (armed && String(u).endsWith({tail})) {{ armed = false; const a = arguments; const self = this; "
             "setTimeout(() => real.apply(self, a), {ms}); return Promise.reject(new TypeError('the reply was lost')); }} "
             "return real.apply(this, arguments); }}; return 1; }})()")
# the answer reaches the cockpit, and half a second later (it is verifying by then) the page is told its reply was lost
SHEET_SENT_THEN_LOST = ("(real, u, o) => { real(u, o).catch(() => 0); "
                        "return new Promise((res, rej) => setTimeout(() => rej(new TypeError('the reply was lost')), 500)); }")
CARD_SENT_THEN_LOST = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
                       "if (armed && String(u).endsWith({tail})) {{ armed = false; real.apply(this, arguments).catch(() => 0); "
                       "return new Promise((res, rej) => setTimeout(() => rej(new TypeError('the reply was lost')), 500)); }} "
                       "return real.apply(this, arguments); }}; return 1; }})()")
SHEET_ARMED = f"[{_button('a')}.disabled, {_button('b')}.disabled]"
# the answer arrives this long after the page was told it was lost: past both of the old heuristic's reads
LATE_MS = 4000


@needs_browser
@needs_ssh
def test_red_the_sheet_never_re_arms_over_an_answer_that_then_signs(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(),
            {"eval": f"window.__stub['/answer'] = {SHEET_LATE.format(ms=LATE_MS)}; {_button('b')}.click() || 1"},
            {"eval": AFTER.format(ms=LATE_MS + 2500)},
            {"eval": SHEET_ARMED},
            {"eval": NOTE},
            {"eval": STATE},
        ])
    v = out["values"]
    re_armed = v[-3] == [False, False]
    signed = not rig.nothing_signed(QRID) and rig.verifies(QRID)
    # the class: controls handed back as "nothing was signed" while the answer then signed
    assert not (re_armed and signed), v
    # the construct: the status read fenced the attempt the cockpit had not seen, so its late answer was refused
    assert re_armed and rig.nothing_signed(QRID) and "never reached" in v[-2], v


@needs_browser
@needs_ssh
def test_red_the_card_never_re_arms_over_an_answer_that_then_signs(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            *_card(rig),
            {"eval": CARD_LATE.format(tail=json.dumps("/answer"), ms=LATE_MS)},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"eval": AFTER.format(ms=LATE_MS + 2500)},
            {"eval": BUTTONS},
            {"eval": FACET_NOTE},
        ])
    v = out["values"]
    re_armed = v[-2] == [False, False, False]
    signed = not rig.nothing_signed(QRID) and rig.verifies(QRID)
    assert not (re_armed and signed), v
    assert re_armed and rig.nothing_signed(QRID) and "never reached" in v[-1], v


def _hold_verify(monkeypatch, release_after_s: float) -> threading.Event:
    """The cockpit's signature verify waits until the returned event is set (set by a timer `release_after_s` from now),
    and a status read waits at most half a second: an answer that is still verifying past the read's bound."""
    import levain.authority as authority
    from levain.consent import sign as signmod

    go = threading.Event()
    real = authority.verify_link_sig

    def held(*a, **k):
        go.wait(60)
        return real(*a, **k)

    monkeypatch.setattr(authority, "verify_link_sig", held)
    monkeypatch.setattr(signmod, "ATTEMPT_WAIT_S", 0.5)
    timer = threading.Timer(release_after_s, go.set)
    timer.daemon = True
    timer.start()
    return go


@needs_browser
@needs_ssh
def test_the_sheet_holds_an_answer_still_verifying_not_confirmed_until_check_again_finds_it_signed(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        go = _hold_verify(monkeypatch, 8.0)
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, rig.authenticator(), *_open(rig), _stub(),
            {"eval": f"window.__stub['/answer'] = {SHEET_SENT_THEN_LOST}; {_button('b')}.click() || 1"},
            {"until": f"/still verifying/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": SHEET_ARMED},
            {"eval": f"(document.getElementById('sheet-signing') || {{}}).textContent"},
            {"eval": "!!document.getElementById('sheet-recheck')"},
            {"eval": AFTER.format(ms=7000)},                  # the verify is let through meanwhile
            {"eval": "document.getElementById('sheet-recheck').click() || 1"},
            {"until": f"{STATE} === 'signed' && {NOTE}", "ms": 30000},
            {"eval": "document.getElementById('sheet-actions').hidden"},
        ])
        go.set()
    v = out["values"]
    assert v[5] == [True, True], v                        # frozen while the cockpit is still verifying
    assert v[6].startswith("Not confirmed:"), v
    assert v[7] is True, v                                # and "Check again" is offered, never pressed for him
    assert v[10].startswith("signed (b)") and "the cockpit confirmed it" in v[10], v
    assert v[11] is True, v                               # signed: nothing is offered any more
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_the_card_holds_an_answer_still_verifying_frozen_until_a_board_read_finds_it_signed(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        go = _hold_verify(monkeypatch, 8.0)
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(), {"until": STRIP_FACET + " && 1", "ms": 30000},
            {"eval": CARD_SENT_THEN_LOST.format(tail=json.dumps("/answer"))},
            {"eval": f"{OPTION_B}.click() || 1"},
            {"until": f"/still verifying/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": BUTTONS},
            {"eval": AFTER.format(ms=3000)},                  # the board read after "not sure" has come and gone
            {"eval": BUTTONS},
            {"eval": FACET_NOTE},
            {"eval": AFTER.format(ms=4000)},                  # the verify is let through meanwhile
            {"eval": "window.LevainCockpitTransport.reload() && 1"},                 # the next board read
            {"until": f"/the cockpit confirmed it/.test({FACET_NOTE}) && {FACET_NOTE}", "ms": 30000},
            {"eval": BUTTONS},
        ])
        go.set()
    v = out["values"]
    assert v[4] == [True, True, True], v[4]
    assert v[6] == [True, True, True], v[6]               # still frozen: the cockpit had not settled it
    assert "still verifying" in v[7] and "not confirmed" in v[7], v[7]
    assert v[10].startswith("signed (b)"), v[10]
    assert v[11] == [True, True, True], v[11]
    assert rig.verifies(QRID)
