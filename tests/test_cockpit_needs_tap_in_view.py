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
    probe = ("JSON.stringify((() => { const o = document.getElementById('levain-modal'); const m = o && o.querySelector('.modal');"
             " const r = m ? m.getBoundingClientRect() : null; const a = document.activeElement;"
             " const fr = a && a.getBoundingClientRect ? a.getBoundingClientRect() : null;"
             " return {open: !!m, top: r && r.top, bottom: r && r.bottom, w: r && r.width, h: r && r.height, ih: innerHeight, sy: scrollY,"
             " focusIn: !!(m && a && m.contains(a)),"
             " focusInView: !!(fr && fr.width > 0 && fr.height > 0 && fr.top >= 0 && fr.bottom <= innerHeight + 1),"
             " pos: o && getComputedStyle(o).position}; })())")
    after = ("JSON.stringify({sy: scrollY, modalGone: !document.getElementById('levain-modal'), anyModal: !!document.querySelector('#levain-modal, .modal-overlay'),"
             " tag: document.activeElement && document.activeElement.className,"
             " locked: document.body.classList.contains('needs-locked'), bodyPos: document.body.style.position, bodyTop: document.body.style.top,"
             f" onLast: document.activeElement === {last}, ih: innerHeight, rowTop: {last}.getBoundingClientRect().top}})")
    record_press = ("(() => { document.addEventListener('click', (e) => { if (e.isTrusted && e.target.closest && e.target.closest('.needs-row'))"
                    " window.__pressY = scrollY; }, true); return 1; })()")
    steps = [{"emulate": PHONE}, {"until": "!!document.querySelector('#ck-needs .needs-more-row') && 1", "ms": 20000},
             {"eval": "document.querySelector('#ck-needs .needs-more-row').click(); 1"},   # the list folds past a few rows: open it
             {"until": READY, "ms": 20000},
             {"eval": "new Promise(r => setTimeout(() => r(1), 600))"},
             {"eval": record_press},    # scrollY at the trusted press itself, after every settle: the place the reader had
             {"eval": f"(() => {{ {last}.scrollIntoView({{block: 'center'}}); {last}.focus(); return scrollY; }})()"},
             {"eval": "new Promise(r => setTimeout(() => r(1), 300))"},
             {"tap": last},
             {"until": "document.getElementById('levain-modal')"},
             {"eval": "new Promise(r => setTimeout(() => r(1), 800))"},
             {"eval": probe},
             {"eval": "(() => { window.scrollTo(0, 0); return 1; })()"},   # a locked body that jumps (iOS), as the page behind the view
             {"key": "Escape", "vk": 27},    # a trusted key to the element holding focus inside the view
             {"eval": "new Promise(r => setTimeout(() => r(1), 400))"},
             {"eval": after}, {"eval": "window.__pressY"}]
    # the driver collects a value per step except emulate and key: map each step to its slot in "values"
    slot = [sum(1 for t in steps[:n] if "emulate" not in t and "key" not in t) for n in range(len(steps))]
    i_probe, i_after, i_press = (slot[steps.index({"eval": x})] for x in (probe, after, steps[-1]["eval"]))
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", steps)["values"]
    opened = json.loads(vals[i_probe])
    closed = json.loads(vals[i_after])
    sy0 = vals[i_press]       # scrollY at the trusted press (the page is locked at open, so the open-time reading is not the place)
    print("MEASURED", opened, closed, sy0)
    assert isinstance(sy0, (int, float)) and sy0 > 300, sy0                          # a long list scrolled well away from the top
    assert opened["open"], opened
    assert opened["w"] > 0 and opened["h"] > 0, opened
    assert opened["pos"] == "fixed", opened
    assert opened["top"] >= 0 and opened["bottom"] <= opened["ih"] + 1, opened      # the detail is on screen
    assert opened["focusIn"] and opened["focusInView"], opened                       # focus moved into it, and it is in view
    assert closed["modalGone"] and not closed["anyModal"], closed                    # the view is gone
    assert not closed["locked"] and closed["bodyPos"] != "fixed" and closed["bodyTop"] == "", closed   # the body lock is cleared
    assert abs(closed["sy"] - sy0) <= 4, (closed, sy0)                               # the page is where it was
    assert closed["onLast"], closed                                                  # focus returned to the tapped row
