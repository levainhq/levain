"""K6: the Memory tab, and the clipped-text defect on the Morning glance (ITEM 0).

A glance tile never clips its text: the earlier test (test_no_tile_draws_outside_its_own_box...) measured against the card, whose
padding hides a cut at the body's edge, so a status word cut by the body ("failin") passed. These measure against the box that
actually cuts (the tile body, the nearest overflow-hidden ancestor), and by the text's own rendered range."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

HEALTH = "[...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => /^Health/i.test(c.innerText))"
# the Morning glance's own tile (the Health tile is Memory-only: width 0 on Morning, so a wait on it measures other tiles)
DOCTOR = "[...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => /Levain health/i.test(c.innerText) && c.getBoundingClientRect().width > 0)"
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
            DESKTOP, {"until": f"{DOCTOR}"}, {"eval": f"!!({DOCTOR})"}, {"eval": CLIPPED},
            PHONE, {"until": f"{DOCTOR}"}, {"eval": f"!!({DOCTOR})"}, {"eval": CLIPPED},
        ])["values"]
    assert out[1] is True and out[4] is True, ("the Morning doctor tile must be on screen before anything is measured", out)
    assert out[2] == [], ("desktop", out[2])
    assert out[5] == [], ("phone", out[5])


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
def test_identity_cards_are_the_rows_and_edit_opens_in_place_with_nothing_hidden(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": "document.querySelector('.ck-mem-ident')"},
            {"eval": """(async () => { await new Promise(r => setTimeout(r, 400));
  const vis = (n) => !!n && n.getClientRects().length > 0;
  const list = document.querySelector('.ck-mem-ident .ck-mem-ilist');
  const cards = [...list.children];
  const K = window.LevainCockpitKinds;
  const jump = K.jumpItems('Operator').filter((i) => i.kind === 'panel');
  const card = cards.find((c) => c.querySelector('.edit-btn'));
  const btn = card.querySelector('.edit-btn');
  const rb = card.getBoundingClientRect(), bb = btn.getBoundingClientRect();
  const hiddenNodes = document.querySelectorAll('.ck-mem-fold, .ck-mem-irow, .ck-mem-item, .ck-mem-edit').length;
  const closedH = rb.height;
  btn.click(); await new Promise(r => setTimeout(r, 300));
  const ta = card.querySelector('textarea');
  const opened = { ta: vis(ta), focus: document.activeElement === ta, inCard: card.contains(ta), h: card.getBoundingClientRect().height };
  const cancel = card.querySelector('.verb-cancel, .edit-cancel'); cancel.click(); await new Promise(r => setTimeout(r, 300));
  return { n: cards.length, cardsAreRows: cards.every((c) => c.classList.contains('panel') && vis(c)), titles: cards.map((c) => c.querySelector('.phead h2').textContent),
    oneLine: cards.map((c) => Math.round(c.getBoundingClientRect().height)), btnInRow: bb.top >= rb.top && bb.bottom <= rb.bottom && vis(btn),
    hiddenNodes, jump: jump.map((j) => [j.label, vis(j.node)]), opened, closedH, after: Math.round(card.getBoundingClientRect().height), afterFocus: document.activeElement === btn }; })()"""},
        ])["values"]
    r = out[1]
    assert r["n"] == 8 and r["cardsAreRows"], r                       # the eight identity cards are the list's rows, every one on screen
    assert r["hiddenNodes"] == 0, r                                    # no hidden card, no second row built over it, no synthetic Edit button
    assert max(r["oneLine"]) < 80, r                                   # a closed row is one line: title | summary | Edit
    assert r["btnInRow"], r                                            # the card's own Edit button sits in its row
    assert r["jump"] and all(v for _l, v in r["jump"]), r              # the jump box lands on a card that is on screen
    assert r["opened"]["ta"] and r["opened"]["focus"] and r["opened"]["inCard"] and r["opened"]["h"] > r["closedH"] + 40, r   # the editor opens in the row, focused
    assert r["after"] == round(r["closedH"]) and r["afterFocus"], r    # cancel closes it back to one line and focus returns to Edit


# ── the parsed sections lose nothing (R2) ──
RENDER = """(async () => { const E = (t) => ({ kind: 'section', ref: 0, id: 'x', title: t, heading: t, zone: 'mind' });
  const draw = (t, body) => window.LevainDashboard.renderPanel(E(t), { sections: [{ heading: t, body }] });
  %s })()"""

PATTERNS = "\n".join([
    "{today — 2026-10-19}",
    "- !!! absence_of_signal_rendered_as_health | 40x (2026-10-19) [evidence: 2a273b8b \"a failed cat read as HOLDS\"]",
    "- !! a name with spaces | 3x (2026-10-18) (cross-session-overlap) — trailing prose here",
    "",
    "{carried — not re-exercised today}",
    "- ! severity_is_reviewer_noise | 4x (2026-10-16) [no-contradicts]",
])
DECISIONS = "\n".join([
    "[decided, 2026-10-18, Phill] **DIOGENES DRIFT SWEEP DISABLED** [episode: 3da3d496] (*\"the premise\"*) with `code span` after",
    "  a continuation line that belongs to the same decision",
    "[forwarded] **DREAMING BECOMES THE DEFAULT** -> `state/x.md`",
    "[carried] THE BATON (`spore-1322`): every consolidate holds it",
])


@needs_browser
def test_a_patterns_body_and_a_decisions_body_render_losslessly(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": "window.LevainDashboard"},
            {"eval": RENDER % ("""const p = draw('Patterns', %s); const d = draw('Decisions', %s);
  const t = (n) => n.textContent.replace(/\\s+/g, ' ');
  return { pRows: [...p.querySelectorAll('.ck-mem-li')].map((r) => [(r.querySelector('.ck-mem-n') || {}).textContent, (r.querySelector('.ck-mem-name') || {}).textContent, (r.querySelector('.ck-mem-rest') || {}).textContent || '']),
    pGroups: [...p.querySelectorAll('.ck-mem-group')].map((g) => g.textContent), pText: t(p),
    dRows: [...d.querySelectorAll('.ck-mem-li')].map((r) => [(r.querySelector('.ck-mem-when') || {}).textContent, (r.querySelector('.ck-mem-text') || {}).textContent]),
    dCode: d.querySelectorAll('.ck-mem-text code').length, dCites: [...d.querySelectorAll('.ck-mem-cite')].map((c) => c.textContent), dText: t(d) }; """ % (json.dumps(PATTERNS), json.dumps(DECISIONS)))},
        ])["values"]
    r = out[1]
    assert [x[:2] for x in r["pRows"]] == [["40x", "absence_of_signal_rendered_as_health"], ["3x", "a name with spaces"], ["4x", "severity_is_reviewer_noise"]], r
    assert "2a273b8b" in r["pRows"][0][2] and "2026-10-19" in r["pRows"][0][2], r            # the evidence id and the date ride on a muted second line
    assert "trailing prose here" in r["pRows"][1][2] and "cross-session-overlap" in r["pRows"][1][2], r
    assert [g.strip() for g in r["pGroups"]] == ["today — 2026-10-19", "carried — not re-exercised today"], r   # stale patterns are labelled as stale
    assert len(r["dRows"]) == 3, r                                                           # the continuation line joined its decision
    assert r["dRows"][0][0] == "10-18" and "continuation line that belongs" in r["dRows"][0][1], r
    assert r["dCode"] >= 3 and r["dCites"] == ["[episode: 3da3d496]"], r                      # code spans keep their rendering; a citation stays, muted
    assert r["dRows"][1][0] == "forwarded" and r["dRows"][2][0] == "carried", r
    assert "every consolidate holds it" in r["dText"], r


@needs_browser
def test_a_body_the_parsers_do_not_recognise_falls_back_to_its_markdown_whole(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    cases = {"Patterns": PATTERNS + "\nA stray sentence that is not a pattern line.", "Decisions": "Some preface prose.\n" + DECISIONS}
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": "window.LevainDashboard"},
            {"eval": RENDER % ("""const bodies = %s; return Object.keys(bodies).map((k) => { const p = draw(k, bodies[k]); return [k, !!p.querySelector('.ck-mem-list'), !!p.querySelector('.md-p, .md-ul'), p.textContent.includes(k === 'Patterns' ? 'A stray sentence' : 'Some preface prose')]; }); """ % json.dumps(cases))},
        ])["values"]
    assert out[1] == [["Patterns", False, True, True], ["Decisions", False, True, True]], out[1]


@needs_browser
def test_a_long_crystal_name_keeps_its_count_beside_it(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": "window.LevainDashboard && document.querySelector('.ck-mem-row')"},
            {"eval": """(() => { const view = { crystal_index: [{ level: 4, name: 'a_stale_record_is_as_dangerous_as_it_is_trusted_and_then_some_more', one_clause: 'short clause' }, { level: 3, name: 'short', one_clause: '' }] };
  const p = window.LevainDashboard.renderPanel({ kind: 'crystals', id: 'crystals', title: 'Crystallized patterns', zone: 'mind' }, view);
  const host = document.createElement('div'); host.style.width = '230px'; document.querySelector('.ck-zgrid').appendChild(host); host.appendChild(p);
  return [...p.querySelectorAll('.row')].map((r) => { const l = r.querySelector('.lvl').getBoundingClientRect(), n = r.querySelector('.name').getBoundingClientRect(); return [Math.round(l.top), Math.round(n.top), l.right <= n.left + 1, n.right <= r.getBoundingClientRect().right + 1]; }); })()"""},
        ])["values"]
    for lvl_top, name_top, beside, inside in out[1]:
        assert abs(lvl_top - name_top) <= 8 and beside and inside, out[1]     # the count and the name share a top edge, side by side, inside the row


@needs_browser
def test_an_editable_section_with_no_text_keeps_its_edit(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": "window.LevainDashboard"},
            {"eval": """(() => { const e = { kind: 'section', ref: 0, id: 'state', title: 'State', heading: 'State', zone: 'mind', edit_class: 'A', source: 'state' };
  const p = window.LevainDashboard.renderPanel(e, { sections: [{ heading: 'State', body: '' }] });
  return [p.dataset.ckNoText || null, p.dataset.editable || null, p.dataset.ckSlim || null]; })()"""},
        ])["values"]
    assert out[1][1:] == ["1", "1"], out[1]       # empty and editable: not omitted from the card, a slim row that keeps its Edit


def test_the_wraps_line_and_the_cognition_header_read_the_authoritative_figures(tmp_path: Path) -> None:
    from tests.test_cockpit_k2_shell import _cut, _node
    fn = _cut("  const MEM_WRAP_CAP = 50;", "  function memCard(")
    out = _node(r"""
const wr = (n, extra) => Array.from({ length: n }, (_, i) => ({ wrapped_at: new Date(2026, 9, 1 + (i % 28), 6, 0).toISOString() }));
out.capped = memWrapsLine({ wraps: wr(50) });
out.total = memWrapsLine({ wraps: wr(50), health: { total_wraps: 212 } });
out.few = memWrapsLine({ wraps: wr(3) });
// the newest stamp wins wherever it sits in the list
const shuffled = [{ wrapped_at: "2026-10-01T06:00:00" }, { wrapped_at: "2026-10-19T06:02:00" }, { wrapped_at: "2026-10-10T06:00:00" }];
out.lastIsMax = memWrapsLine({ wraps: shuffled }).endsWith("06:02");
out.dark = memCogBits({ write_path_live: false, wraps: wr(2), graph: { nodes: [1], edges: [] } });
out.darkHealth = memCogBits({ health: { write_path_live: false }, wraps: [] });
out.live = memCogBits({ write_path_live: true, wraps: wr(2) });
out.capBits = memCogBits({ wraps: wr(50) });
out.badCrystals = true;
""", tmp_path, fn)
    assert out["capped"].startswith("50+ wraps"), out["capped"]                  # the list is capped at 50: it never claims a total of 50
    assert out["total"].startswith("212 wraps"), out["total"]                    # the authoritative figure wins when the view has it
    assert out["few"].startswith("3 wraps"), out["few"]
    assert out["lastIsMax"] is True
    assert "write path dark" in out["dark"] and "write path dark" in out["darkHealth"], out
    assert "write path dark" not in out["live"], out
    assert any(b.startswith("50+") for b in out["capBits"]), out["capBits"]
