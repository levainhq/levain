"""levain#181: the cockpit keyboard model. Panels are reached by Jump; Tab then moves inside the panel Jump landed on.

The Jump list listed only the board's panels (`#board .panel > .phead h2`). The glance tiles, the hero blocks (Needs, Notes, Drop,
the Machine sky, the state line), the Settings sections and the places themselves were not in it, so from the keyboard they were reachable
only by Tab through everything before them. The list is now read from the page's own blocks and places; nothing here keeps a roster of
them, so a block the page grows later is either listed or fails the live-DOM comparison.

What runs without a browser: the list built from a fake DOM (node), and the wiring in the markup. What needs one (written, run on CI):
the list against the live page, and opening, choosing and landing from the keyboard alone."""

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
const tile = withHead(node({ tag: "div", data: { ckPanel: "ops" } }), "div", "Ops");
const needs = attrNode(node({ props: { id: "ck-needs" } }), "Needs you");
const drop = attrNode(node({ props: { id: "ck-drop" } }), "Drop to the desk");
const bare = node({ tag: "div", data: { ckPanel: "state" } });            // no label at all: the panel's own id names it
const unnamed = node({ tag: "div", props: { id: "ck-skycol" } });         // nothing to name it by and no panel id: not listed
const gone = attrNode(node({ props: { id: "ck-notes" } }), "Notes from agents"); gone.closest = (s) => (s === "[hidden]" ? gone : null);
const zone = withHead(node({ props: { className: "ck-zone dev-zone" } }), "div", "Devices");
const places = ["home", "desk", "room", "memory", "settings"].map((p) => { const b = node({ tag: "button", data: { iaPlaceBtn: p } }); b.text = p[0].toUpperCase() + p.slice(1); return b; });
const lists = {};
lists[K.JUMP_BLOCKS] = [needs, tile, bare, unnamed, gone, drop, zone];
lists[K.JUMP_PLACES] = places;
globalThis.document.querySelectorAll = (sel) => lists[sel] || [];
"""


def test_the_jump_lists_every_drawn_block_and_every_place_the_page_holds(tmp_path: Path) -> None:
    out = run_node(FAKE + r"""
const items = K.jumpItems("");
out.kinds = items.map((i) => i.kind + ":" + i.label);
out.places = items.filter((i) => i.kind === "place").map((i) => i.place);
out.nodes = items.filter((i) => i.kind === "block").map((i) => i.node === needs ? "needs" : i.node === tile ? "tile" : i.node === bare ? "bare" : i.node === drop ? "drop" : i.node === zone ? "zone" : "other");
""", tmp_path)
    assert out["places"] == ["home", "desk", "room", "memory", "settings"], out        # the places are the page's own bar, in its order
    assert out["nodes"] == ["needs", "tile", "bare", "drop", "zone"], out              # a hidden block and an unnameable one are not offered
    labels = [k.split(":", 1)[1] for k in out["kinds"] if k.startswith("block:")]
    assert labels == ["Needs you", "Ops", "State", "Drop to the desk", "Devices"], out  # named by aria-label, a heading, the panel id


def test_a_search_keeps_every_matching_panel_and_block_and_trims_only_rows(tmp_path: Path) -> None:
    out = run_node(FAKE + r"""
out.empty = K.jumpItems("").length;
out.some = K.jumpItems("o").map((i) => i.label);
""", tmp_path)
    assert out["empty"] == 10, out                                                       # five places and five blocks: the empty list is never cut to a fixed length
    assert "Ops" in out["some"] and "Needs you" in out["some"] and "Home" in out["some"], out


def test_a_place_is_matched_by_its_name(tmp_path: Path) -> None:
    out = run_node(FAKE + r"""out.hits = K.jumpItems("mem").filter((i) => i.kind === "place").map((i) => i.place);""", tmp_path)
    assert out["hits"] == ["memory"], out


def test_the_machine_block_carries_a_name_the_jump_can_list() -> None:
    html = (WEB / "dashboard.html").read_text()
    m = re.search(r'<div class="skycol frame" id="ck-skycol"([^>]*)>', html)
    assert m and 'aria-label="Machine"' in m.group(1), m


def test_no_cockpit_script_gives_a_stop_a_positive_tabindex() -> None:
    """Tab order stays the document's: a positive tabindex would put one stop ahead of every panel, and Jump's landing would not be where Tab continues."""
    for name in ("cockpit_view.js", "dashboard_core.js", "dashboard_places.js", "devices_panel.js", "agents_view.js", "topology_view.js"):
        src = (WEB / name).read_text()
        bad = re.findall(r"""(?:tabIndex\s*=\s*[1-9]|setAttribute\(\s*["']tabindex["']\s*,\s*["']?[1-9])""", src)
        assert not bad, (name, bad)


def test_jump_landing_does_not_take_a_tab_stop_from_a_panel_that_has_one() -> None:
    """An openable glance tile is a Tab stop (tabindex 0): landing on it must not rewrite that to -1."""
    src = (WEB / "cockpit_view.js").read_text()
    i = src.index("function landOnPanel")
    body = src[i:i + 900]
    assert 'hasAttribute("tabindex")' in body, body


# ── the live page (browser; written here, run on CI) ──
# every block the jump should offer, found from the page by the same rule a person would use: the page's panels and named blocks that are
# not hidden, minus the panels no place draws. Compared as node identity with what the jump box lists.
LIVE = """(() => {
  const K = window.LevainCockpitKinds;
  const items = K.jumpItems('');
  const listed = items.filter((i) => i.kind !== 'place').map((i) => i.node);
  const candidates = [...document.querySelectorAll(K.JUMP_BLOCKS + ', #board .panel[data-ck-panel]')].filter((n) => !n.closest('[hidden]') && !['notifications', 'topology'].includes(n.dataset.ckPanel));
  const bar = [...document.querySelectorAll('.ia-places [data-ia-place-btn]')].map((b) => b.dataset.iaPlaceBtn);
  return JSON.stringify({ probe: 'live', places: items.filter((i) => i.kind === 'place').map((i) => i.place), bar,
    missing: candidates.filter((n) => !listed.includes(n)).map((n) => n.id || n.dataset.ckPanel || n.className),
    extra: listed.filter((n) => !candidates.includes(n)).map((n) => n.id || n.dataset.ckPanel || n.className), n: candidates.length });
})()"""
CTRL_K = "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'k', ctrlKey: true, bubbles: true, cancelable: true})); 1"
TYPE = "(() => { const i = document.querySelector('.palette-input'); i.value = %s; i.dispatchEvent(new Event('input')); return 1; })()"
KEYS = "document.querySelector('.palette-input').dispatchEvent(new KeyboardEvent('keydown', {key: %s, bubbles: true, cancelable: true})); 1"
# where focus is, relative to the block the first line of the list named
WHERE = """(() => { const a = document.activeElement, id = window.__want, K = window.LevainCockpitKinds;
  const n = K.jumpItems('').map((i) => i.node).find((x) => x && (x.id === id || (x.dataset && x.dataset.ckPanel === id)));
  return JSON.stringify({ probe: 'where', want: id, found: !!n, inside: !!n && n.contains(a), tag: a ? a.tagName : null, shown: !!n && n.getClientRects().length > 0 }); })()"""


@needs_browser
def test_the_jump_list_is_the_live_pages_blocks_and_places(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE, {"eval": LIVE}])
    (got,) = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"probe":"live"')]
    assert got["n"] > 0, got                                                           # the page drew blocks (the comparison proved something)
    assert not got["missing"] and not got["extra"], got                                # the list is the page's blocks, no more, no fewer
    assert got["places"] == got["bar"], got                                            # and every place the bar has


@needs_browser
def test_jump_opens_and_chooses_from_the_keyboard_alone_and_focus_lands_in_the_block(tmp_path: Path) -> None:
    """Ctrl-K, type the label of the first glance tile, Enter: focus is inside the chosen block (no pointer anywhere)."""
    PICK = ("(() => { const K = window.LevainCockpitKinds; const it = K.jumpItems('').find((i) => i.kind === 'block' && i.node.closest && i.node.closest('#ck-widgets'));"
            " window.__want = it.node.dataset.ckPanel; window.__pickLabel = it.label; return JSON.stringify({probe: 'pick', label: it.label}); })()")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE,
            {"eval": PICK},
            {"eval": CTRL_K}, SETTLE, {"eval": "JSON.stringify({probe: 'open', open: !!document.querySelector('.palette-input'), focused: document.activeElement === document.querySelector('.palette-input')})"},
            {"eval": TYPE % "window.__pickLabel"}, SETTLE,
            {"eval": KEYS % json.dumps("Enter")}, SETTLE, SETTLE, {"eval": WHERE},
        ])
    probes = {json.loads(v)["probe"]: json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"probe"')}
    assert probes["open"] == {"probe": "open", "open": True, "focused": True}, probes       # Ctrl-K opens it with the input focused
    where = probes["where"]
    assert where["found"] and where["inside"] and where["shown"] and where["tag"] != "BODY", where

