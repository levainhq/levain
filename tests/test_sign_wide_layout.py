"""The sign sheet and /pair at every size (Phill 11-01: "why is it this narrow little strip on a huge empty page"). Below 800px the
page is the 420px phone card, unchanged; from 800px a needs-phill card is a two-column decision sheet (reading left, the answers right
and sticky) and every other sheet and /pair is one wider measure. The stylesheet is pinned as text; the sticky column is run in a browser."""
from __future__ import annotations

import re
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_sign_card import _Rig, needs_ssh
from tests.test_sign_card_format import LONG, _file_card, _open

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
CSS = (WEB / "dashboard.css").read_text()


def _wide_block() -> str:
    m = re.search(r"@media \(min-width: 800px\) \{\n  \.pair-body:not\(\.sheet-embedded\) \{.*?\n\}\n", CSS, re.S)
    assert m, "the >= 800px sign-sheet media block is missing"
    return m.group(0)


def test_the_phone_card_rule_is_unchanged_and_the_wide_rules_live_at_800px() -> None:
    assert ".pair-card { width: min(100%, 420px); background: var(--card); border: 1px solid var(--hair); border-radius: var(--r-lg); padding: var(--sp-7) var(--sp-6); }" in CSS
    wide = _wide_block()
    assert "#sheet:has(#sheet-card)" in wide and "grid-template-areas" in wide and "min(100%, 1120px)" in wide
    assert "position: sticky" in wide and "min(100%, 720px)" in wide
    assert "overflow: hidden" not in wide, "a scroll container above the deciding column would stop it sticking"
    # nothing of the wide design leaks below 800px: every rule that names the two-column areas sits inside the media block
    outside = CSS.replace(wide, "")
    assert "grid-template-areas: \"brand from\"" not in outside and "1120px" not in outside


def test_the_controls_are_one_group_the_page_can_hold_as_a_column() -> None:
    html = (WEB / "sign.html").read_text()
    i = html.index('id="sheet-decide"')
    assert html.index('id="sheet-actions"') > i and html.index('id="sheet-note"') > i and html.index('id="sheet-back"') > i
    assert "#sheet-decide { display: contents; }" in CSS     # below 800px the group is not a box: the old order and spacing


@needs_browser
@needs_ssh
def test_the_deciding_column_holds_its_place_while_the_reading_column_scrolls(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    rid = _file_card(rig, LONG)
    top = "(() => Math.round(document.getElementById('sheet-decide').getBoundingClientRect().top))()"
    cols = ("(() => { const a = document.getElementById('sheet-body').getBoundingClientRect(), "
            "b = document.getElementById('sheet-decide').getBoundingClientRect(); return [a.right <= b.left + 1, Math.round(b.width)]; })()")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            {"emulate": {"w": 1440, "h": 420, "dpr": 1, "mobile": False}}, rig.authenticator(), *_open(rig, rid),
            {"eval": "(() => { const d = document.getElementById('sheet-detail'); if (d) d.open = true; "
                     "const f = document.createElement('div'); f.style.height = '1600px'; document.getElementById('sheet-detail').append(f); return 1; })()"},
            {"eval": cols}, {"eval": top},
            {"eval": "(() => { window.scrollTo(0, 600); return 1; })()"}, {"eval": top},
            {"emulate": {"w": 390, "h": 844, "dpr": 1, "mobile": True}}, {"eval": cols},
        ])
    v = out["values"]
    cols0, top0, top1, cols390 = v[-5], v[-4], v[-2], v[-1]
    assert cols0[0] is True and cols0[1] > 340, v               # 1440px: the answers sit beside the reading
    assert 0 <= top1 <= top0 + 30, v                            # the page scrolled 600px and the column stayed in view, near where it was
    assert cols390[0] is False, v                               # 390px: the answers sit below the reading, never beside it
