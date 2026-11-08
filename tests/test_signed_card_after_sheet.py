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
    assert json.loads(out["values"][-1])["note"].startswith("signed (a)"), out["values"]
    assert signed_file.exists()
