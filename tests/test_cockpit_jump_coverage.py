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
// a block whose headings answer by selector (a descendant may carry a heading the block's own title is not): map = { selector: text }
const withSel = (n, map) => { n.querySelector = (sel) => (map[sel] ? Object.assign(new N("div"), { text: map[sel] }) : null); return n; };
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
// the page's own listeners on document: an event fired on a node in the page reaches them after its ancestors
const docL = {};
document.addEventListener = (t, f) => { (docL[t] = docL[t] || []).push(f); };
document.removeEventListener = (t, f) => { docL[t] = (docL[t] || []).filter((g) => g !== f); };
const docFire = (t, ev) => { ev = Object.assign({ target: document.body, preventDefault() { this.defaultPrevented = true; } }, ev); for (const f of (docL[t] || []).slice()) f(ev); return ev; };
N.prototype.fire = function (t, ev) {
  ev = Object.assign({ target: this, preventDefault() { this.defaultPrevented = true; } }, ev);
  const onDoc = (docL[t] || []).slice();   // a listener added while the event is on its way is not called for it
  let top = this;
  for (let n = this; n; n = n.parent) { top = n; for (const f of (n.h && n.h[t]) || []) f(ev); }
  if (top === document.body) for (const f of onDoc) f(ev);
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
const qs = {};   // what document.querySelector answers for a selector a test wants to resolve
document.querySelector = (s) => (s === ".modal-overlay" && modalUp ? new N("div") : s === "main" ? mainRegion : qs[s] || null);
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


def test_a_key_aimed_at_a_control_outside_the_box_is_not_the_boxs(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const dlg = openBox(); const p = partsOf(dlg);
const outside = node({ tag: "input" }); document.body.appendChild(outside);
const esc = outside.fire("keydown", key("Escape")); const down = outside.fire("keydown", key("ArrowDown"));
out.outsideOpen = !!boxOf(); out.outsidePrevented = !!((esc && esc.defaultPrevented) || (down && down.defaultPrevented));
p.box.fire("keydown", key("Escape"));
out.insideClosed = !boxOf();
""", tmp_path)
    assert out == {"outsideOpen": True, "outsidePrevented": False, "insideClosed": True}, out


def test_a_press_that_drops_focus_to_the_body_inside_the_box_gives_it_back_to_the_field_while_the_box_is_open(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const dlg = openBox(); const p = partsOf(dlg);
// the refocus is a microtask: it runs after the driver's own synchronous print, so the answer is printed again at the end of the chain
// (the runner reads the last line)
const tick = () => Promise.resolve().then(() => Promise.resolve());
document.activeElement = document.body;
p.box.fire("focusout", { relatedTarget: null });
tick().then(() => {
  out.refocused = document.activeElement === p.input;
  p.box.fire("keydown", key("Escape"));
  document.activeElement = document.body;
  p.box.fire("focusout", { relatedTarget: null });
  return tick();
}).then(() => { out.afterClose = document.activeElement === p.input; console.log(JSON.stringify(out)); });
""", tmp_path)
    assert out == {"refocused": True, "afterClose": False}, out


# a hand-off waits on the page's own observer and a timer: both are held by the test, and a redraw is delivered to the observers whose
# target the changed parent is (or, for a subtree observer, sits under)
HANDOFF = r"""
const observers = [];
globalThis.MutationObserver = class { constructor(cb) { this.cb = cb; this.target = null; } observe(t, o) { this.target = t; this.opts = o; observers.push(this); } disconnect() { this.target = null; } };
const redrawn = (parent) => { for (const m of observers.slice()) if (m.target && (m.target === parent || (m.opts.subtree && m.target.contains(parent)))) m.cb([]); };
const timers = [];
globalThis.setTimeout = (f) => { timers.push(f); return timers.length; };
globalThis.clearTimeout = () => {};
const watching = () => observers.filter((m) => m.target).length;
const pageListeners = () => (docL.pointerdown || []).length + (docL.keydown || []).length;
"""


def test_a_hero_panel_under_a_nested_slot_hands_focus_on_when_a_redraw_replaces_it(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + HANDOFF + r"""
const hero = new N("section"); hero._conn = true; qs["#ck-hero"] = hero;
const slot = new N("div"); hero.appendChild(slot);                      // the panel is not a child of the hero: a band slot holds it
const inHero = (id, label) => { const b = block(id, label); b.closest = (s) => (s === "#ck-hero" ? hero : null); return b; };
let panel = inHero("state", "State"); slot.appendChild(panel);
lists[K.JUMP_BLOCKS] = [panel]; lists["#ck-hero [data-ck-panel]"] = [panel];
const p = partsOf(openBox()); typeIn(p, "state"); p.input.fire("keydown", key("Enter")); flush();
out.landed = document.activeElement === panel;
panel._conn = false; slot.kids = [];                                  // a stream redraw replaces the node under the slot; focus falls to the body
document.activeElement = document.body;
const next = inHero("state", "State"); slot.appendChild(next); lists["#ck-hero [data-ck-panel]"] = [next];
redrawn(slot);
out.handedOn = document.activeElement === next;
""", tmp_path)
    assert out == {"landed": True, "handedOn": True}, out


def test_a_second_choice_or_a_new_query_ends_the_first_choices_landing(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const a = block("alpha", "Alpha", { hidden: true }), b = block("beta", "Beta", { hidden: true });
lists[K.JUMP_BLOCKS] = [a, b];
const pick = (p, text) => { typeIn(p, text); p.input.fire("keydown", key("Enter")); };
// two choices before either block is drawn: the page then draws the first one; the second is the one the reader meant
let dlg = openBox(); let p = partsOf(dlg);
pick(p, "alpha"); pick(p, "beta");
a.shown = true; flush();
out.second = { open: !!boxOf(), onFirst: document.activeElement === a, status: p.status.textContent };
p.input.fire("keydown", key("Escape")); a.shown = false;
// a new query while a choice is still looking for its block: the choice is dropped, the count stays and the box stays open
dlg = openBox(); p = partsOf(dlg);
pick(p, "alpha"); typeIn(p, "zzzz");
a.shown = true; flush();
out.typed = { open: !!boxOf(), onBlock: document.activeElement === a, status: p.status.textContent, focusInput: document.activeElement === p.input };
""", tmp_path)
    assert out["second"] == {"open": True, "onFirst": False, "status": "Beta is not shown here"}, out
    assert out["typed"] == {"open": True, "onBlock": False, "status": "nothing matches", "focusInput": True}, out


def test_a_section_is_named_by_its_own_title_and_a_panel_by_its_heading(tmp_path: Path) -> None:
    out = run_node(FAKE + r"""
const devices = withSel(node({ props: { className: "ck-zone dev-zone" } }), { ".zone-title": "Devices", ".phead h2": "Paired devices" });
const presence = withSel(node({ props: { className: "ck-zone dev-zone pr-settings" } }), { ".zone-title": "Presence & notification settings", ".phead h2": "Tap tokens" });
const ops = withSel(node({ tag: "div", data: { ckPanel: "ops" } }), { ".phead h2": "Ops", ".zone-title": "Elsewhere" });
lists[K.JUMP_BLOCKS] = [devices, presence, ops];
out.labels = K.jumpItems("").filter((i) => i.kind === "block").map((i) => i.label);
out.found = K.jumpItems("devices").filter((i) => i.kind === "block").map((i) => i.label);
""", tmp_path)
    assert out["labels"] == ["Devices", "Presence & notification settings", "Ops"], out     # a section by its own title, a panel by its heading
    assert out["found"] == ["Devices"], out


def test_the_boxs_keys_work_wherever_focus_is_and_the_backdrop_does_not_move_focus(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const before = (docL.keydown || []).length;
const opener = new N("input"); opener._conn = true; document.activeElement = opener;
const dlg = openBox(); const p = partsOf(dlg);
document.activeElement = document.body;                                  // a click on the box's padding, scrollbar or "nothing matches" row left the field
const tab = docFire("keydown", key("Tab"));
out.moved = p.input.attrs["aria-activedescendant"]; out.held = tab.defaultPrevented;
docFire("keydown", key("ArrowDown"));
out.down = p.input.attrs["aria-activedescendant"];
docFire("keydown", key("Escape"));
out.closed = !boxOf(); out.back = document.activeElement === opener; out.listenerGone = (docL.keydown || []).length === before;
// the backdrop: pressing on it closes the box, and the press's own default (a move of focus to the body) is cancelled
document.activeElement = opener;
const dlg2 = boxOf() || openBox();
const ev = dlg2.fire("mousedown", { target: dlg2 });
out.backdrop = { closed: !boxOf(), prevented: !!ev.defaultPrevented, back: document.activeElement === opener };
""", tmp_path)
    assert out["moved"] == "palette-opt-1" and out["held"] and out["down"] == "palette-opt-2", out
    assert out["closed"] and out["back"] and out["listenerGone"], out
    assert out["backdrop"] == {"closed": True, "prevented": True, "back": True}, out


def test_a_place_the_page_cannot_go_to_is_said_in_the_box_and_the_box_stays_open(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const attempt = (setup) => { setup(); const dlg = openBox(); const p = partsOf(dlg); typeIn(p, "memory"); p.input.fire("keydown", key("Enter")); flush();
  const r = { open: !!boxOf(), status: p.status.textContent, onInput: document.activeElement === p.input }; if (boxOf()) p.input.fire("keydown", key("Escape")); return r; };
const realPlaces = globalThis.LevainPlaces;
out.missing = attempt(() => { globalThis.LevainPlaces = undefined; });
out.throws = attempt(() => { globalThis.LevainPlaces = { go: () => { throw new Error("no"); } }; });
out.works = attempt(() => { globalThis.LevainPlaces = realPlaces; });
""", tmp_path)
    assert out["missing"] == {"open": True, "status": "Memory cannot be opened here", "onInput": True}, out
    assert out["throws"] == {"open": True, "status": "Memory cannot be opened here", "onInput": True}, out
    assert out["works"]["open"] is False, out


def test_a_block_that_cannot_take_focus_ends_its_hand_off(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + HANDOFF + r"""
const stuck = block("stuck", "Stuck"); stuck.focus = () => {}; qs["#board"] = new N("div");   // an inert ancestor: focus does not move
lists[K.JUMP_BLOCKS] = [stuck];
const p = partsOf(openBox()); const base = pageListeners();             // the box's own key listener is not what is counted
typeIn(p, "stuck"); p.input.fire("keydown", key("Enter")); flush();
out.said = p.status.textContent; out.open = !!boxOf();
out.watching = watching(); out.listeners = pageListeners() - base;       // no observer, no listener left to take focus back from the field
""", tmp_path)
    assert out == {"said": "Stuck is not shown here", "open": True, "watching": 0, "listeners": 0}, out


def test_a_block_with_no_panel_id_is_found_again_after_a_redraw(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const mk = () => { const z = withSel(node({ props: { className: "ck-zone dev-zone" } }), { ".zone-title": "Devices" }); z.dataset.zone = "devices"; z._conn = true; z.closest = (s) => (s === ".dev-zone, .ck-thiscockpit" ? z : null); return z; };
const old = mk(); lists[K.JUMP_BLOCKS] = [old];
const p = partsOf(openBox()); typeIn(p, "devices");
old._conn = false;                                                        // Settings drew its sections again while the box was open
const fresh = mk(); qs['[data-zone="devices"]'] = fresh;
p.input.fire("keydown", key("Enter")); flush();
out.landed = document.activeElement === fresh; out.open = !!boxOf();
""", tmp_path)
    assert out == {"landed": True, "open": False}, out


def test_without_animation_frames_a_place_is_still_given_time_to_paint(tmp_path: Path) -> None:
    out = run_node(FAKE + BOX + r"""
const timed = [];
globalThis.requestAnimationFrame = undefined;
globalThis.setTimeout = (f) => { timed.push(f); return timed.length; };
const late = block("late", "Late", { hidden: true });
lists[K.JUMP_BLOCKS] = [late];
document.body.dataset.iaPlace = "memory";
const p = partsOf(openBox()); typeIn(p, "late"); p.input.fire("keydown", key("Enter"));
out.early = { open: !!boxOf(), status: p.status.textContent };            // the place has not painted yet: nothing is said about the block
late.shown = true;
for (let i = 0; i < 40 && timed.length; i++) timed.shift()();
out.late = { open: !!boxOf(), focus: document.activeElement === late };
""", tmp_path)
    assert out["early"]["open"] is True and "not shown" not in out["early"]["status"], out
    assert out["late"] == {"open": False, "focus": True}, out


def test_the_masthead_button_and_the_machine_block_carry_what_a_reader_needs() -> None:
    html = (WEB / "dashboard.html").read_text()
    m = re.search(r'<div class="skycol frame" id="ck-skycol"([^>]*)>', html)
    assert m and 'aria-label="Machine"' in m.group(1), m
    b = re.search(r'<button id="jumpbtn"([^>]*)>', html)
    assert b and re.search(r'aria-keyshortcuts="[^"]*Control\+K[^"]*Meta\+K', b.group(1)), b


# ── the live page (browser; written here, run on CI) ──
# What the page holds, found by a structural query of the test's own. A block is: a node that carries a panel id; a top-level section of
# <main> that holds no panel and has a title of its own (a section of settings; a zone of board panels is stood for by its panels);
# a labelled section that is a direct child of the page column (Needs, Notes, Drop); the hero's labelled group (the Machine). Not a block:
# a section nested inside one of those, a section with no title, and anything hidden. None of it reads the selector list the box lists
# from, so a block the page has and the box misses is `missing`. Panels no place draws are left out.
LIVE = """(() => {
  const K = window.LevainCockpitKinds;
  const items = K.jumpItems('');
  const listed = items.filter((i) => i.kind !== 'place').map((i) => i.node);
  const column = document.getElementById('ck-hero').parentElement;
  const there = [...document.querySelectorAll('[data-ck-panel]'),
                 ...document.querySelectorAll('main > section:not(:has([data-ck-panel])):has(.zone-title)'),
                 ...column.querySelectorAll(':scope > section[aria-label]'),
                 ...document.querySelectorAll('#ck-hero > [role="group"][aria-label]')]
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
