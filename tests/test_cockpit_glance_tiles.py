"""An At a Glance tile never clips its own content, and a tile that holds items back opens them all (Phill 10-15: the Health widget
said "+7 more" cut off by the tile's bottom, and "why isn't this like the Decisions Waiting widget and clickable to load a modal that
DOES show all data?").

The default install's Health widget is a status-dots tile with twelve metrics, so it hides most of them: it is a button, the focus view
lists every metric behind a filter box, and nothing in any tile is drawn outside the tile at the page's desktop width."""

from __future__ import annotations

from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

HEALTH = "[...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => /^Health/i.test(c.innerText))"
ESC = "document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))"
DESKTOP = {"emulate": {"w": 1440, "h": 900, "dpr": 1}}


@needs_browser
def test_a_tile_that_holds_items_back_is_a_button_and_one_that_holds_none_is_not(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP,
            {"until": f"{HEALTH} && {HEALTH}.querySelector('.xnote')"},
            {"eval": f"(() => {{ const t = {HEALTH}; return [t.getAttribute('role'), t.getAttribute('aria-haspopup'), t.tabIndex, /\\+\\d+ more/.test(t.innerText)]; }})()"},
            # a tile that draws everything it has (no "+N more") is no button
            {"eval": "[...document.querySelectorAll('.ck-widgets .ck-card.w')].filter(c => !c.querySelector('.xnote') && c.dataset.ckKind !== 'count').map(c => c.getAttribute('role'))"},
        ])["values"]
    assert out[1] == ["button", "dialog", 0, True], out
    assert all(r is None for r in out[2]), out


@needs_browser
def test_the_focus_view_lists_every_metric_behind_a_filter_box(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP,
            {"until": f"{HEALTH} && {HEALTH}.querySelector('.xnote')"},
            {"eval": f"(() => {{ {HEALTH}.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', bubbles: true}})); const m = document.getElementById('levain-modal'); "
                     "return [m.querySelectorAll('.op').length, !!m.querySelector('.xfilter-input'), m.querySelector('.xfilter-count').textContent]; })()"},
            {"eval": "(async () => { const i = document.querySelector('#levain-modal .xfilter-input'); i.value = 'strength'; i.dispatchEvent(new Event('input', {bubbles: true})); "
                     "await new Promise(r => setTimeout(r, 300)); return [document.querySelectorAll('#levain-modal .op').length, document.querySelector('#levain-modal .xfilter-count').textContent]; })()"},
            {"eval": f"(async () => {{ {ESC}; await new Promise(r => setTimeout(r, 200)); return !document.getElementById('levain-modal'); }})()"},
        ])["values"]
    assert out[1] == [12, True, "12 metrics"], out            # the tile drew four of them: the view draws all twelve
    assert out[2] == [2, "2 of 12"], out                      # avg strength, max strength
    assert out[3] is True, out


@needs_browser
def test_no_tile_draws_outside_its_own_box_at_desktop_or_phone_width(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    probe = ("(async () => { await new Promise(r => setTimeout(r, 400)); return [...document.querySelectorAll('.ck-widgets .ck-card.w')].map(c => { const cr = c.getBoundingClientRect(); "
             "const body = c.querySelector('.wbody'); const out = [...c.querySelectorAll('.wbody *')].filter(e => { const r = e.getBoundingClientRect(); "
             "return r.height > 0 && (r.bottom > cr.bottom - 1 || r.right > cr.right + 1); }).length; return [c.innerText.slice(0, 20), out, body.scrollHeight <= body.clientHeight + 1]; }); })()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP, {"until": f"{HEALTH} && {HEALTH}.querySelector('.xnote')"}, {"eval": probe},
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, {"eval": probe},
        ])["values"]
    for tiles in (out[1], out[2]):
        assert tiles and all(t[1] == 0 and t[2] for t in tiles), tiles
