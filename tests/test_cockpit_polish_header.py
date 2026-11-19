"""levain#141 polish, the header lane (1119-10): what Phill typed after using layout A, held where a test can hold it.

Held: the entity named once in the masthead (the brand side's wordmark; #entity stays in the markup, undrawn), the jar out of the masthead
and drawn into #ck-jarbox when the page has one, the masthead controls on one line, the now line on one baseline, Drop to the desk's
inner padding, Home's order with the At a glance block between the Desk and Work (and the document order of those blocks the same, so
Tab walks the drawn order), the phone's header row with Sync and Jump beside Drop, Grants and Notifications (44 px targets on touch),
the sticky masthead (below the dialogs, never over the drop box a jump lands on), Work's rows at tablet width, and 44 px icon buttons on
touch at 1101-1500.

NOT held (a person judges them): whether the decrowded bar breathes, whether the now line reads as one line, the weight of a 150 px
sticky bar on a phone. The head shoots those at 390/820/1180/1440/1920."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests._ke_dom import NODE, WEB, run_node  # noqa: F401
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_narrow_layout_rig import drive_touch
from tests.test_cockpit_places import PANELS, READY, SETTLE, WORK_ROW

HTML = (WEB / "dashboard.html").read_text(encoding="utf-8")
CSS = (WEB / "dashboard.css").read_text(encoding="utf-8")
CORE = (WEB / "dashboard_core.js").read_text(encoding="utf-8")
HEADER = "/* ── levain#141 polish: header, now line, drop, Home order ── */"


def _vals(out: dict) -> list[dict]:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{")]


def _open(tmp_path: Path, steps: list, touch: bool = False) -> dict:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        run = drive_touch if touch else _drive
        return run(tmp_path, f"{url}/#write_token=T", steps)


# ---------------------------------------------------------------------------------------------------------------- static

def test_the_masthead_has_no_jar_and_keeps_the_entity_element() -> None:
    """The jar is not in the masthead markup; #entity is (the page's writer and the rename affordance find it), and the CSS names it undrawn."""
    assert 'id="jar"' not in HTML and 'class="jar"' not in HTML
    assert 'id="entity"' in HTML
    assert re.search(r"body\.ia \.masthead \.unit \{ display: none; \}", CSS)


def test_the_notes_come_before_the_drop_in_the_document() -> None:
    """Home draws needs, notes, the Desk head, the drop: the document holds the same order (Tab follows the document)."""
    assert HTML.index('id="ck-needs"') < HTML.index('id="ck-notes"') < HTML.index('id="ck-drop"') < HTML.index('id="ck-widgets"') < HTML.index("<main")


def test_the_polish_css_is_one_commented_block_at_the_end() -> None:
    assert CSS.count(HEADER) == 1
    assert CSS.index(HEADER) > CSS.index("body.ia[data-ia-place=\"memory\"] #ck-drop { display: none !important; }")


def test_the_masthead_sits_below_every_dialog() -> None:
    """z-index: the masthead under the jump palette (20), the presence sheet (40) and the modal overlay (50), above the page's own sticky bars (5)."""
    z = int(re.search(r"body\.ia \.deck > \.masthead \{[^}]*z-index: (\d+)", CSS).group(1))
    palette = int(re.search(r"\.palette \{[^}]*z-index: (\d+)", CSS).group(1))
    sheet = int(re.search(r"\.pr-sheet \{[^}]*z-index: (\d+)", CSS).group(1))
    modal = int(re.search(r"position: fixed; inset: 0; z-index: (\d+); display: flex; align-items: center;", CSS).group(1))
    assert 5 < z < palette and z < sheet and z < modal, (z, palette, sheet, modal)


def test_the_jar_is_drawn_into_the_stats_box_when_there_is_one_and_nowhere_else(tmp_path: Path) -> None:
    """renderJar writes into #ck-jarbox; with no such element it returns and draws nothing, and nothing throws."""
    start = CORE.index("  function renderJar(jar) {")
    fn = CORE[start:CORE.index("  // The masthead state line:", start)]
    drv = tmp_path / "jar.js"
    drv.write_text(r"""
class Node { constructor(t) { this.tag = t; this.kids = []; this.attrs = {}; this.cls = new Set(); this.className = ""; this.hidden = false; this.style = {}; }
  append(...k) { this.kids.push(...k); } appendChild(k) { this.kids.push(k); return k; } replaceChildren() { this.kids = []; }
  setAttribute(k, v) { this.attrs[k] = String(v); } addEventListener() {} remove() {}
  get classList() { const s = this.cls; return { add: (c) => s.add(c), toggle: (c, on) => (on ? s.add(c) : s.delete(c)), contains: (c) => s.has(c) }; }
  set textContent(t) { this.text = String(t); } get textContent() { return (this.text || "") + this.kids.map((k) => k.textContent).join(""); } }
const el = (tag, cls, text) => { const n = new Node(tag); n.className = cls; if (text != null) n.textContent = text; return n; };
let box = null, jarLastToday = null, jarLastDay = null;
globalThis.document = { getElementById: (id) => (id === "ck-jarbox" ? box : id === "jar" ? (() => { throw new Error("the masthead jar was read"); })() : null),
  createElementNS: (ns, t) => new Node(t) };
""" + fn + r"""
const out = {};
const jar = { status: "ok", label: "38 today, typical 282", level: 0.4, today: 38, day: "2026-11-19" };
renderJar(jar);                                   // no box: draws nothing, throws nothing
box = new Node("div");
renderJar(jar);
out.drawn = [box.kids.map((k) => k.tag), box.kids[1] && box.kids[1].textContent, box.cls.has("jar"), box.hidden];
renderJar(null);
out.hidden = [box.hidden, box.kids.length];
console.log(JSON.stringify(out));
""", encoding="utf-8")
    import subprocess
    r = subprocess.run([NODE, str(drv)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["drawn"] == [["svg", "span"], "38 today, typical 282", True, False], out
    assert out["hidden"] == [True, 0], out


# ---------------------------------------------------------------------------------------------------------------- browser

ONE = r"""(() => { const R = (e) => e.getBoundingClientRect(), mid = (e) => Math.round((R(e).top + R(e).bottom) / 2);
  const unit = document.querySelector('.masthead .unit'), ent = document.getElementById('entity');
  const ctl = [...document.querySelectorAll('.masthead .ia-act, .masthead #refresh, .masthead #jumpbtn, .masthead .themesw')].filter(e => R(e).width > 0);
  const mids = ctl.map(mid);
  return JSON.stringify({ unitShown: unit.getClientRects().length > 0, entityText: ent.textContent, word: !!document.querySelector('.masthead .wordmark').getClientRects().length,
    jarInMasthead: !!document.querySelector('.masthead #jar, .masthead .jar'), spread: Math.max(...mids) - Math.min(...mids), n: ctl.length, sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }); })()"""


@needs_browser
def test_the_masthead_names_the_entity_once_holds_no_jar_and_keeps_its_controls_on_one_line(tmp_path: Path) -> None:
    steps: list = []
    for w in (1101, 1180, 1440, 1920):
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 1}}, SETTLE, {"eval": ONE}]
    out = _open(tmp_path, [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE] + steps)
    rows = _vals(out)
    assert len(rows) == 4, out["values"]
    for g, w in zip(rows, (1101, 1180, 1440, 1920)):
        assert g["unitShown"] is False and g["word"] is True and g["jarInMasthead"] is False, (w, g)
        assert g["n"] >= 4 and g["spread"] <= 3, (w, g)                     # the controls are centred on one line
        assert g["sw"] <= g["cw"], (w, g)


PHONE_ROW = r"""(() => { const b = document.getElementById('presencebtn'); b.hidden = false; b.querySelector('.pr-chip-t').textContent = 'here';
  return new Promise(r => setTimeout(r, 300)).then(() => { const R = (s) => document.querySelector(s).getBoundingClientRect();
    const rows = ['.ia-act[data-phone-jump=drop]', '.ia-act[data-phone-jump=grants]', '.ia-act[data-phone-jump=notifications]', '#refresh', '#jumpbtn'].map((s) => R(s));
    return JSON.stringify({ tops: rows.map((r) => Math.round(r.top)), heights: rows.map((r) => Math.round(r.height)), widths: rows.map((r) => Math.round(r.width)),
      chipTop: Math.round(R('#presencebtn').top), rowBottom: Math.round(Math.max(...rows.map((r) => r.bottom))), sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }); }); })()"""


@needs_browser
def test_on_a_phone_sync_and_jump_share_the_row_of_drop_grants_and_notifications(tmp_path: Path) -> None:
    """With the presence chip drawn (it takes a row of its own) at 390, 600 and 720, the five buttons are one row, the chip is below it,
    and every one is a 44 px target on touch."""
    steps: list = []
    for w in (390, 600, 720):
        steps += [{"emulate": {"w": w, "h": 844, "dpr": 2, "mobile": True}}, SETTLE, {"eval": PHONE_ROW}]
    out = _open(tmp_path, [{"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, {"touch": True}, READY, SETTLE] + steps, touch=True)
    rows = _vals(out)
    assert len(rows) == 3, out["values"]
    for g, w in zip(rows, (390, 600, 720)):
        assert max(g["tops"]) - min(g["tops"]) <= 2, (w, g)                  # one row
        assert g["chipTop"] >= min(g["tops"]), (w, g)                         # the chip is never above them (at 390 and 600 it is a row below; at 720 it fits beside)
        assert min(g["heights"]) >= 44 and min(g["widths"]) >= 44, (w, g)
        assert g["sw"] <= g["cw"], (w, g)


COARSE = r"""(() => { const hit = [...document.querySelectorAll('.masthead .indicators button, .masthead .themesw button, .masthead .ia-act')]
  .filter((b) => b.getClientRects().length > 0).map((b) => { const r = b.getBoundingClientRect(); return [b.id || b.getAttribute('aria-label') || b.className, Math.round(r.width), Math.round(r.height)]; });
  return JSON.stringify({ coarse: matchMedia('(pointer: coarse)').matches, w: innerWidth, small: hit.filter((h) => h[1] < 44 || h[2] < 44), n: hit.length }); })()"""


@needs_browser
def test_icon_only_header_buttons_are_44_px_on_touch_from_1101_to_1500(tmp_path: Path) -> None:
    steps: list = []
    for w in (1101, 1280, 1500):
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 2, "mobile": True}}, SETTLE, {"eval": COARSE}]
    out = _open(tmp_path, [{"emulate": {"w": 1280, "h": 900, "dpr": 2, "mobile": True}}, {"touch": True}, READY, SETTLE] + steps, touch=True)
    rows = _vals(out)
    assert len(rows) == 3, out["values"]
    for g in rows:
        assert g["coarse"] is True and g["n"] >= 8, g
        assert g["small"] == [], g


NOW_LINE = r"""(() => { const hero = document.getElementById('ck-hero'); hero.hidden = false;
  document.getElementById('ck-when').innerHTML = '<div class="clockbox"><span class="clock">8:02 am</span><span class="date">Thursday, November 19</span></div>';
  const slot = document.getElementById('ck-state-slot'); slot.innerHTML = '<p class="stateline"><span class="ftext">Helping Som get ready for work</span></p>';
  const base = (e) => { const p = document.createElement('span'); p.style.cssText = 'display:inline-block;width:0;height:0'; e.insertBefore(p, e.firstChild); const y = p.getBoundingClientRect().bottom; p.remove(); return y; };
  return new Promise(r => setTimeout(r, 300)).then(() => { const c = document.querySelector('#ck-band .clock'), s = document.querySelector('#ck-band .stateline .ftext');
    return JSON.stringify({ clock: Math.round(base(c) * 10) / 10, state: Math.round(base(s) * 10) / 10, sameRow: Math.abs(c.getBoundingClientRect().top - s.getBoundingClientRect().top) < 40 }); }); })()"""


@needs_browser
def test_the_clock_and_the_state_line_share_one_baseline_where_they_share_a_row(tmp_path: Path) -> None:
    steps: list = []
    for w in (1101, 1440, 1920):
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 1}}, SETTLE, {"eval": NOW_LINE}]
    out = _open(tmp_path, [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE] + steps)
    rows = _vals(out)
    assert len(rows) == 3, out["values"]
    for g, w in zip(rows, (1101, 1440, 1920)):
        assert g["sameRow"] is True, (w, g)
        assert abs(g["clock"] - g["state"]) <= 1.5, (w, rows)


DROP = r"""(() => { const d = document.getElementById('ck-drop'), R = (e) => e.getBoundingClientRect(), box = R(d);
  const head = d.querySelector('.needs-headline h3, .needs-headline'), words = document.getElementById('ck-drop-words'), rec = d.querySelector('.drop-receipt');
  return JSON.stringify({ headGap: Math.round(R(head).left - box.left), wordsGap: Math.round(R(words).left - box.left), wordsRight: Math.round(box.right - R(words).right),
    receiptPad: rec ? parseFloat(getComputedStyle(rec).paddingLeft) : null }); })()"""


@needs_browser
def test_drop_to_the_desk_keeps_its_inner_padding_on_home(tmp_path: Path) -> None:
    steps: list = []
    for w in (1440, 390):
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 1}}, SETTLE, {"eval": DROP}]
    out = _open(tmp_path, [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, {"until": "document.getElementById('ck-drop-words') && 1", "ms": 30000}, SETTLE] + steps)
    wide, narrow = _vals(out)
    assert wide["headGap"] >= 16 and wide["wordsGap"] >= 16 and wide["wordsRight"] >= 16, wide
    assert narrow["headGap"] >= 12 and narrow["wordsGap"] >= 12, narrow
    assert narrow["receiptPad"] is not None and narrow["receiptPad"] >= 4, narrow      # its messages carry a left padding on a phone


ORDER = r"""(() => { const q = (s) => document.querySelector(s);
  const blocks = ['#ck-needs', '#ck-notes', '.ia-head[data-ia-head="desk"]', '#ck-drop', '.ia-head[data-ia-head="glance"]', '#ck-widgets', '.ia-head[data-ia-head="work"]', '.ia-head[data-ia-head="machine"]']
    .map((s) => { const n = q(s); return { s, n, order: n ? Number(getComputedStyle(n).order) : null }; });
  const w = q('#ck-widgets'); w.hidden = false; if (!w.firstElementChild) { const t = document.createElement('div'); t.textContent = 'tile'; w.appendChild(t); }
  const dom = blocks.filter((b) => b.n).slice().sort((a, b) => (a.n.compareDocumentPosition(b.n) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1)).map((b) => b.s);
  const drawn = blocks.filter((b) => b.n).slice().sort((a, b) => a.order - b.order).map((b) => b.s);
  const top = (s) => Math.round(q(s).getBoundingClientRect().top + scrollY);
  return new Promise(r => setTimeout(r, 300)).then(() => JSON.stringify({ dom, drawn, orders: blocks.map((b) => [b.s, b.order]),
    ys: ['.ia-head[data-ia-head="desk"]', '#ck-drop', '.ia-head[data-ia-head="glance"]', '#ck-widgets', '.ia-head[data-ia-head="work"]', '.ia-head[data-ia-head="machine"]'].map(top) })); })()"""


@needs_browser
def test_home_draws_at_a_glance_between_the_desk_and_work_and_the_document_agrees(tmp_path: Path) -> None:
    """The drawn order (the heads and blocks by `order`, and their vertical positions) and the document order of the same blocks are one list
    at three widths, so Tab walks it top to bottom. The Desk panel (inside the re-rendered board) and the hero's graph toggle are outside
    this list: see the return's residue."""
    steps: list = []
    for w in (390, 820, 1440):
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 1}}, SETTLE, {"eval": ORDER}]
    out = _open(tmp_path, [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, PANELS, SETTLE] + steps)
    rows = _vals(out)
    assert len(rows) == 3, out["values"]
    want = ["#ck-needs", "#ck-notes", '.ia-head[data-ia-head="desk"]', "#ck-drop", '.ia-head[data-ia-head="glance"]', "#ck-widgets",
            '.ia-head[data-ia-head="work"]', '.ia-head[data-ia-head="machine"]']
    for g, w in zip(rows, (390, 820, 1440)):
        assert g["dom"] == want and g["drawn"] == want, (w, g)
        assert g["ys"] == sorted(g["ys"]), (w, g)


STICKY = r"""(() => { window.scrollTo(0, 700); return new Promise(r => setTimeout(r, 400)).then(() => { const m = document.querySelector('.masthead'), r = m.getBoundingClientRect(), cs = getComputedStyle(m);
  return JSON.stringify({ scrollY: Math.round(scrollY), top: Math.round(r.top), h: Math.round(r.height), bg: cs.backgroundColor, z: cs.zIndex, vis: r.width > 300 }); }); })()"""
JUMP_DROP = r"""(() => { window.scrollTo(0, 2500); document.querySelector('.ia-act[data-phone-jump=drop]').click();
  return new Promise(r => setTimeout(r, 900)).then(() => JSON.stringify({ dropTop: Math.round(document.getElementById('ck-drop').getBoundingClientRect().top),
    mast: Math.round(document.querySelector('.masthead').getBoundingClientRect().bottom) })); })()"""


@needs_browser
def test_the_masthead_stays_at_the_top_while_the_page_scrolls_and_a_jump_lands_below_it(tmp_path: Path) -> None:
    steps: list = []
    for w in (390, 820, 1440):
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 1}}, SETTLE, {"eval": STICKY}, {"eval": JUMP_DROP}]
    out = _open(tmp_path, [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, PANELS, SETTLE] + steps)
    rows = _vals(out)
    assert len(rows) == 6, out["values"]
    for i, w in enumerate((390, 820, 1440)):
        st, jump = rows[2 * i], rows[2 * i + 1]
        assert st["scrollY"] > 100 and st["top"] == 0 and st["vis"], (w, st)        # scrolled, and the bar is still at the top
        assert st["bg"] not in ("rgba(0, 0, 0, 0)", "transparent"), (w, st)          # with the page's background under it
        assert jump["dropTop"] >= jump["mast"] - 1, (w, jump)                       # the drop box is not under the bar after a jump to it


@needs_browser
def test_work_is_never_an_orphan_row_at_tablet_width(tmp_path: Path) -> None:
    """721-1100: the List takes the row; Board and Inbox sit side by side under it (before: three cards at two-up, Inbox alone)."""
    steps: list = []
    for w in (740, 820, 1000, 1100):
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 1}}, SETTLE, {"eval": WORK_ROW}]
    out = _open(tmp_path, [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, PANELS, SETTLE] + steps)
    rows = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"head"' in v]
    assert len(rows) == 4, out["values"]
    for g, w in zip(rows, (740, 820, 1000, 1100)):
        assert g["tray"]["w"] >= g["board"]["w"] * 1.8, (w, g)                       # the List is the full row
        assert g["board"]["y"] == g["inbox"]["y"] and g["board"]["y"] >= g["tray"]["y"] + g["tray"]["h"], (w, g)
        assert abs(g["board"]["w"] - g["inbox"]["w"]) <= 2, (w, g)
