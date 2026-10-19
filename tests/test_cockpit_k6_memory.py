"""K6: the Memory tab, and the clipped-text defect on the Morning glance (ITEM 0).

A glance tile never clips its text: the earlier test (test_no_tile_draws_outside_its_own_box...) measured against the card, whose
padding hides a cut at the body's edge, so a status word cut by the body ("failin") passed. These measure against the box that
actually cuts (the tile body, the nearest overflow-hidden ancestor), and by the text's own rendered range."""

from __future__ import annotations

from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

HEALTH = "[...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => /^Health/i.test(c.innerText))"
DESKTOP = {"emulate": {"w": 1440, "h": 900, "dpr": 1}}
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}

# every element with its own text in a glance tile whose text is cut: wider than its box, or past the edge of the nearest clipping ancestor
PROBE = """const strip = innerWidth > 700; 
  const clips = (e) => { for (let a = e.parentElement; a; a = a.parentElement) { const o = getComputedStyle(a).overflowX; if (o !== 'visible') return a; } return null; };
  const bad = [];
  for (const tile of document.querySelectorAll('.ck-widgets .ck-card.w')) {
    for (const e of tile.querySelectorAll('*')) {
      const own = [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
      if (!own) continue;
      if (strip && e.classList.contains('sub')) continue;   // the desktop strip's one-line sub is cut with an ellipsis ON PURPOSE (the tile opens the whole)
      const r = document.createRange(); r.selectNodeContents(e); const t = r.getBoundingClientRect();
      if (t.width === 0) continue;
      const c = clips(e), cr = c ? c.getBoundingClientRect() : null;
      const cut = (c && (t.right > cr.right + 1 || t.left < cr.left - 1)) || e.scrollWidth > e.clientWidth + 1;
      if (cut) bad.push([tile.innerText.slice(0, 14).replace(/\\n/g, ' '), e.textContent.trim().slice(0, 20), Math.round(t.right), c ? Math.round(cr.right) : null, e.scrollWidth, e.clientWidth]);
    }
  }
  return bad;"""
CLIPPED = "(async () => { await new Promise(r => setTimeout(r, 400)); " + PROBE + " })()"


@needs_browser
def test_no_glance_tile_clips_its_text_at_phone_or_desktop_width(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP, {"until": f"{HEALTH} && {HEALTH}.querySelector('.xnote')"}, {"eval": CLIPPED},
            PHONE, {"eval": CLIPPED},
        ])["values"]
    assert out[1] == [], ("desktop", out[1])
    assert out[2] == [], ("phone", out[2])


# The fixture's own words fit the tile in the headless font and not in the real one ("failin" in the shot), so the probe puts a status
# row of the longest realistic words through the tile's own markup: a state word and its reading, in a tile ~163px wide.
LONG_ROW = """(() => { const tile = [...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => c.querySelector('.op') && c.getBoundingClientRect().width > 0);
  const row = tile.querySelector('.op').cloneNode(true); row.querySelector('.op-l').textContent = 'cockpit keys mac';
  row.querySelector('.v').textContent = 'failing,3/7'; tile.querySelector('.op').parentElement.appendChild(row); return true; })()"""


@needs_browser
def test_a_status_row_with_a_long_value_wraps_instead_of_clipping_in_a_narrow_tile(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            PHONE, {"until": "document.querySelector('.ck-widgets .op')"}, {"eval": "(async () => { await new Promise(r => setTimeout(r, 400)); " + LONG_ROW + "; " + PROBE + " })()"},
        ])["values"]
    assert out[1] == [], ("390", out[1])


# ───────────────────────────── the Memory tab ─────────────────────────────
MEMORY = f"#write_token=T&memory"
SHAPE = """(async () => { await new Promise(r => setTimeout(r, 700));
  const vis = (n) => !!n && n.getClientRects().length > 0 && getComputedStyle(n).visibility !== 'hidden';
  const neo = document.querySelector('.ck-mem-neo');
  const row = document.querySelector('.ck-mem-row');
  const kids = row ? [...row.children].map(n => { const r = n.getBoundingClientRect(); return [n.dataset.ckPanel, Math.round(r.top), Math.round(r.width), Math.round(r.height)]; }) : [];
  const id = document.querySelector('.ck-mem-ident');
  const crys = document.querySelector('.ck-mem-crys'), cog = document.querySelector('.ck-mem-cog');
  return {
    neo: !!neo,
    neoTitle: neo ? neo.querySelector(':scope > .phead h2').textContent : null,
    sections: neo ? [...neo.querySelectorAll('.ck-mem-nest')].map(n => [n.dataset.ckPanel, vis(n), n.querySelector('.phead h2').textContent]) : [],
    sectionTabs: document.querySelectorAll('.ck-zone[data-zone="mind"] .ck-tabs').length,
    emptyLine: neo && neo.querySelector(':scope > .pbody > .empty, :scope > .empty') ? neo.querySelector('.empty').textContent : null,
    healthStrip: [...document.querySelectorAll('.ck-widgets .ck-card.w')].some(vis),
    row: kids, rowCols: row ? getComputedStyle(row).gridTemplateColumns.split(' ').length : 0,
    crysText: crys ? crys.innerText : null, crysH: crys ? Math.round(crys.getBoundingClientRect().height) : null,
    cogText: cog ? cog.innerText : null, cogH: cog ? Math.round(cog.getBoundingClientRect().height) : null,
    cogTabs: cog ? cog.querySelectorAll('.ck-tabs').length : null,
    ident: id ? { head: id.querySelector(':scope > .phead').innerText.replace(/\\s+/g, ' '), items: id.querySelectorAll('.ck-mem-item').length, rows: [...id.querySelectorAll('.ck-mem-irow')].map(r => [r.children[0].textContent, r.children[1].textContent, !!r.querySelector('.ck-mem-edit')]),
      stray: document.querySelectorAll('.ck-zone[data-zone="identity"] .ck-zgrid > .panel:not(.ck-mem-card)').length } : null,
  }; })()"""


def _shape(tmp_path: Path, width: int = 1440) -> dict:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": width, "h": 900, "dpr": 1}}, {"until": "document.querySelector('.ck-mem-neo, .ck-zone[data-zone=\"mind\"] .panel')"},
            {"eval": SHAPE},
        ])["values"][-1]


@needs_browser
def test_the_neocortex_is_one_card_with_every_section_readable_at_once(tmp_path: Path) -> None:
    s = _shape(tmp_path)
    assert s["neo"] and s["neoTitle"] == "Neocortex", s
    assert s["sectionTabs"] == 0, s                       # no section tabs: nothing is behind a click
    assert s["sections"] and all(vis for _id, vis, _t in s["sections"]), s    # every section the card holds is on screen together
    assert all(t.strip() for _id, _v, t in s["sections"]), s
    assert s["healthStrip"] is False, s                   # the Health strip is retired from this tab (its numbers are Cognition's header)


@needs_browser
def test_crystals_and_cognition_share_one_row_and_an_empty_one_is_one_line(tmp_path: Path) -> None:
    s = _shape(tmp_path)
    kids = s["row"]
    assert [k[0] for k in kids] == ["crystals", "graph"] or [k[0] for k in kids] == ["crystals", "graph", "wraps"], s
    assert kids[0][1] == kids[1][1], s                    # one row: the two cards start at the same height
    assert s["rowCols"] == 2, s
    assert kids[0][2] > kids[1][2], s                     # crystals take the wider share (about 60 / 40)
    # the fixture has no consolidation and no crystal: each says so in one line, with no chart frame and no tab row
    assert "none crystallized yet" in s["crysText"] and s["crysH"] < 120, s
    assert "trace needs two consolidations" in s["cogText"] and s["cogH"] < 120 and s["cogTabs"] == 0, s


@needs_browser
def test_identity_is_one_eight_row_list_and_edit_opens_the_cards_own_editor(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": "document.querySelector('.ck-mem-ident')"},
            {"eval": SHAPE},
            {"eval": "(async () => { const item = [...document.querySelectorAll('.ck-mem-item')].find(i => i.querySelector('.ck-mem-edit')); "
                     "const card = item.querySelector('.panel'); const shown = () => getComputedStyle(card).display !== 'none'; const before = shown();"
                     "item.querySelector('.ck-mem-edit').click(); await new Promise(r => setTimeout(r, 300)); "
                     "const ed = card.querySelector('.text-editor'); const open = [before, shown(), !!ed && !ed.hidden, !!ed && !!ed.querySelector('textarea')];"
                     "card.querySelector('.verb-cancel').click(); await new Promise(r => setTimeout(r, 300)); return open.concat([shown()]); })()"},
        ])["values"]
    ident = out[1]["ident"]
    assert ident and ident["items"] == 8 and len(ident["rows"]) == 8, out[1]
    assert ident["head"].startswith("Identity") and "8" in ident["head"] and "read at every session start" in ident["head"], ident
    assert ident["stray"] == 0, ident                     # the eight separate cards are gone from the grid
    assert all(label and text for label, text, _e in ident["rows"]), ident
    assert out[2] == [False, True, True, True, False], out[2]   # folded -> Edit opens the same editor -> cancel folds it again
