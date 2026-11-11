"""The cockpit's progressive first draw (S4) in a real browser: the board draws as its panels land, and the page says when it is WHOLE.

The first board is drawn in parts and the whole draw then REPLACES every node, so anything a reader (or a test) did on a part was lost
under them (the decisions tile that lost its focus, a /?view=needs jump that landed 495 px off, consent rows tapped on a part). Two earlier
fixes marked the PARTIAL board with a flag that something had to clear, and each review found a new way for it to stay set or clear early.
The frame is the other way round: ready is a POSITIVE whole-board signal, `html[data-board-whole]`, set when the whole
board is drawn or when no whole draw is coming (a failed or refused read), never on a partial draw, and the partial board is drawn inert
(the regions the draw replaces carry `inert`, dimmed) so nothing can be done on it.

The partial window is made long by holding every panel read after the first four (the core: activity, state, begin, decisions) until the
test releases them from the page, so each state can be read in it and the window lasts exactly as long as the test needs. Steps in that window say `"partial": true` (the driver otherwise waits for
the positive signal before every step)."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k4a_needs import DESKTOP

# every panel read after the first four distinct ones is held until the test calls window.__releasePanels() (a step of its own); the core four
# answer at once, so the board draws a part, and the rest land together at the release. The partial window therefore lasts exactly as long as
# the test needs, with no wall clock to lose on a loaded runner (the one bound is the page's own per-panel timeout, PANEL_TIMEOUT_MS in
# dashboard_boot.js: a read held longer than that fails, so the steps before the release must finish inside it). `__held` counts the held
# reads started and answered. The init step reloads the page once; this runs in the reloaded document.
DELAY = """(() => { const f = window.fetch; const seen = new Set(); let open = null; const gate = new Promise((r) => { open = r; });
  window.__releasePanels = () => { open(); return 1; }; window.__held = { started: 0, done: 0 };
  window.fetch = async (...a) => {
  const u = String(a[0]);
  if (u.indexOf('/cockpit/panel/') === 0) { seen.add(u); if (seen.size > 4) { window.__held.started++; await gate; const r = await f.apply(window, a); window.__held.done++; return r; } }
  return f.apply(window, a); };
  // what the page looked like at the moment the signal appeared (a mutation observer runs right after the task that set the attribute): how many
  // elements were still inert, and how many tiles the widgets region held
  window.__wholeLog = [];
  new MutationObserver(() => { if (document.documentElement.hasAttribute('data-board-whole') && !window.__wholeLog.length)
    window.__wholeLog.push({ inert: document.querySelectorAll('[inert]').length, tiles: document.querySelectorAll('#ck-widgets .ck-card').length }); })
    .observe(document, { attributes: true, subtree: true, attributeFilter: ['data-board-whole'] });   // the init script runs before <html> exists
})();"""
RELEASE = "window.__releasePanels()"

# the regions the draw replaces (dashboard_boot.js BOARD_REGIONS): #board holds the zones, and its siblings hold the hero, the Needs strip, the
# notes, the brief and the widgets (the tiles), where a reader first acts. The masthead and the nav are not among them, and neither is the
# drop (#ck-drop): its card is built once and refreshed in place, never replaced, so an inert drop would only stop the Drop tab's focus.
REGIONS = ["ck-hero", "ck-needs", "ck-notes", "ck-brief", "ck-widgets", "state", "board"]
# the board is a part: the regions are inert, a tile is drawn in the widgets region, and the signal is not there
PART_UP = ("(() => { const w = document.getElementById('ck-widgets'); return !!w && w.hasAttribute('inert') && !!w.querySelector('.ck-card') && "
           "!document.documentElement.hasAttribute('data-board-whole') && 1; })()")
WHOLE_UP = "document.documentElement.hasAttribute('data-board-whole') && 1"
# How long the tests wait for the signal after the release. It MUST stay well inside the page's watchdog (BOARD_HOLD_MS in dashboard_boot.js,
# a minute from the first inert part): the watchdog also releases the page and sets the signal, so a wait that could outlast it would let a
# whole draw that forgot to set the signal pass on the watchdog's release (found by mutation: with this at 60 s, deleting the whole draw's
# boardMark(true) left both tests green). The steps before the release take a few seconds, so 30 s here keeps the signal the whole draw's own.
WHOLE_WAIT_MS = 30000
STATE = """JSON.stringify((() => { const h = document.documentElement; const ids = REGIONS;
  const present = ids.filter((id) => document.getElementById(id));
  const focusable = document.querySelector('#ck-widgets .ck-card, #ck-needs button, #board button'); let focused = null;
  if (focusable) { if (!focusable.hasAttribute('tabindex')) focusable.setAttribute('tabindex', '-1'); focusable.focus(); focused = document.activeElement === focusable; }
  const live = (sel) => { const n = document.querySelector(sel); return !!n && !n.closest('[inert]'); };
  const w = document.getElementById('ck-widgets');
  // the state line is inert and sits inside the hero (the now band's slot): it is dimmed through the hero, once, never twice
  const st = document.getElementById('state'); let stateEff = null;
  if (st && !st.hidden) { stateEff = 1; for (let n = st; n; n = n.parentElement) stateEff *= parseFloat(getComputedStyle(n).opacity); }
  const dropEl = document.getElementById('ck-drop');
  return { stateEff: stateEff, heroOpacity: getComputedStyle(document.getElementById('ck-hero')).opacity, stateInHero: !!st && !!st.closest('#ck-hero'),
    dropInert: !!dropEl && !!dropEl.closest('[inert]'), dropPresent: !!dropEl, inert: present.filter((id) => document.getElementById(id).hasAttribute('inert')), present: present, whole: h.hasAttribute('data-board-whole'),
    tiles: w.querySelectorAll('.ck-card').length, opacity: getComputedStyle(w).opacity, focusableFound: !!focusable, focused: focused,
    live: { sync: live('#refresh'), nav: live('[data-phone-jump]'), theme: live('[data-theme-set]'), tab: live('[data-mode-tab]'), masthead: live('.masthead') },
    inertElsewhere: [...document.querySelectorAll('[inert]')].filter((n) => ids.indexOf(n.id) < 0).length }; })())""".replace("REGIONS", json.dumps(REGIONS))

# a press at the centre of the first tile in the viewport, whatever the page's hit test says: the driver's tap step presses where `contains` says
# the element is, so the stand-in below hands it the tile's rectangle. The listeners record where every press landed.
LISTEN = """(() => { window.__hits = []; for (const t of ['pointerdown', 'mousedown', 'mouseup', 'click'])
  window.addEventListener(t, (ev) => { window.__hits.push({ type: t, inTile: !!(ev.target && ev.target.closest && ev.target.closest('#ck-widgets .ck-card')), at: ev.target && (ev.target.id || ev.target.tagName) }); }, true); })()"""
AT_CARD = """(() => { const c = document.querySelector('#ck-widgets .ck-card'); if (!c) return null; c.scrollIntoView({ block: 'center' }); const r = c.getBoundingClientRect();
  return { disabled: false, ownerDocument: document, scrollIntoView() {}, getBoundingClientRect: () => r, contains: () => true }; })()"""
HITS = "JSON.stringify(window.__hits)"


def _boot(tmp_path: Path):
    _root, src, _ids = _install(tmp_path)
    return _serve(src, surface_tokens={"browser": "T"}, install_class="reported")


@needs_browser
def test_a_partial_board_is_inert_and_not_whole_and_the_whole_draw_is_live_and_says_so(tmp_path: Path) -> None:
    """Partial: every region the draw replaces carries inert (the masthead, the nav, Sync and the theme switch do not), the part is dimmed, a tile
    on it cannot take focus, a press at the tile's centre lands outside it (the probe: CDP mouse events skip inert nodes), and the positive
    signal is absent. After the whole draw: nothing is inert, the signal is there, and when the signal appeared nothing was inert any more and
    the tiles were drawn (same turn). The same focus attempt and the same press now reach the tile (the controls that show the probes can
    see a live one)."""
    with _boot(tmp_path) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"init": DELAY}, DESKTOP,
            {"until": PART_UP, "ms": 40000, "partial": True},
            {"eval": STATE, "partial": True},
            {"tap": AT_CARD, "before": LISTEN, "ms": 5000, "partial": True},
            {"eval": HITS, "partial": True},
            {"eval": RELEASE, "partial": True},
            {"until": WHOLE_UP, "ms": WHOLE_WAIT_MS, "partial": True},
            {"eval": STATE, "partial": True},
            {"eval": "JSON.stringify(window.__wholeLog)", "partial": True},
            {"tap": AT_CARD, "before": "window.__hits = []", "ms": 5000},
            {"eval": HITS},
        ])["values"]
    assert len(out) == 10, out
    part, hits_part, whole, whole_log, hits_whole = (json.loads(out[i]) for i in (1, 3, 6, 7, 9))
    assert part["whole"] is False and part["tiles"] > 0, part
    assert part["inert"] == part["present"] and "ck-widgets" in part["inert"] and "board" in part["inert"], part   # every region the draw replaces
    assert float(part["opacity"]) < 1, part                                   # inert has no look of its own: the part is dimmed
    assert part["focusableFound"] and part["focused"] is False, part          # nothing on the part takes focus
    assert all(part["live"].values()) and part["inertElsewhere"] == 0, part   # the masthead, the nav, Sync and the theme switch stay live
    assert part["dropPresent"] and part["dropInert"] is False, part          # the drop is never replaced by the whole draw: it stays live (its tab focuses the box)
    assert part["stateInHero"] and (part["stateEff"] is None or abs(part["stateEff"] - float(part["heroOpacity"])) < 0.01), part   # dimmed once, through the hero
    assert hits_part and all(h["inTile"] is False for h in hits_part), hits_part   # the press went to the page behind, never into the tile
    assert whole["inert"] == [] and whole["whole"] is True and float(whole["opacity"]) == 1, whole
    assert whole["focused"] is True, whole                                    # control: the same focus attempt lands on the live tile
    assert len(whole_log) == 1 and whole_log[0]["inert"] == 0 and whole_log[0]["tiles"] > 0, whole_log   # inert was already gone when the signal came
    assert any(h["inTile"] for h in hits_whole), hits_whole                   # control: the same press on the whole board lands on the tile


@needs_browser
def test_a_whole_draw_deferred_for_an_open_editor_leaves_the_part_inert_and_the_signal_unset_until_it_lands(tmp_path: Path) -> None:
    """An editor open when the rest of the panels land defers the whole draw (the page's own rule: a draw that would replace what the reader is
    typing in waits). The signal stays unset and the part stays inert for as long as that holds, and both turn over when the deferred draw lands
    after the editor closes. (The signal is never set on a claim the board will not be replaced: the deferred draw would falsify it.)"""
    ADD_EDITOR = "(() => { const e = document.createElement('div'); e.className = 'state-editor'; e.id = 'fake-editor'; document.body.appendChild(e); return 1; })()"
    DROP_EDITOR = "(() => { document.getElementById('fake-editor').remove(); return 1; })()"
    # past the release of the held panels, with the editor still open: the page reads its panels a few at a time, so `started` counts only the
    # reads begun so far and a tick between two batches sees done == started with more to come. So: wait until every held read begun so far has
    # been answered, then a settle in which the whole draw would have landed had the editor not deferred it (a longer settle only strengthens
    # the test; the correct code never draws), and take it as settled only if NO further read began during that settle (else wait again)
    SETTLED = ("new Promise((r) => { const t0 = Date.now(); const tick = () => { const h = window.__held; "
               "if (Date.now() - t0 > 20000) r(0); else if (h.started > 0 && h.done >= h.started) { const n = h.started; "
               "setTimeout(() => { const g = window.__held; if (g.started === n && g.done === n) r(1); else tick(); }, 3000); } else setTimeout(tick, 100); }; tick(); })")
    with _boot(tmp_path) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"init": DELAY}, DESKTOP,
            {"until": PART_UP, "ms": 40000, "partial": True},
            {"eval": ADD_EDITOR, "partial": True},
            {"eval": RELEASE, "partial": True},
            {"eval": SETTLED, "partial": True},
            {"eval": STATE, "partial": True},
            {"eval": DROP_EDITOR, "partial": True},
            {"until": WHOLE_UP, "ms": WHOLE_WAIT_MS, "partial": True},
            {"eval": STATE, "partial": True},
        ])["values"]
    assert out[3] in (1, "1"), out                                                  # every held read was answered before the state was read
    held, landed = json.loads(out[4]), json.loads(out[7])
    assert held["whole"] is False and held["inert"] == held["present"], held      # the panels have all landed, the draw is deferred: still a part
    assert landed["whole"] is True and landed["inert"] == [], landed
