"""The Needs-you band is a grid of compact cards (Phill 11-06): 3 across at desktop, 2 on an iPad, 1 on a phone. The face is the
band number, the first sentence (clamped to 2 lines) and its blocks line; it holds no answer control, and a click opens the
focus view (today's detail). The voice button is not built: the cockpit's speak route reads a room reply by its gseq, not a band row."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_needs_rows import LONG_OPTS, READY, ROWS, _file

SIZES = (("desktop", {"w": 1280, "h": 900, "dpr": 1}, 3), ("ipad", {"w": 820, "h": 1100, "dpr": 1}, 2),
         ("phone", {"w": 390, "h": 844, "dpr": 2, "mobile": True}, 1))
SHOTS = os.environ.get("CARDS_SHOTS")
PROBE = (f"JSON.stringify({{cols: getComputedStyle(document.querySelector('#ck-needs .needs-grid')).gridTemplateColumns.split(' ').length,"
         f" lefts: new Set({ROWS}.map(r => Math.round(r.getBoundingClientRect().left))).size,"
         f" titles: {ROWS}.map(r => {{ const t = r.querySelector('.needs-row-title'); const cs = getComputedStyle(t);"
         f"   return {{lines: cs.webkitLineClamp, h: t.getBoundingClientRect().height, lh: parseFloat(cs.lineHeight), text: t.textContent}}; }}),"
         f" bands: {ROWS}.map(r => (r.querySelector('.needs-row-band') || {{}}).textContent),"
         f" subs: {ROWS}.map(r => (r.querySelector('.needs-row-sub') || {{}}).textContent),"
         f" inner: document.querySelectorAll('#ck-needs .needs-row button, #ck-needs .needs-row input, #ck-needs .needs-row textarea, #ck-needs .needs-row a').length}})")


@needs_browser
def test_the_band_is_a_card_grid_with_a_clamped_first_sentence_face(tmp_path: Path, monkeypatch) -> None:
    _file(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    steps: list = []
    for name, emu, _cols in SIZES:
        steps += [{"emulate": emu}, {"until": READY, "ms": 20000}, {"eval": "new Promise(r => setTimeout(() => r(1), 600))"}, {"eval": PROBE}]
        if SHOTS:
            steps.append({"shot": str(Path(SHOTS) / f"cards_{name}.png"), "full": True})
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", steps)["values"]
    got = [json.loads(v) for v in vals if isinstance(v, str) and v.startswith("{")]
    assert len(got) == 3, vals
    for (name, _emu, cols), g in zip(SIZES, got):
        assert g["cols"] == cols, (name, g)
        assert g["lefts"] == cols, (name, g)                       # three cards sit in `cols` distinct columns
        for t in g["titles"]:
            assert t["lines"] == "2", (name, t)                     # the 2-line clamp is in force
            assert t["h"] <= t["lh"] * 2 + 2, (name, t)             # and the long item's title is no taller than two lines
            assert not t["text"].startswith("THE") and t["text"].count(".") <= 1, (name, t)   # the first sentence only
        assert any(t["text"].startswith("Which way should the long decision go?") for t in g["titles"]), (name, g["titles"])
        assert all(b for b in g["bands"]), (name, g["bands"])        # every card leads with its band number
        assert any(s and s.startswith("blocks: ") for s in g["subs"]), (name, g["subs"])
        assert g["inner"] == 0, (name, g)                            # the face carries no answer control: the card is the one button


@needs_browser
def test_a_click_on_a_card_opens_the_focus_view(tmp_path: Path, monkeypatch) -> None:
    _file(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    click = f"(() => {{ if (document.querySelector('#levain-modal .needs-question')) return 1; const r = {ROWS}.find(x => /rec b/.test(x.textContent)); r.click(); return 1; }})()"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", [{"emulate": SIZES[0][1]}, {"until": READY, "ms": 20000},
            {"do": click, "until": "document.querySelectorAll('#levain-modal .needs-question .question-option').length === 3"},
            {"eval": "document.querySelectorAll('#levain-modal .question-option').length"}])["values"]
    assert vals[-1] == len(LONG_OPTS)


@needs_browser
def test_a_read_only_surface_with_no_band_transport_still_draws_the_cards(tmp_path: Path, monkeypatch) -> None:
    """codex HIGH: a read-only manifest surface passes no bandRead; the cards then draw without band numbers, never an error."""
    _file(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    src = dataclasses.replace(src, write_scope=None)      # a read-only surface: dashboard_boot passes no bandRead
    probe = (f"JSON.stringify({{n: {ROWS}.length, err: /could not|error/i.test(document.getElementById('ck-needs').innerText),"
             f" bands: {ROWS}.map(r => (r.querySelector('.needs-row-band') || {{}}).textContent || '')}})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/", [{"emulate": SIZES[0][1]}, {"until": f"{ROWS}.length === 3", "ms": 20000},
                                            {"eval": "new Promise(r => setTimeout(() => r(1), 600))"}, {"eval": probe}])["values"]
    got = json.loads(next(v for v in vals if isinstance(v, str) and v.startswith("{")))
    assert got["n"] == 3 and not got["err"], got
    assert all(b == "" for b in got["bands"]), got           # no transport, no numbers
