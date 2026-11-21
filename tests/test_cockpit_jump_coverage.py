"""levain#181: the cockpit keyboard model. Panels are reached by Jump; Tab then moves inside the panel Jump landed on.

The Jump list listed only the board's panels. The glance tiles, the hero blocks, the Needs, Notes and Drop blocks, the Settings sections
and the places themselves were not in it, so from the keyboard they were reachable only by Tab through everything before them. The
list is now read from the live page by a named set of block selectors plus the places bar; a new kind of block needs its selector added
(the live-page test below enumerates the page's blocks by its own structural query, so a block the selectors miss fails it).

What runs without a browser: the list, the cap, the name of a block, and the whole behaviour of the box (its ARIA wiring, keys,
focus on leaving, landing and not landing) against a fake DOM in node. What needs a browser (written, run on CI): the list against the
live page, and opening, choosing and landing from the keyboard alone."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests._ke_dom import run_node
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_places import READY, SETTLE

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"

# a fake page: the blocks the jump box reads, by the selector the page exports (K.JUMP_BLOCKS) and the places bar by its own selector
FAKE = r"""
const node = (o) => Object.assign(new N(o.tag || "section"), o.props || {}, { dataset: o.data || {} });
const withHead = (n, tag, text) => { n.querySelector = () => Object.assign(new N(tag), { text }); return n; };
const attrNode = (n, label) => { n.getAttribute = (k) => (k === "aria-label" ? label : null); return n; };
// a glance tile that is openable carries an instruction as its aria-label; its visible title is the heading
const tile = attrNode(withHead(node({ tag: "div", data: { ckPanel: "ops" } }), "div", "Ops"), "Open Ops in a focus view");
const plain = withHead(node({ tag: "div", data: { ckPanel: "mem" } }), "div", "Mem");
const needs = attrNode(node({ props: { id: "ck-needs" } }), "Needs you");
const drop = attrNode(node({ props: { id: "ck-drop" } }), "Drop to the desk");
const bare = node({ tag: "div", data: { ckPanel: "state" } });            // no label at all: the panel's own id names it
const unnamed = node({ tag: "div", props: { id: "ck-skycol" } });         // nothing to name it by and no panel id: not listed
const gone = attrNode(node({ props: { id: "ck-notes" } }), "Notes from agents"); gone.closest = (s) => (s === "[hidden]" ? gone : null);
const zone = withHead(node({ props: { className: "ck-zone dev-zone" } }), "div", "Devices");
const places = ["home", "desk", "room", "memory", "settings"].map((p) => { const b = node({ tag: "button", data: { iaPlaceBtn: p } }); b.text = p[0].toUpperCase() + p.slice(1); return b; });
const lists = {};
lists[K.JUMP_BLOCKS] = [needs, tile, plain, bare, unnamed, gone, drop, zone];
lists[K.JUMP_PLACES] = places;
globalThis.document.querySelectorAll = (sel) => lists[sel] || [];
"""


def test_the_jump_lists_every_drawn_block_and_every_place_the_page_holds(tmp_path: Path) -> None:
    out = run_node(FAKE + r"""
const items = K.jumpItems("");
out.kinds = items.map((i) => i.kind + ":" + i.label);
out.places = items.filter((i) => i.kind === "place").map((i) => i.place);
out.nodes = items.filter((i) => i.kind === "block").map((i) => i.node === needs ? "needs" : i.node === tile ? "tile" : i.node === plain ? "plain" : i.node === bare ? "bare" : i.node === drop ? "drop" : i.node === zone ? "zone" : "other");
""", tmp_path)
    assert out["places"] == ["home", "desk", "room", "memory", "settings"], out        # the places are the page's own bar, in its order
    assert out["nodes"] == ["needs", "tile", "plain", "bare", "drop", "zone"], out     # a hidden block and an unnameable one are not offered
    labels = [k.split(":", 1)[1] for k in out["kinds"] if k.startswith("block:")]
    # a glance tile is named by its visible title whether or not it is openable; a block that is not a panel by its own label, then its heading, then the panel id
    assert labels == ["Needs you", "Ops", "Mem", "State", "Drop to the desk", "Devices"], out


def test_a_place_is_matched_by_its_name(tmp_path: Path) -> None:
    out = run_node(FAKE + r"""out.hits = K.jumpItems("mem").filter((i) => i.kind === "place").map((i) => i.place);""", tmp_path)
    assert out["hits"] == ["memory"], out


# a panel searched in the store, as the page holds one (fill makes its state); the jump reads it for rows and for the "Search ... for" line
STORE_PANEL = r"""
const crystals = new N("div"); crystals._conn = true;
K.fill(crystals, { pid: "crystals", kind: "list", typed: true, title: "Crystals", lines: [{ text: "alpha one" }, { text: "alpha two" }, { text: "alpha three" }],
  hays: ["alpha one", "alpha two", "alpha three"], search: { on: true, scope: "store" } }, { entry: { title: "Crystals" } });
const blocks = (n) => Array.from({ length: n }, (_, i) => { const b = attrNode(node({ props: { id: "ck-b" + i } }), "Alpha block " + i); b._conn = true; return b; });
const kinds = (q) => K.jumpItems(q).map((i) => i.kind);
const count = (ks, k) => ks.filter((x) => x === k).length;
"""

CONNECTED = r"""
Object.defineProperty(N.prototype, "isConnected", { configurable: true, get() { return this._conn !== undefined ? this._conn : (this === document.body || !!(this.parent && this.parent.isConnected)); } });
"""


def test_the_cap_cuts_rows_and_never_the_store_line(tmp_path: Path) -> None:
    out = run_node(FAKE + CONNECTED + STORE_PANEL + r"""
lists[K.JUMP_BLOCKS] = blocks(16);                         // more places, panels and blocks than the cap holds
out.many = kinds("alpha");
lists[K.JUMP_BLOCKS] = blocks(12);                         // room for a few rows only
out.some = kinds("alpha");
lists[K.JUMP_BLOCKS] = blocks(2);                          // room for every row
out.few = kinds("alpha");
""", tmp_path)
    many, some, few = out["many"], out["some"], out["few"]
    assert many.count("block") == 16 and many.count("row") == 0 and many.count("store") == 1, many    # every block kept, no room for a row, the store line kept
    assert some.count("block") == 12 and some.count("row") == 2 and some.count("store") == 1, some    # the rows take what room is left
    assert few.count("block") == 2 and few.count("row") == 3 and few.count("store") == 1, few


# the box in a fake page: nodes know their parent (so they are in the page or not), can take focus, and pass events up to their parents
BOX = CONNECTED + r"""
N.prototype.appendChild = function (k) { k.parent = this; this.kids.push(k); return k; };
N.prototype.replaceChildren = function (...ks) { for (const k of this.kids) k.parent = null; this.kids = []; this.append(...ks); };
N.prototype.contains = function (n) { for (let x = n; x; x = x.parent) if (x === this) return true; return false; };
N.prototype.remove = function () {
  const had = document.activeElement && this.contains(document.activeElement);
  if (this.parent) this.parent.kids = this.parent.kids.filter((k) => k !== this);
  this.parent = null;
  if (had) document.activeElement = document.body;
};
N.prototype.addEventListener = function (t, f) { ((this.h = this.h || {})[t] = this.h[t] || []).push(f); };
N.prototype.fire = function (t, ev) {
  ev = Object.assign({ target: this, preventDefault() { this.defaultPrevented = true; } }, ev);
  for (let n = this; n; n = n.parent) for (const f of (n.h && n.h[t]) || []) f(ev);
  return ev;
};
N.prototype.focus = function () { if (this.isConnected) document.activeElement = this; };
N.prototype.scrollIntoView = function (o) { (this.scrolled = this.scrolled || []).push(o); };
N.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
N.prototype.hasAttribute = function (k) { return k in this.attrs; };
N.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
N.prototype.getClientRects = function () { return this.shown === false ? [] : [1]; };
let modalUp = false;
const mainRegion = new N("main"); mainRegion._conn = true;
document.querySelector = (s) => (s === ".modal-overlay" && modalUp ? new N("div") : s === "main" ? mainRegion : null);
const frames = [];
globalThis.requestAnimationFrame = (f) => { frames.push(f); };
const flush = (n) => { for (let i = 0; i < (n || 40) && frames.length; i++) frames.shift()(); };
const goCalls = [];
globalThis.LevainPlaces = { go: (p) => { goCalls.push(p); document.body.dataset.iaPlace = p; } };
document.body.dataset.iaPlace = "home";
const jumpbtn = mount("jumpbtn"); jumpbtn._conn = true;
const block = (id, label, o) => { const b = withHead(node({ tag: "div", data: { ckPanel: id } }), "div", label); b._conn = true; if (o && o.hidden) b.shown = false; if (o && o.tabindex) b.attrs.tabindex = o.tabindex; return b; };
const key = (k, extra) => Object.assign({ key: k }, extra || {});
const boxOf = () => document.body.kids.find((k) => k.className === "palette") || null;
const openBox = () => { K.pageKey(Object.assign(key("k", { ctrlKey: true }), { target: document.body, preventDefault() {} })); return boxOf(); };
const partsOf = (dlg) => { const all = dlg.all(() => true); return { input: all.find((n) => n.tag === "input"), list: all.find((n) => n.tag === "ul"), status: all.find((n) => n.attrs.role === "status"),
  none: all.find((n) => (" " + n.className + " ").indexOf(" pl-none ") >= 0), box: dlg.kids[0], opts: all.filter((n) => n.attrs.role === "option") }; };
const typeIn = (p, text) => { p.input.value = text; p.input.fire("input", {}); };
"""


def test_the_boxs_aria_wiring_names_its_parts_and_the_active_option_is_always_in_the_page(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const dlg = openBox(); let p = partsOf(dlg);
out.roles = { dlg: dlg.attrs.role, input: p.input.attrs.role, list: p.list.attrs.role, status: p.status.attrs.role };
out.input = { controls: p.input.attrs["aria-controls"], listId: p.list.id, auto: p.input.attrs["aria-autocomplete"], label: p.input.attrs["aria-label"], placeholder: p.input.placeholder };
out.listLabel = p.list.attrs["aria-label"]; out.live = p.status.attrs["aria-live"];
out.noneInList = p.list.all((n) => (" " + n.className + " ").indexOf(" pl-none ") >= 0).length;
out.notOptions = p.list.kids.filter((k) => k.attrs.role !== "option").length;
const resolves = () => { const id = p.input.attrs["aria-activedescendant"]; return !!id && dlg.all((n) => n.id === id).length === 1; };
out.firstOk = resolves();
const before = p.list.kids.slice();
p.input.fire("keydown", key("ArrowDown")); p.input.fire("keydown", key("ArrowDown"));
out.sameNodes = p.list.kids.length === before.length && p.list.kids.every((k, i) => k === before[i]);                // moved in place, not rebuilt
out.selected = p.opts.map((o) => o.attrs["aria-selected"]).join(",").slice(0, 20);
out.activeIsSelected = (() => { const id = p.input.attrs["aria-activedescendant"]; const o = p.opts.find((x) => x.id === id); return !!o && o.attrs["aria-selected"] === "true"; })();
out.scrolled = p.opts[2].scrolled || [];
out.untouched = (p.opts[5].scrolled || []).length;
out.count = p.status.textContent;
out.listed = p.opts.length;
typeIn(p, "ops");
p = partsOf(dlg);
out.afterType = { ok: resolves(), status: p.status.textContent, opts: p.opts.length };
typeIn(p, "zzzznothing");
p = partsOf(dlg);
out.empty = { status: p.status.textContent, active: p.input.attrs["aria-activedescendant"] || null, opts: p.opts.length, noneShown: p.none.hidden === false, noneHidden: p.none.attrs["aria-hidden"], expanded: p.input.attrs["aria-expanded"] };
""", tmp_path)
    assert out["roles"] == {"dlg": "dialog", "input": "combobox", "list": "listbox", "status": "status"}, out
    assert out["input"]["controls"] == out["input"]["listId"] and out["input"]["auto"] == "list" and out["input"]["label"], out
    assert "place" in out["input"]["placeholder"], out                                                        # the placeholder names a place: the list holds places too
    assert out["listLabel"] and out["live"] == "polite", out                                                  # the listbox has a name; the count is announced politely
    assert out["noneInList"] == 0 and out["notOptions"] == 0, out                                             # nothing but options inside the listbox
    assert out["firstOk"] and out["sameNodes"] and out["activeIsSelected"], out
    assert out["selected"].startswith("false,false,true"), out
    assert out["scrolled"] == [{"block": "nearest"}], out                                                    # the highlighted row, and only it, is brought into view
    assert out["untouched"] == 0, out
    assert re.fullmatch(r"\d+ results", out["count"]) and out["count"] == f"{out['listed']} results", out
    assert out["afterType"]["ok"] and out["afterType"]["opts"] >= 1 and re.fullmatch(r"\d+ results?", out["afterType"]["status"]), out
    assert out["empty"] == {"status": "nothing matches", "active": None, "opts": 0, "noneShown": True, "noneHidden": "true", "expanded": "false"}, out


def test_leaving_the_box_gives_focus_back_and_never_to_the_body(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const where = (n) => n === document.body ? "body" : n === jumpbtn ? "jumpbtn" : n === mainRegion ? "main" : n === opener ? "opener" : "other";
const opener = new N("input"); opener._conn = true;
const run = (setup, via) => { document.activeElement = opener; const dlg = openBox(); setup(); const p = partsOf(dlg); (via === "box" ? p.box : p.input).fire("keydown", key("Escape")); return { where: where(document.activeElement), open: !!boxOf() }; };
out.kept = run(() => {});                                                   // the opener is still on the page
out.redrawn = run(() => { opener._conn = false; });                         // a redraw replaced it while the box was open
opener._conn = true;
out.noButton = (() => { jumpbtn._conn = false; const r = run(() => { opener._conn = false; }); jumpbtn._conn = true; return Object.assign(r, { tabindex: mainRegion.attrs.tabindex }); })();
opener._conn = true;
out.fromBox = run(() => { opener._conn = false; }, "box");                   // Escape from anywhere inside the box, not only the field
opener._conn = true;
out.again = (() => { document.activeElement = opener; openBox(); K.pageKey(Object.assign(key("k", { metaKey: true }), { target: document.body, preventDefault() {} })); return { where: where(document.activeElement), open: !!boxOf() }; })();
""", tmp_path)
    assert out["kept"] == {"where": "opener", "open": False}, out
    assert out["redrawn"] == {"where": "jumpbtn", "open": False}, out
    assert out["noButton"] == {"where": "main", "open": False, "tabindex": "-1"}, out
    assert out["fromBox"] == {"where": "jumpbtn", "open": False}, out
    assert out["again"] == {"where": "opener", "open": False}, out                                            # a second Cmd-K toggles it shut the same way


def test_ctrl_k_does_nothing_while_a_dialog_is_open(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const press = () => { const ev = Object.assign(key("k", { ctrlKey: true }), { target: document.body, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }); K.pageKey(ev); return ev.defaultPrevented; };
modalUp = true;
out.underModal = { took: press(), open: !!boxOf() };
modalUp = false;
out.free = { took: press(), open: !!boxOf() };
""", tmp_path)
    assert out["underModal"] == {"took": False, "open": False}, out
    assert out["free"] == {"took": True, "open": True}, out


def test_choosing_a_block_lands_focus_in_it_and_keeps_a_tab_stop_it_already_has(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const stop = block("ops", "Ops", { tabindex: "0" }), other = block("mem", "Mem");
lists[K.JUMP_BLOCKS] = [stop, other];
const choose = (text) => { const dlg = openBox(); const p = partsOf(dlg); typeIn(p, text); p.input.fire("keydown", key("Enter")); flush(); return !!boxOf(); };
out.a = { open: choose("ops"), focus: document.activeElement === stop, tabindex: stop.attrs.tabindex };
out.b = { open: choose("mem"), focus: document.activeElement === other, tabindex: other.attrs.tabindex };
out.go = goCalls.slice();
""", tmp_path)
    assert out["a"] == {"open": False, "focus": True, "tabindex": "0"}, out         # an openable tile stays a Tab stop
    assert out["b"] == {"open": False, "focus": True, "tabindex": "-1"}, out        # any other block takes focus without joining the Tab order
    assert out["go"] == [], out                                                     # already on Home: no place switch


def test_a_block_that_is_not_drawn_is_announced_and_the_box_stays_open(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const ghost = block("ghost", "Ghost", { hidden: true });
lists[K.JUMP_BLOCKS] = [ghost];
const attempt = (place) => { goCalls.length = 0; document.body.dataset.iaPlace = place; const dlg = openBox(); const p = partsOf(dlg); typeIn(p, "ghost"); p.input.fire("keydown", key("Enter")); flush();
  const r = { open: !!boxOf(), status: p.status.textContent, focusInBox: dlg.contains(document.activeElement), go: goCalls.slice() }; p.input.fire("keydown", key("Escape")); return r; };
out.home = attempt("home");           // already Home: nothing to switch to
out.memory = attempt("memory");       // another place: Home is tried once
""", tmp_path)
    assert out["home"] == {"open": True, "status": "Ghost is not shown here", "focusInBox": True, "go": []}, out
    assert out["memory"] == {"open": True, "status": "Ghost is not shown here", "focusInBox": True, "go": ["home"]}, out


def test_an_input_method_composition_is_not_a_key_of_the_box(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const dlg = openBox(); const p = partsOf(dlg);
p.input.fire("keydown", key("Enter", { isComposing: true })); p.input.fire("keydown", key("ArrowDown", { isComposing: true })); p.input.fire("keydown", key("Escape", { isComposing: true }));
out.during = { open: !!boxOf(), active: p.input.attrs["aria-activedescendant"] };
p.input.fire("keydown", key("ArrowDown"));
out.after = p.input.attrs["aria-activedescendant"];
""", tmp_path)
    assert out["during"] == {"open": True, "active": "palette-opt-0"}, out
    assert out["after"] == "palette-opt-1", out


def test_tab_and_escape_work_from_the_boxs_own_surface_not_only_the_field(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const dlg = openBox(); const p = partsOf(dlg);
const tab = p.box.fire("keydown", key("Tab"));
out.moved = p.input.attrs["aria-activedescendant"]; out.held = tab.defaultPrevented;
p.box.fire("keydown", key("Escape"));
out.open = !!boxOf();
""", tmp_path)
    assert out == {"moved": "palette-opt-1", "held": True, "open": False}, out


def test_the_masthead_button_and_the_machine_block_carry_what_a_reader_needs() -> None:
    html = (WEB / "dashboard.html").read_text()
    m = re.search(r'<div class="skycol frame" id="ck-skycol"([^>]*)>', html)
    assert m and 'aria-label="Machine"' in m.group(1), m
    b = re.search(r'<button id="jumpbtn"([^>]*)>', html)
    assert b and re.search(r'aria-keyshortcuts="[^"]*Control\+K[^"]*Meta\+K', b.group(1)), b


# ── the live page (browser; written here, run on CI) ──
# What the page holds, found by a structural query of the test's own: every node that carries a panel id, every section inside <main> that holds
# no panel (a section of settings, not a zone of board panels, which the panels themselves stand for) and every labelled section (Needs, Notes,
# Drop). None of it reads the selector list the box lists from, so a block the page has and the box misses is `missing`. Panels no place draws
# are left out.
LIVE = """(() => {
  const K = window.LevainCockpitKinds;
  const items = K.jumpItems('');
  const listed = items.filter((i) => i.kind !== 'place').map((i) => i.node);
  const there = [...document.querySelectorAll('[data-ck-panel], main section:not(:has([data-ck-panel])), section[aria-label]')]
    .filter((n) => !n.closest('[hidden]') && !['notifications', 'topology'].includes(n.dataset.ckPanel));
  const bar = [...document.querySelectorAll('.ia-places [data-ia-place-btn]')].map((b) => b.dataset.iaPlaceBtn);
  const name = (n) => n.id || n.dataset.ckPanel || n.className;
  return JSON.stringify({ probe: 'live', places: items.filter((i) => i.kind === 'place').map((i) => i.place), bar, n: there.length,
    missing: there.filter((n) => !listed.includes(n)).map(name),
    stale: listed.filter((n) => !n.isConnected || n.closest('[hidden]')).map(name) });
})()"""
CTRL_K = "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'k', ctrlKey: true, bubbles: true, cancelable: true})); 1"
TYPE = "(() => { const i = document.querySelector('.palette-input'); i.value = %s; i.dispatchEvent(new Event('input')); return 1; })()"
KEYS = "document.querySelector('.palette-input').dispatchEvent(new KeyboardEvent('keydown', {key: %s, bubbles: true, cancelable: true})); 1"
# the glance tile whose title no other panel, block or place of the list also matches: typed alone it is the first line
UNIQUE_TILE = ("(() => { const K = window.LevainCockpitKinds;"
               " const tiles = [...document.querySelectorAll('#ck-widgets > [data-ck-panel]')].filter((t) => t.querySelector('.k'));"
               " const t = tiles.find((x) => K.jumpItems(x.querySelector('.k').textContent.trim()).filter((i) => i.kind !== 'row' && i.kind !== 'store').length === 1);"
               " if (!t) return JSON.stringify({probe: 'pick', found: false, tiles: tiles.map((x) => x.querySelector('.k').textContent)});"
               " window.__want = t.dataset.ckPanel; window.__title = t.querySelector('.k').textContent.trim();"
               " const mine = K.jumpItems('').find((i) => i.node === t);"
               " return JSON.stringify({probe: 'pick', found: true, title: window.__title, listedAs: mine ? mine.label : null}); })()")
# where focus is, relative to the tile as the page holds it now (a redraw replaces the tile's node, so it is found again by its panel id)
TILE_NOW = "document.querySelector('#ck-widgets > [data-ck-panel=\"' + window.__want + '\"]')"
WHERE = ("(() => { const a = document.activeElement, n = " + TILE_NOW + ";"
         " return JSON.stringify({ probe: 'where', inside: !!n && n.contains(a), tag: a ? a.tagName : null, shown: !!n && n.getClientRects().length > 0, open: !!document.querySelector('.palette') }); })()")


@needs_browser
def test_the_jump_list_covers_every_block_the_page_holds_and_every_place(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY,
                                                          {"until": "document.querySelector('#ck-widgets > [data-ck-panel]') && 1", "ms": 30000}, SETTLE, {"eval": LIVE}])
    (got,) = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"probe":"live"')]
    assert got["n"] > 0, got                                                           # the page drew blocks (the comparison proved something)
    assert not got["missing"], got                                                     # every block the page holds is in the list
    assert not got["stale"], got                                                       # and nothing listed is gone or hidden
    assert got["places"] == got["bar"], got                                            # and every place the bar has


@needs_browser
def test_jump_opens_and_chooses_from_the_keyboard_alone_and_focus_lands_in_the_block(tmp_path: Path) -> None:
    """Ctrl-K, type the title of a glance tile that no other line matches, Enter: focus is inside that tile (no pointer anywhere)."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY,
            {"until": "document.querySelector('#ck-widgets > [data-ck-panel]') && 1", "ms": 30000}, SETTLE,
            {"eval": UNIQUE_TILE},
            {"eval": CTRL_K}, {"until": "document.querySelector('.palette-input') && 1", "ms": 10000},
            {"eval": "JSON.stringify({probe: 'open', open: !!document.querySelector('.palette-input'), focused: document.activeElement === document.querySelector('.palette-input')})"},
            {"eval": TYPE % "window.__title"},
            {"eval": KEYS % json.dumps("Enter")},
            {"until": "!document.querySelector('.palette') && 1", "ms": 10000}, SETTLE,
            {"eval": WHERE},
        ])
    probes = {json.loads(v)["probe"]: json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"probe"')}
    assert probes["pick"]["found"], probes                                                   # a tile with a title of its own exists on the page
    assert probes["pick"]["listedAs"] == probes["pick"]["title"], probes                      # the list names it by its visible title
    assert probes["open"] == {"probe": "open", "open": True, "focused": True}, probes         # Ctrl-K opens it with the input focused
    where = probes["where"]
    assert where["inside"] and where["shown"] and where["tag"] != "BODY" and not where["open"], where
