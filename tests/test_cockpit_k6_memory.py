"""K6: the Memory tab, and the clipped-text defect on the Morning glance (ITEM 0).

A glance tile never clips its text: the earlier test (test_no_tile_draws_outside_its_own_box...) measured against the card, whose
padding hides a cut at the body's edge, so a status word cut by the body ("failin") passed. These measure against the box that
actually cuts (the tile body, the nearest overflow-hidden ancestor), and by the text's own rendered range."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

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
    "- !!! absence_of_signal_rendered_as_health | 40x (2026-10-19) [evidence: 2a273b8b \"a failed cat read as HOLDS\"] — tail prose here",
    "- ! name with `code` | 2x (2026-10-18) (cross-session-overlap)",
    "- !! a name with spaces | 3x (2026-10-18) [no-contradicts]",
    "",
    "{carried — not re-exercised today}",
    "- ! severity_is_reviewer_noise | 4x (2026-10-16) (carried-forward)",
    "A stray sentence that is not a pattern line.",
])
DECISIONS = "\n".join([
    "[decided, 2026-10-18, Phill] **DIOGENES DRIFT SWEEP DISABLED** [episode: 3da3d496] (*\"the premise\"*) · **SECOND** [episode: e2] with `code span` after",
    "  a continuation line that belongs to the same decision",
    "",
    "[forwarded] **DREAMING BECOMES THE DEFAULT** -> `state/x.md`",
    "[carried] THE BATON (`spore-1322`): every consolidate holds it",
    "",
    "[rejected, 10-17] Z was turned down",
    "Some preface prose that matches nothing.",
])
# the plain markdown render of a body (a heading that is neither Patterns nor Decisions takes the undecorated path) against the same
# body drawn as Patterns / Decisions: the decorated section keeps every character the markdown showed, minus the chip and date text
# it moved out in front
LOSSLESS = RENDER % ("""const norm = (x) => x.replace(/\\s+/g, ' ').trim();
  const text = (n) => { const c = n.cloneNode(true); for (const x of c.querySelectorAll('.ck-mem-n, .ck-mem-when')) x.remove(); return norm(c.textContent); };
  const body = (n) => n.querySelector('.section-display');
  const pP = draw('Patterns', %s), pD = draw('Decisions', %s), plainP = draw('Notes', %s), plainD = draw('Notes', %s);
  return {
    pText: text(body(pP)), pPlain: norm(body(plainP).textContent.replace(/\\|\\s*\\d+x\\b/g, '')),
    pChips: [...pP.querySelectorAll('.ck-mem-n')].map((x) => x.textContent), pGroups: [...pP.querySelectorAll('.ck-mem-group')].map((x) => x.textContent),
    pNames: [...pP.querySelectorAll('.ck-mem-name')].map((x) => x.textContent), pCode: pP.querySelectorAll('.ck-mem-name code').length,
    pStray: [...body(pP).querySelectorAll('p.md-p')].filter((x) => /stray/.test(x.textContent)).map((x) => x.outerHTML),
    pStrayPlain: [...body(plainP).querySelectorAll('p.md-p')].filter((x) => /stray/.test(x.textContent)).map((x) => x.outerHTML),
    dText: text(body(pD)), dPlain: norm(body(plainD).textContent),
    dWhen: [...pD.querySelectorAll('.ck-mem-when')].map((x) => x.textContent), dCode: pD.querySelectorAll('code').length,
    dRejected: [...body(pD).querySelectorAll('p.md-p')].filter((x) => /rejected/.test(x.textContent)).map((x) => [x.outerHTML, x.querySelectorAll('.ck-mem-when').length]),
    dRejectedPlain: [...body(plainD).querySelectorAll('p.md-p')].filter((x) => /rejected/.test(x.textContent)).map((x) => x.outerHTML),
    dLead: [...body(pD).querySelectorAll('.ck-mem-li')].length };""" % (json.dumps(PATTERNS), json.dumps(DECISIONS), json.dumps(PATTERNS), json.dumps(DECISIONS)))


@needs_browser
def test_patterns_and_decisions_are_the_markdown_dressed_in_place_and_lose_nothing(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": "window.LevainDashboard"}, {"eval": LOSSLESS},
        ])["values"]
    r = out[1]
    plain = re.sub(r"\{([^{}]*?) \u2014 ([^{}]*)\}", "\\1 \u00b7 \\2", r["pPlain"])   # the group head is shown without its braces, its dash a middle dot
    assert r["pText"] == plain, r                                          # every other character the markdown showed is still there
    assert r["dText"] == r["dPlain"], r
    assert r["pChips"] == ["40x", "2x", "3x", "4x"], r                      # the count moved into a leading chip
    assert [g.strip() for g in r["pGroups"]] == ["today \u00b7 2026-10-19", "carried \u00b7 not re-exercised today"], r   # stale patterns are labelled, braces dropped
    assert r["pNames"][0].strip() == "!!! absence_of_signal_rendered_as_health", r  # the severity mark survives, in the name
    assert r["pCode"] == 1, r                                              # a code span in a name stays a code span
    assert r["pStray"] == r["pStrayPlain"] and len(r["pStray"]) == 1, r    # a line that matches nothing is left exactly as rendered
    assert r["dWhen"] == ["10-18", "forwarded", "carried"], r              # the bracket's date (or its word) leads each decision
    assert "Phill" in r["dText"], r                                        # who decided stays, as muted text
    assert r["dCode"] >= 3, r
    assert len(r["dRejected"]) == 1 and r["dRejected"][0][1] == 0 and r["dRejected"][0][0] == r["dRejectedPlain"][0], r   # [rejected] is not folded into a row


@needs_browser
def test_a_long_crystal_name_keeps_its_count_beside_it(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": "window.LevainDashboard && document.querySelector('.ck-mem-row')"},
            {"eval": """(() => { const view = { crystal_index: [{ level: 4, name: 'a_stale_record_is_as_dangerous_as_it_is_trusted_and_then_some_more', one_clause: 'short clause' }, { level: 3, name: 'short', one_clause: '' }] };
  const p = window.LevainDashboard.renderPanel({ kind: 'crystals', id: 'crystals', title: 'Crystallized patterns', zone: 'mind' }, view);
  const host = document.createElement('div'); host.style.width = '230px'; document.querySelector('.ck-mem-row').parentNode.appendChild(host); host.appendChild(p);
  return [...p.querySelectorAll('.row')].map((r) => { const l = r.querySelector('.lvl').getBoundingClientRect(), n = r.querySelector('.name').getBoundingClientRect(); return [Math.round(l.top), Math.round(n.top), l.right <= n.left + 1, n.right <= r.getBoundingClientRect().right + 1]; }); })()"""},
        ])["values"]
    for lvl_top, name_top, beside, inside in out[1]:
        assert abs(lvl_top - name_top) <= 8 and beside and inside, out[1]     # the count and the name share a top edge, side by side, inside the row


@needs_browser
def test_an_editable_section_with_no_text_keeps_its_edit(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    # a real neocortex file with an EMPTY State: the section is editable, so it stays on the card as one slim row
    (root / ".levain" / "memory.continuity.md").write_text(
        "# Continuity — Sage\n\n## State\n\n## Active Threads\n- t1\n\n## Patterns\n" + PATTERNS + "\n\n## Decisions\n" + DECISIONS + "\n\n## Context\nctx\n\n## Understanding\nthe felt layer\n",
        encoding="utf-8")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": "document.querySelector('.ck-mem-neo .ck-mem-nest')"},
            {"eval": """(async () => { await new Promise(r => setTimeout(r, 400));
  const slim = document.querySelector('.ck-mem-neo .ck-mem-slim');
  if (!slim) return null;
  const mid = (n) => { const r = n.getBoundingClientRect(); return [Math.round((r.top + r.bottom) / 2), Math.round(r.left), Math.round(r.right)]; };
  const h = slim.querySelector('.phead h2'), e = slim.querySelector('.empty'), b = slim.querySelector('.edit-btn');
  // the slim row's CONTENT is one line; its box is the grid cell, stretched to sit level with the right-hand column's tall sections
  const used = Math.round(Math.max(...[h, e, b].filter(Boolean).map((n) => n.getBoundingClientRect().bottom)) - slim.getBoundingClientRect().top);
  return { title: h && h.textContent, empty: e && e.textContent, edit: !!b, h: used,
    mids: [h, e, b].map((n) => n && mid(n)), pats: document.querySelectorAll('.ck-mem-neo .ck-mem-pats .ck-mem-n').length, decs: document.querySelectorAll('.ck-mem-neo .ck-mem-decs .ck-mem-when').length }; })()"""},
        ])["values"]
    r = out[1]
    assert r and r["title"].strip().lower() == "state" and r["empty"] == "empty" and r["edit"], r      # State · empty · Edit
    ys = [m[0] for m in r["mids"]]
    assert max(ys) - min(ys) <= 12 and r["mids"][0][2] <= r["mids"][2][1] and r["h"] < 70, r          # one row, left to right
    assert r["pats"] == 4 and r["decs"] == 3, r                      # the real page's Patterns and Decisions carry their chips


@needs_browser
def test_cancelling_a_section_edit_returns_focus_to_its_edit_button(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    (root / ".levain" / "memory.continuity.md").write_text("# Continuity — Sage\n\n## State\nfocus line\n\n## Patterns\n- p1 | 1x\n", encoding="utf-8")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": "document.querySelector('.ck-mem-neo .edit-btn')"},
            {"eval": """(async () => { const b = document.querySelector('.ck-mem-neo .edit-btn'); b.click(); await new Promise(r => setTimeout(r, 200));
  const ta = document.activeElement === document.querySelector('.ck-mem-neo textarea');
  document.querySelector('.ck-mem-neo .edit-cancel').click(); await new Promise(r => setTimeout(r, 200));
  return [ta, document.activeElement === b]; })()"""},
        ])["values"]
    assert out[1] == [True, True], out[1]      # the editor took focus, and cancel hands it back to the button that opened it


@needs_browser
def test_a_long_section_never_runs_over_the_one_below_it_on_a_phone(tmp_path: Path) -> None:
    root, src, _ids = _install(tmp_path)
    many = "\n".join(f"- ! pattern_number_{i}_with_a_long_name_that_wraps_on_a_phone | {i % 4 + 1}x (2026-10-1{i % 9}) [evidence: e{i} \"some quote that runs on\"]" for i in range(16))
    (root / ".levain" / "memory.continuity.md").write_text(f"# Continuity — Sage\n\n## State\nfocus\n\n## Patterns\n{many}\n\n## Decisions\n{DECISIONS}\n", encoding="utf-8")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            PHONE, {"until": "document.querySelector('.ck-mem-neo .ck-mem-nest')"},
            {"eval": """(async () => { await new Promise(r => setTimeout(r, 500));
  const ns = [...document.querySelectorAll('.ck-mem-neo .ck-mem-nest')].map((n) => { const r = n.getBoundingClientRect(); return [n.dataset.ckPanel, r.top, r.bottom, n.scrollHeight, r.height, n.classList.contains('is-clipped') && /Read all/i.test(n.innerText)]; });
  return ns; })()"""},
        ])["values"]
    ns = out[1]
    # the probe is real: the long section is cut at its cap on purpose and says so ("Read all"), the K6 polish premise (the section is no
    # longer drawn whole past the cap)
    assert any(says for *_r, says in ns), ns
    for (_p, _t, bottom, sh, h, _s), (_p2, top2, _b2, _s2, _h2, _s3) in zip(ns, ns[1:]):
        assert sh <= h + 1 and bottom <= top2 + 1, ns                                     # no section is cut at a cap and spilled over its neighbour


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
out.dark = memCogBits({ write_path_status: "unreadable", wraps: wr(2), graph: { nodes: [1], edges: [] } });
out.darkHealth = memCogBits({ health: { status: "unreadable" }, wraps: [] });
out.noWraps = memCogBits({ health: { status: "no-wraps", write_path_live: false }, write_path_status: "no-wraps", wraps: [] });
out.formation = memCogBits({ wraps: [], graph: { nodes: [], edges: [], formation_off_at_wrap: 12 } });
out.live = memCogBits({ write_path_status: "live", wraps: wr(2) });
out.capBits = memCogBits({ wraps: wr(50) });
out.totalBits = memCogBits({ wraps: wr(50), health: { total_wraps: 212 } });
out.totalFew = memCogBits({ wraps: wr(3), health: { total_wraps: 3 } });
out.darkEither = memCogBits({ write_path_status: "live", health: { status: "unreadable" }, wraps: [] });
out.darkTop = memCogBits({ write_path_status: "unreadable", health: { status: "live" }, wraps: [] });
""", tmp_path, fn)
    assert out["capped"].startswith("50+ wraps"), out["capped"]                  # the list is capped at 50: it never claims a total of 50
    assert out["total"].startswith("212 wraps"), out["total"]                    # the authoritative figure wins when the view has it
    assert out["few"].startswith("3 wraps"), out["few"]
    assert out["lastIsMax"] is True
    assert "write path unreadable" in out["dark"] and "write path unreadable" in out["darkHealth"], out
    assert "write path unreadable" not in out["live"], out
    assert not [b for b in out["noWraps"] if "write path" in b], out                    # no wraps yet is not a fault
    assert "no new links since wrap 12" in out["formation"], out["formation"]
    assert any(b.startswith("50+") for b in out["capBits"]), out["capBits"]
    assert out["totalBits"][0] == "212 consolidations", out["totalBits"]            # the same total as the wraps line, never the capped list's length
    assert out["totalFew"][0] == "3 consolidations", out["totalFew"]
    assert "write path unreadable" in out["darkEither"], out                       # the health's own word wins over the top-level one
    assert "write path unreadable" not in out["darkTop"], out


@needs_browser
def test_crystals_survive_a_bad_index_and_show_their_detail_and_break_at_underscores(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": "window.LevainDashboard && document.querySelector('.ck-mem-row')"},
            {"eval": """(() => { const E = { kind: 'crystals', id: 'crystals', title: 'Crystallized patterns', zone: 'mind' };
  const draw = (idx) => window.LevainDashboard.renderPanel(E, { crystal_index: idx });
  const bad = [ 'oops', { length: 3 }, 7, true ].map((v) => { const p = draw(v); return [p.textContent.includes('none crystallized yet'), /unavailable|undefined/.test(p.textContent)]; });
  const p = draw([{ level: 4, name: 'a_stale_record_is_trusted', one_clause: 'short clause', permanence: 'permanent', activation_mode: 'always', tags: ['alpha', 'beta'] }]);
  const host = document.createElement('div'); document.querySelector('.ck-mem-row').parentNode.appendChild(host); host.appendChild(p);
  const m = p.querySelector('.ck-crys-meta'), nm = p.querySelector('.name');
  return { bad, meta: m && m.textContent, metaVisible: !!m && m.getClientRects().length > 0 && getComputedStyle(m).display !== 'none', title: p.querySelector('.row').title,
    wbr: nm.querySelectorAll('wbr').length, name: nm.textContent }; })()"""},
        ])["values"]
    r = out[1]
    assert r["bad"] == [[True, False]] * 4, r                         # a truthy non-array index is an empty list, not a crash or garbage rows
    assert r["meta"] == "permanent · always · #alpha #beta" and r["metaVisible"] and not r["title"], r   # detail is on the row, not a tooltip touch cannot reach
    assert r["name"] == "a_stale_record_is_trusted" and r["wbr"] == 4, r   # a break opportunity after each underscore; the name's text is unchanged


@needs_browser
def test_an_identity_row_keeps_its_degradation_note_visible(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{MEMORY}", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, {"until": "document.querySelector('.ck-mem-ident .ck-mem-ilist')"},
            {"eval": """(async () => { await new Promise(r => setTimeout(r, 300));
  const e = { kind: 'config', ref: 0, id: 'config:x', title: 'Probe row', zone: 'identity', banner: [{ tone: 'stale', text: 'stale: read 3 days ago' }] };
  const p = window.LevainDashboard.renderPanel(e, { config_docs: [{ body: 'one summary line' }] });
  // what finalizePanel does: everything after the header goes into the body
  const head = p.querySelector('.phead'), body = document.createElement('div'); body.className = 'pbody';
  while (head.nextSibling) body.appendChild(head.nextSibling);
  p.appendChild(body); p.classList.add('ck-mem-nest', 'ck-mem-rowcard');
  document.querySelector('.ck-mem-ident .ck-mem-ilist').appendChild(p);
  const n = p.querySelector('.ext-note'), s = p.querySelector('.md-p');
  const rp = p.getBoundingClientRect(), rn = n.getBoundingClientRect();
  return { shown: n.getClientRects().length > 0, inside: rn.top >= rp.top && rn.bottom <= rp.bottom + 1, below: s ? rn.top >= s.getBoundingClientRect().bottom - 1 : null, text: n.textContent }; })()"""},
        ])["values"]
    r = out[1]
    assert r["shown"] and r["inside"] and r["text"] == "stale: read 3 days ago", r      # a stale / partial / held-back row never reads as a healthy one


# ───────────────────────── L3 r1 fixes: the anneal cut must never draw "zero" as "dead" ─────────────────────────
_MOCK = r"""
Object.assign(Node.prototype, { setAttribute(k, v) { (this.attrs = this.attrs || {})[k] = String(v); }, addEventListener() {},
  replaceChildren(...ks) { this.kids = []; this.append(...ks); }, querySelectorAll() { return []; } });
Object.defineProperty(Node.prototype, "children", { get() { return this.kids; } });
Object.defineProperty(Node.prototype, "classList", { get() { return { toggle() {}, contains: () => false }; } });
const document = { createElementNS: (_ns, tag) => new Node(tag), createElement: (tag) => new Node(tag) };
const walk = (n, f) => { f(n); for (const k of n.kids || []) walk(k, f); };
const all = (n, pred) => { const r = []; walk(n, (x) => { if (pred(x)) r.push(x); }); return r; };
"""


def test_a_store_with_no_wraps_is_drawn_neutral_and_only_unreadable_is_drawn_dark(tmp_path: Path) -> None:
    from tests.test_cockpit_k2_shell import _cut, _node
    fn = _cut("  function renderHealth(entry, view) {", "  // The cognition trace")
    out = _node(_MOCK + r"""
const fmt = (n) => n, panel = () => el("div", "panel"), kv = () => {};
const badge = (h) => { const p = renderHealth({}, { health: h }); return all(p, (x) => /\bbadge\b/.test(x.className))[0]; };
const cls = (h) => badge(h).className;
out.live = cls({ status: "live" });
out.none = cls({ status: "no-wraps" });
out.unreadable = cls({ status: "unreadable" });
out.oldLive = cls({ write_path_live: true });
out.oldNone = cls({ write_path_live: false });
out.noneText = badge({ status: "no-wraps" }).textContent;
""", tmp_path, fn)
    assert out["live"] == "badge live" and out["oldLive"] == "badge live", out
    assert out["none"] == "badge neutral" and out["oldNone"] == "badge neutral", out        # no wraps yet is not a fault
    assert out["unreadable"] == "badge dark", out                                           # only a store that cannot be read is
    assert out["noneText"] == "NO WRAPS YET"
    html = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "app" / "dashboard.html").read_text(encoding="utf-8")
    assert '(st === "unreadable" ? "dark" : "neutral")' in html and ".badge.neutral" in html     # the MCP-App page draws the same three classes
    css = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard.css").read_text(encoding="utf-8")
    assert ".badge.neutral" in css


def test_the_apps_empty_graph_keeps_the_formation_note() -> None:
    html = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "app" / "dashboard.html").read_text(encoding="utf-8")
    empty = html[html.index('"not formed"') - 200: html.index('"not formed"') + 220]
    assert "formation_off_at_wrap" in empty and "no new links since wrap" in empty, empty


def test_the_cognition_trace_stops_drawing_links_after_formation_went_off(tmp_path: Path) -> None:
    from tests.test_cockpit_k2_shell import _cut, _node
    fn = _cut("  function renderGraph(entry, view) {", "  // a tier that faulted")
    out = _node(_MOCK + r"""
const num = (x) => { const n = +x; return Number.isFinite(n) ? n : 0; };
const fmt = (n) => String(n), panel = () => el("div", "panel");
const memWrapTotal = (view) => (view.health && Number.isInteger(view.health.total_wraps) ? view.health.total_wraps : null);
const wrapsOf = (links) => links.map((l) => ({ episodes_compressed: 5, graduations_validated: 1, associations_formed: l, associations_strengthened: 0 }));
const trace = (view) => {
  const p = renderGraph({}, view);
  const legend = all(p, (x) => /\bscope-legend\b/.test(x.className))[0];
  const chips = legend ? legend.kids.map((c) => c.kids.map((k) => k.textContent)) : [];
  const poly = all(p, (x) => x.tag === "polyline" && /trace-core ch3/.test(x.attrs.class || ""));
  return { chips, links: poly.map((x) => x.attrs.points.split(" ").length) };
};
// newest first: 4 post-cut zeros, then 3 historical wraps (wraps 3,2,1 formed 7,5,2 links); formation went off after wrap 3
out.cut = trace({ wraps: wrapsOf([0, 0, 0, 0, 7, 5, 2]), health: { total_wraps: 7 }, graph: { nodes: [], edges: [], formation_off_at_wrap: 3 } });
out.never = trace({ wraps: wrapsOf([0, 0, 0, 0]), health: { total_wraps: 4 }, graph: { nodes: [], edges: [], formation_off_at_wrap: 0 } });
out.old = trace({ wraps: wrapsOf([0, 0, 3]), health: { total_wraps: 3 } });
""", tmp_path, fn)
    labels = lambda r: [c[0] for c in r["chips"]]
    assert labels(out["cut"]) == ["Episodes", "Graduations", "links (formation off since wrap 3)"], out["cut"]
    assert out["cut"]["links"] == [3], out["cut"]             # one core line, the three historical points only (no post-cut zeros)
    assert out["cut"]["chips"][2][1] == "7 · max 7", out["cut"]   # the legend reads the last DRAWN point (wrap 3), not a post-cut zero
    assert labels(out["never"]) == ["Episodes", "Graduations"], out["never"]      # a store that never formed links has no such channel
    assert labels(out["old"]) == ["Episodes", "Graduations", "Links"], out["old"]  # an anneal that cannot say keeps the channel as it was
    assert out["old"]["links"] == [3]


_TRACE_PRELUDE = r"""
const num = (x) => { const n = +x; return Number.isFinite(n) ? n : 0; };
const fmt = (n) => String(n), panel = () => el("div", "panel");
const memWrapTotal = (view) => (view.health && Number.isInteger(view.health.total_wraps) ? view.health.total_wraps : null);
const wrapsOf = (links) => links.map((l) => ({ episodes_compressed: 5, graduations_validated: 1, associations_formed: l, associations_strengthened: 0 }));
const trace = (view) => {
  const p = renderGraph({}, view);
  const legend = all(p, (x) => /\bscope-legend\b/.test(x.className))[0];
  const chips = legend ? legend.kids.map((c) => c.kids.map((k) => k.textContent)) : [];
  const dots = all(p, (x) => x.tag === "circle" && /ch3/.test(x.attrs.class || ""));
  const lines = all(p, (x) => x.tag === "polyline" && /trace-core ch3/.test(x.attrs.class || ""));
  const notes = all(p, (x) => /\bnote\b/.test(x.className)).map((x) => x.textContent);
  return { chips, dots: dots.length, lines: lines.length, notes };
};
"""


def test_a_one_point_links_series_is_drawn_as_a_visible_point(tmp_path: Path) -> None:
    from tests.test_cockpit_k2_shell import _cut, _node
    fn = _cut("  function renderGraph(entry, view) {", "  // a tier that faulted")
    out = _node(_MOCK + _TRACE_PRELUDE + r"""
// formation went off after wrap 1, which formed 4 links: exactly one drawn Links point
out.one = trace({ wraps: wrapsOf([0, 0, 0, 4]), health: { total_wraps: 4 }, graph: { nodes: [], edges: [], formation_off_at_wrap: 1 } });
out.many = trace({ wraps: wrapsOf([0, 0, 7, 4]), health: { total_wraps: 4 }, graph: { nodes: [], edges: [], formation_off_at_wrap: 2 } });
""", tmp_path, fn)
    assert out["one"]["dots"] == 1 and out["one"]["lines"] == 0, out["one"]       # a polyline of one point draws nothing: a circle does
    assert out["many"]["dots"] == 0 and out["many"]["lines"] == 1, out["many"]


def test_the_links_channel_never_invents_wrap_numbers_when_the_total_is_unknown(tmp_path: Path) -> None:
    from tests.test_cockpit_k2_shell import _cut, _node
    fn = _cut("  function renderGraph(entry, view) {", "  // a tier that faulted")
    out = _node(_MOCK + _TRACE_PRELUDE + r"""
// health failed (no total) but the capped history loaded, and formation is off after wrap 60
out.nototal = trace({ wraps: wrapsOf([0, 0, 0, 5, 5, 5]), graph: { nodes: [], edges: [], formation_off_at_wrap: 60 } });
""", tmp_path, fn)
    labels = [c[0] for c in out["nototal"]["chips"]]
    assert not any(l.lower().startswith("links") for l in labels), out["nototal"]          # no channel labelled by counted-back numbers
    assert out["nototal"]["lines"] == 0 and out["nototal"]["dots"] == 0, out["nototal"]
    assert any("wrap numbers unknown" in n for n in out["nototal"]["notes"]), out["nototal"]  # and it says why


def test_a_timebase_with_no_link_samples_reads_no_samples_not_zero(tmp_path: Path) -> None:
    from tests.test_cockpit_k2_shell import _cut, _node
    fn = _cut("  function renderGraph(entry, view) {", "  // a tier that faulted")
    out = _node(_MOCK + _TRACE_PRELUDE + r"""
// 30 wraps, formation off after wrap 3: the newest 24 hold no pre-cut link sample
const links = [...Array(27).fill(0), 7, 5, 2];
const view = { wraps: wrapsOf(links), health: { total_wraps: 30 }, graph: { nodes: [], edges: [], formation_off_at_wrap: 3 } };
const p = renderGraph({}, view);
const tbs = all(p, (x) => /\bscope-tb\b/.test(x.className));
out.tbs = tbs.length;
const click = (b) => { for (const f of b.listeners || []) f(); };
Node.prototype.addEventListener = function (t, f) { (this.listeners = this.listeners || []).push(f); };
const p2 = renderGraph({}, view);
const tb2 = all(p2, (x) => /\bscope-tb\b/.test(x.className));
click(tb2[1]);   // the 24-wrap timebase
const legend = all(p2, (x) => /\bscope-legend\b/.test(x.className))[0];
out.chips = legend.kids.map((c) => c.kids.map((k) => k.textContent));
""", tmp_path, fn)
    link = [c for c in out["chips"] if c[0].lower().startswith("links")]
    assert link and link[0][1] == "no samples", out["chips"]
