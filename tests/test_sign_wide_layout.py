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
    assert "#sheet-decide, #sheet-decide-in { display: contents; }" in CSS     # below 800px the group is not a box: the old order and spacing
    assert "#sheet-decide-in" in html and html.index('id="sheet-decide-in"') > i
    assert "#sheet-note:empty { margin: 0; padding: 0; min-height: 0; }" in CSS and "#sheet-note:empty { display: none" not in CSS, \
        "the aria-live status line stays rendered when empty"
    assert not re.search(r"#sheet-(detail|note|back) \{ order:", CSS), "reading order is the DOM's, never CSS `order`"


@needs_browser
@needs_ssh
def test_the_deciding_column_holds_its_place_while_the_reading_column_scrolls(tmp_path: Path, monkeypatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    rid = _file_card(rig, LONG)
    top = "(() => Math.round(document.getElementById('sheet-decide-in').getBoundingClientRect().top))()"
    cols = ("(() => { const a = document.getElementById('sheet-body').getBoundingClientRect(), "
            "b = document.getElementById('sheet-actions').getBoundingClientRect(); "
            "return [a.right <= b.left + 1, Math.round(b.width), a.bottom <= b.top + 1, getComputedStyle(document.getElementById('sheet-decide')).display]; })()")
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
    assert cols390[0] is False and cols390[2] is True, v        # 390px: the answers sit below the reading, never beside it
    assert cols390[3] == "contents", v                          # and the group is no box there (its own rect would be all zeros)


TABS = ("(() => { const a = document.activeElement; if (!a || a === document.body) return 'body'; "
        "if (a.dataset && a.dataset.choice) return 'choice:' + a.dataset.choice; "
        "if (a.tagName === 'SUMMARY') return a.parentElement.id; return a.id || a.tagName; })()")


@needs_browser
@needs_ssh
def test_the_tab_order_is_the_reading_order_at_390_and_1440(tmp_path: Path, monkeypatch) -> None:
    """L3 r1 (codex MED): Detail is placed in the DOM where it is read, not moved by CSS `order`. 390px: the signed disclosure,
    the answers, Detail, the way back. 1440px: Detail, the signed disclosure (the reading column, top to bottom), the answers, the way back."""
    rig = _Rig(tmp_path, monkeypatch)
    rid = _file_card(rig, LONG)
    n = 9
    with rig.serving():
        res = {}
        for w, h, mobile in ((390, 844, True), (1440, 900, False)):
            steps = [{"emulate": {"w": w, "h": h, "dpr": 1, "mobile": mobile}}, rig.authenticator(), *_open(rig, rid)]
            for _ in range(n):
                steps += [{"key": "Tab"}, {"eval": TABS}]
            out = _drive(tmp_path, f"{rig.origin}/nope", steps)
            res[w] = out["values"][-n:]
    for w, seq in res.items():
        seq = seq[:seq.index("sheet-home") + 1] if "sheet-home" in seq else seq     # the ring wraps after the way back
        first_choice = next(i for i, x in enumerate(seq) if str(x).startswith("choice:"))
        last_choice = max(i for i, x in enumerate(seq) if str(x).startswith("choice:"))
        assert "sheet-detail" in seq and "sheet-signed" in seq and "sheet-home" in seq, (w, seq)
        d, sg, home = seq.index("sheet-detail"), seq.index("sheet-signed"), seq.index("sheet-home")
        if w == 390:
            assert sg < first_choice and last_choice < d < home, (w, seq)
        else:
            assert d < sg < first_choice and last_choice < home, (w, seq)


@needs_browser
@needs_ssh
def test_a_short_reading_column_beside_a_tall_deciding_one_keeps_every_control_reachable(tmp_path: Path, monkeypatch) -> None:
    """L3 r1 (codex MED): the deciding column stretches the grid rows and its sticky child scrolls inside itself, so with a short
    reading column and many long options in a short window the first and last controls are both reachable and the column
    stays on screen while the page scrolls."""
    from tests.test_sign_card_format import SHORT
    rig = _Rig(tmp_path, monkeypatch)
    rid = _file_card(rig, SHORT)
    f = rig.qdir / "sign" / f"{rid}.json"
    import json
    b = json.loads(f.read_text())
    b["options"] = [{"key": f"o{i}", "text": f"Option number {i}: " + ("a long answer that wraps across several lines " * 3).strip()} for i in range(8)]
    b.pop("recommended", None)
    f.write_text(json.dumps(b))
    rect = ("(() => { const c = document.getElementById('sheet-decide-in'), r = c.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.bottom), "
            "window.innerHeight, c.scrollHeight > c.clientHeight, getComputedStyle(c).position]; })()")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            {"emulate": {"w": 1440, "h": 420, "dpr": 1, "mobile": False}}, rig.authenticator(), *_open(rig, rid),
            {"eval": rect},
            {"eval": "(() => { window.scrollTo(0, 99999); return 1; })()"}, {"eval": rect},
            {"eval": "(() => { const c = document.getElementById('sheet-decide-in'); c.scrollTop = 0; const a = document.querySelector('#sheet-actions [data-choice]').getBoundingClientRect(); "
                     "return Math.round(a.top - c.getBoundingClientRect().top); })()"},
            {"eval": "(() => { const c = document.getElementById('sheet-decide-in'); c.scrollTop = c.scrollHeight; const bs = [...document.querySelectorAll('#sheet-actions [data-choice]')]; "
                     "const l = bs[bs.length - 1].getBoundingClientRect(), r = c.getBoundingClientRect(); return [bs.length, l.bottom <= r.bottom + 1, l.top >= r.top - 1]; })()"},
        ])
    v = out["values"][-5:]
    start, atend, firsttop, last = v[0], v[2], v[3], v[4]
    assert start[4] == "sticky" and start[3] is True, v           # the column is the sticky, internally scrolling child and it overflows
    assert 0 <= atend[0] < atend[2] and atend[1] > 0, v           # after scrolling the page to its end the column is still on screen
    assert 0 <= firsttop < 200, v                                 # the first control is at the column's top
    assert last[0] == 8 and last[1] is True and last[2] is True, v   # the last control is reachable by scrolling the column


def test_every_card_disclosure_summary_has_one_marker_that_turns_when_open() -> None:
    """Detail and the signing disclosure (and the cockpit's signed text, same classes) read as tappable: a drawn chevron, the native
    marker off (iOS Safari too), a 90 degree turn when [open], a visible focus ring, a touch-sized summary, reduced motion respected."""
    for k in ("card-detail", "card-signed"):
        assert f".{k} > summary::-webkit-details-marker" in CSS and "list-style: none" in CSS
        assert f".{k} > summary::after" in CSS and f".{k}[open] > summary::after" in CSS
        assert f".{k} > summary:focus-visible" in CSS
    assert "transform: rotate(-45deg)" in CSS and "transform: rotate(45deg)" in CSS
    assert re.search(r"prefers-reduced-motion: no-preference\) \{ \.card-signed > summary::after.*transition: transform", CSS), "the turn animates only when motion is allowed"
    assert re.search(r"\.card-signed > summary, \.card-detail > summary \{[^}]*min-height: var\(--hit-min, 44px\)", CSS)
