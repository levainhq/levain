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
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import FACET_NOTE, OPTION_B, QRID, TOK, _Rig, needs_ssh
from tests.test_cockpit_sign_card_r5 import AFTER, BUTTONS, LOSE_REPLY, _card
from tests.test_sign_sheet_page import DESKTOP, NOTE, STATE, _button, _open, _stub

# the answer is told to the page as lost at once, and reaches the server `ms` later (the sheet's stub form)
SHEET_LATE = ("(real, u, o) => {{ setTimeout(() => real(u, o), {ms}); "
              "return Promise.reject(new TypeError('the reply was lost')); }}")
# the card's form: the first POST ending in `tail` is lost to the page at once and reaches the server `ms` later
CARD_LATE = ("(() => {{ const real = window.fetch; let armed = true; window.fetch = function (u, o) {{ "
             "if (armed && String(u).endsWith({tail})) {{ armed = false; const a = arguments; const self = this; "
             "setTimeout(() => real.apply(self, a), {ms}); return Promise.reject(new TypeError('the reply was lost')); }} "
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
    # the class: controls handed back as "nothing was signed" while the answer then signed
    assert not (re_armed and rig.verifies(QRID)), v
    if re_armed:
        assert rig.nothing_signed(QRID), v


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
    assert not (re_armed and rig.verifies(QRID)), v
    if re_armed:
        assert rig.nothing_signed(QRID), v
