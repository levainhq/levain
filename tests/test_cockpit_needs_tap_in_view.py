"""A tapped Needs-you row opens its detail IN VIEW (Phill 11-06: with a long list, tapping an item "looks like nothing opens but it
does, you have to scroll up like 3 pages"). On a phone, with a long list scrolled to its last row, the focus view is on screen,
holds focus, and closing it returns the page to the same scroll and the focus to the tapped row."""
from __future__ import annotations

import json
from pathlib import Path

from levain.consent.questions import QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

N = 30
PHONE = {"w": 390, "h": 844, "dpr": 2, "mobile": True}
ROWS = "[...document.querySelectorAll('#ck-needs .needs-row')]"
READY = f"{ROWS}.length === {N} && 1"


def _file_many(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    store = QuestionStore(home / "consent")
    for i in range(N):
        store.file(agent="claude_code", agent_session_id=f"s{i}", what=f"Question number {i}: which way?",
                   options=[("a", "Yes"), ("b", "No")], rec="a", blocks="waits", cwd="/w")


@needs_browser
def test_tapped_row_detail_opens_in_view_and_close_restores_place(tmp_path: Path, monkeypatch) -> None:
    _file_many(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    last = f"{ROWS}[{ROWS}.length - 1]"
    probe = ("JSON.stringify((() => { const m = document.querySelector('#levain-modal .modal') || document.querySelector('#levain-modal');"
             " const r = m ? m.getBoundingClientRect() : null; const a = document.activeElement;"
             " return {open: !!m, top: r && r.top, bottom: r && r.bottom, ih: innerHeight, sy: scrollY,"
             " focusIn: !!(m && a && m.contains(a)), pos: m && getComputedStyle(document.getElementById('levain-modal')).position}; })())")
    after = ("JSON.stringify({sy: scrollY, open: !!document.getElementById('levain-modal'), tag: document.activeElement && document.activeElement.className,"
             f" onLast: document.activeElement === {last}, ih: innerHeight, rowTop: {last}.getBoundingClientRect().top}})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", [{"emulate": PHONE}, {"until": "!!document.querySelector('#ck-needs .needs-more-row') && 1", "ms": 20000},
            {"eval": "document.querySelector('#ck-needs .needs-more-row').click(); 1"},   # the list folds past a few rows: open it
            {"until": READY, "ms": 20000},
            {"eval": "new Promise(r => setTimeout(() => r(1), 600))"},
            {"eval": f"(() => {{ {last}.scrollIntoView({{block: 'center'}}); window.__sy0 = scrollY; {last}.focus(); return scrollY; }})()"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 300))"},
            {"tap": last},
            {"until": "document.getElementById('levain-modal')"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 800))"},
            {"eval": probe},
            {"eval": "(() => { window.scrollTo(0, 0); return 1; })()"},   # a locked body that jumps (iOS), as the page behind the view
            {"eval": "(() => { document.querySelector('#levain-modal').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); return 1; })()"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 400))"},
            {"eval": after}, {"eval": "window.__sy0"}])["values"]
    objs = [json.loads(v) for v in vals if isinstance(v, str) and v.startswith("{")]
    opened = next(o for o in objs if "focusIn" in o)
    closed = next(o for o in objs if "onLast" in o)
    sy0 = vals[-1]            # scrollY just before the tap (the page is locked at open, so the open-time reading is not the place)
    print("MEASURED", opened, closed, sy0)
    assert opened["open"], opened
    assert opened["pos"] == "fixed", opened
    assert opened["top"] >= 0 and opened["bottom"] <= opened["ih"] + 1, opened      # the detail is on screen
    assert opened["focusIn"], opened                                                 # focus moved into it
    assert abs(closed["sy"] - sy0) <= 4, (closed, sy0)                               # the page is where it was
    assert closed["onLast"], closed                                                  # focus returned to the tapped row
