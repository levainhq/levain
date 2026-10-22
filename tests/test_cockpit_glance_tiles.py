"""An At a Glance tile never clips its own content, and a tile that holds items back opens them all (Phill 10-15: the Health widget
said "+7 more" cut off by the tile's bottom, and "why isn't this like the Decisions Waiting widget and clickable to load a modal that
DOES show all data?").

The default install's Health widget is a status-dots tile with twelve metrics, so it hides most of them: it is a button, the focus view
lists every metric behind a filter box, and nothing in any tile is drawn outside the tile at the page's desktop width."""

from __future__ import annotations

from pathlib import Path

import pytest

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
            # a tile that draws everything it has (no "+N more") is no button; the Levain health face (S4) is one answer
            # over its checks, so it is a button to them without a "+N more" line
            {"eval": "[...document.querySelectorAll('.ck-widgets .ck-card.w')].filter(c => !c.querySelector('.xnote') && c.dataset.ckKind !== 'count' && c.dataset.ckPanel !== 'doctor').map(c => c.getAttribute('role'))"},
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
            # a control inside the tile is its own: a click on it does not open the focus view; a click on the tile does
            {"eval": f"(async () => {{ const t = {HEALTH}; const b = document.createElement('button'); b.textContent = 'inner'; t.querySelector('.k').appendChild(b); b.click(); "
                     "const underButton = !!document.getElementById('levain-modal'); b.remove(); return underButton; })()"},
            {"eval": f"(() => {{ {HEALTH}.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', bubbles: true}})); const m = document.getElementById('levain-modal'); "
                     "return [m.querySelectorAll('.op').length, !!m.querySelector('.xfilter-input'), m.querySelector('.xfilter-count').textContent]; })()"},
            {"eval": "(async () => { const i = document.querySelector('#levain-modal .xfilter-input'); i.value = 'strength'; i.dispatchEvent(new Event('input', {bubbles: true})); "
                     "await new Promise(r => setTimeout(r, 300)); return [document.querySelectorAll('#levain-modal .op').length, document.querySelector('#levain-modal .xfilter-count').textContent]; })()"},
            # the cockpit refreshing while he types rebuilds the open view: the query he typed is still there
            {"eval": "(async () => { document.dispatchEvent(new Event('visibilitychange')); window.dispatchEvent(new Event('focus')); await new Promise(r => setTimeout(r, 600)); "
                     "const i = document.querySelector('#levain-modal .xfilter-input'); return [i && i.value, document.querySelectorAll('#levain-modal .op').length]; })()"},
            {"eval": f"(async () => {{ {ESC}; await new Promise(r => setTimeout(r, 200)); return !document.getElementById('levain-modal'); }})()"},
        ])["values"]
    assert out[1] is False, out                               # the click landed on a button inside the tile
    assert out[2] == [12, True, "12 metrics"], out            # the tile drew four of them: the view draws all twelve
    assert out[3] == [2, "2 of 12"], out                      # avg strength, max strength
    assert out[4] == ["strength", 2], out
    assert out[5] is True, out


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


# A tile built by hand on the live page, through the same `fill` the cockpit uses, in a box of the height we choose.
TILE = """
const K = window.LevainCockpitKinds;
const mk = (h) => { const card = document.createElement('div'); card.className = 'ck-card w'; card.style.cssText = `width:300px;height:${h}px;position:absolute;left:0;top:0`;
  const p = document.createElement('div'); p.className = 'wbody'; card.appendChild(p); document.body.appendChild(card); p.hid = []; p.addEventListener('levain-hidden', (e) => p.hid.push(e.detail)); return [card, p]; };
const metrics = (n) => Array.from({length: n}, (_, i) => ({label: 'm' + i, value: i + 1, unit: '', status: 'ok'}));
const fillTile = (p, data) => K.fill(p, Object.assign({typed: true, search: {}}, data), {widget: true, entry: {title: 'T'}});
const room = (card, h) => { card.style.height = h + 'px'; K.fitWidgets(document.body); };
const fits = (p) => p.scrollHeight <= p.clientHeight + 1;
"""


@needs_browser
def test_a_tile_follows_its_room_and_its_data_and_never_clips_a_reading(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    reading = ("{pid: 'wx', kind: 'reading', reading: {value: 61, unit: '°F', condition: 'Clear and a long way from anything', "
               "hi: 70, lo: 40, as_of: new Date().toISOString(), sub: ["
               + ", ".join(f"{{label: 'wind{i}', value: {i}}}" for i in range(6))
               + ", {label: 'air', value: 41, band: 'good'}]}}")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP, {"until": f"{HEALTH}"},
            # 1: squeezed until the text has to be cut, then given room again: the cut goes with the squeeze
            {"eval": "(async () => { " + TILE + "const [card, p] = mk(4); const s = fillTile(p, {pid: 'sq', kind: 'status-dots', metrics: metrics(4)}); const cut = p.querySelector('.xbody').classList.contains('clamped'); "
                     "room(card, 400); await new Promise(r => setTimeout(r, 100)); return [cut, p.querySelector('.xbody').classList.contains('clamped'), p.hid.slice(-1)[0], p.querySelector('.xnote') === null]; })()"},
            # 3: an openable tile that then faults is no longer a button (hidden 0), and is one again after a good repaint
            {"eval": "(async () => { " + TILE + "const [card, p] = mk(120); const s = fillTile(p, {pid: 'fa', kind: 'status-dots', metrics: metrics(12)}); const before = p.hid.slice(-1)[0]; "
                     "s.fault = 'down'; s.paint(); const during = p.hid.slice(-1)[0]; s.fault = ''; s.paint(); return [before > 0, during, p.hid.slice(-1)[0] > 0]; })()"},
            # 4: a reading with many sub-readings that carry no band, and an age, in a short box: it fits, and says what it left out
            {"eval": "(async () => { " + TILE + f"const [card, p] = mk(150); fillTile(p, {reading}); return [fits(p), p.hid.slice(-1)[0] > 0, /\\+\\d+ more/.test(p.innerText)]; }})()"},
        ])["values"]
    assert out[1] == [True, False, 0, True], out
    assert out[2] == [True, 0, True], out
    assert out[3] == [True, True, True], out


@needs_browser
def test_a_line_tile_keeps_one_observer_and_fits_a_long_note(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP, {"until": f"{HEALTH}"},
            {"eval": """(async () => { const D = window.LevainDashboard; let open = null;
              const tile = (h, note) => { const b = document.createElement('div'); b.className = 'wbody'; b.style.cssText = `width:240px;height:${h}px;position:absolute;left:0;top:0;display:flex;flex-direction:column;overflow:hidden`;
                const n = document.createElement('div'); n.className = 'ext-note'; n.textContent = note; const r = document.createElement('div'); r.className = 'row'; r.textContent = 'one row of the list';
                b.append(n, r); const c = document.createElement('div'); c.className = 'ck-card w'; c.appendChild(b); document.body.appendChild(c); D.fitLineTile(b, (on) => { open = on; }); return b; };
              // each render replaces the last one's tile: only the current one is still watched
              for (let i = 0; i < 5; i++) { const t = tile(80, 'x'); if (i < 4) t.parentNode.remove(); }
              const watched = D.lineTileCount();
              // a long provider note over one row: the tile still fits, and is a button to the rest
              const b = tile(50, 'a long provider note '.repeat(12));
              const n = b.querySelector('.ext-note'); const lh = parseFloat(getComputedStyle(n).lineHeight) || 0; return [watched, n.offsetHeight >= 2 * lh - 1, open]; })()"""},
        ])["values"]
    assert out[1] == [1, True, True], out


@needs_browser
def test_openability_is_one_rule_faults_revoke_it_and_a_heading_never_stands_alone(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP, {"until": f"{HEALTH}"},
            # 8a: a count tile faulted by a refetch is no button, and a good repaint gives the button back
            {"eval": "(async () => { " + TILE + "const [card, p] = mk(120); const s = fillTile(p, {pid: 'cn', face: 'count', kind: 'count', count: 3, lines: [{text: 'a'}]}); const before = p.hid.slice(-1)[0]; "
                     "s.fault = 'down'; s.paint(); const during = p.hid.slice(-1)[0]; s.fault = ''; s.paint(); return [before > 0, during, p.hid.slice(-1)[0] > 0]; })()"},
            # 8b: a faulted status-dots tile whose fault line overflows its box is no button either (the clamp does not re-arm it)
            {"eval": "(async () => { " + TILE + "const [card, p] = mk(14); const s = fillTile(p, {pid: 'fo', kind: 'status-dots', metrics: metrics(2)}); s.fault = 'a long reason it is down '.repeat(8); s.paint(); K.fitWidgets(document.body); "
                     "const during = p.hid.slice(-1)[0]; room(card, 14); return [during, p.hid.slice(-1)[0], s.clamp]; })()"},
            # 8c: the page's own count tile (Decisions) hears the same event: 0 revokes the button, a good paint's 1 gives it back
            {"eval": "(() => { const c = [...document.querySelectorAll('.ck-widgets .ck-card.w')].find(x => x.dataset.ckKind === 'count'); const body = c.querySelector('.wbody'); const was = c.getAttribute('role'); "
                     "body.dispatchEvent(new CustomEvent('levain-hidden', {bubbles: true, detail: 0})); const during = c.getAttribute('role'); "
                     "body.dispatchEvent(new CustomEvent('levain-hidden', {bubbles: true, detail: 1})); return [was, during, c.getAttribute('role')]; })()"},
            # 9: group headings and rows in a box with room for one child: never a heading alone, and "+N more" counts rows
            {"eval": """(async () => { const D = window.LevainDashboard; let open = null;
              const b = document.createElement('div'); b.className = 'wbody'; b.style.cssText = 'width:240px;position:absolute;left:0;top:0;display:flex;flex-direction:column;overflow:hidden';
              for (const g of ['g1', 'g2']) { const h = document.createElement('div'); h.className = 'ext-grouphead'; h.textContent = g; const r = document.createElement('div'); r.className = 'row'; r.textContent = 'row ' + g; b.append(h, r); }
              const c = document.createElement('div'); c.className = 'ck-card w'; c.appendChild(b); document.body.appendChild(c); const first = b.children[1].getBoundingClientRect().bottom - b.getBoundingClientRect().top; b.style.height = Math.ceil(first + 30) + 'px'; D.fitLineTile(b, (on) => { open = on; });
              const kids = [...b.children].map(e => e.className + ':' + e.textContent);
              return [kids, open]; })()"""},
        ])["values"]
    assert out[1] == [True, 0, True], out
    assert out[2][0] == 0 and out[2][1] == 0, out
    assert out[3] == ["button", None, "button"], out
    assert out[4][0] == ["ext-grouphead:g1", "row:row g1", "xnote:+1 more"] and out[4][1] is True, out


@needs_browser
@pytest.mark.parametrize("width", [800, 880])
def test_tiles_keep_a_minimum_width_at_half_screen_laptop_width(tmp_path: Path, width: int) -> None:
    """The column count follows the tile count AND the room: at ~800px (past the 720px phone rule, a half-screen laptop) rows of
    four left tiles of 170-230px and multi-figure tiles clipped. Five, six and eight tiles: none under 200px. 880px is the boundary a
    cap that ignores the grid gap misses (four columns of ~182px)."""
    _root, src, _ids = _install(tmp_path)
    probe = ("(async () => { await new Promise(r => setTimeout(r, 400)); const w = document.querySelector('.ck-widgets'); const K = window.LevainCockpitKinds;"
             " const seed = w.children[0]; const run = (n) => { while (w.children.length < n) w.appendChild(seed.cloneNode(true));"
             " while (w.children.length > n) w.lastChild.remove(); K.fillGrids();"
             " const cards = [...w.children].filter(c => c.offsetParent !== null);"
             " return [w.dataset.shape, cards.length, Math.round(Math.min(...cards.map(c => c.getBoundingClientRect().width)))]; };"
             " return [6, 7, 9].map(run); })()   // one seeded child is hidden: 5, 6 and 8 shown")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": width, "h": 900, "dpr": 1}},
            {"until": "document.querySelector('.ck-widgets') && document.querySelector('.ck-widgets').children.length > 0 && 1"},
            {"eval": probe},
        ])["values"]
    for shape, n, narrowest in out[1]:
        assert shape == "tiles" and n > 4, out
        assert narrowest >= 200, f"a tile is {narrowest}px wide at {width}px with {n} tiles: {out}"
