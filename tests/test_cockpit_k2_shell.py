"""K2 r2: the page shell. The tab tables, the deep links, the three-state theme, the band's wrap line, the empty-provider row, the
count pill and the light tokens. Run in node against the page's own source (cockpit_view.js loaded as in _ke_dom; the shell functions
of dashboard_core.js cut out by name), and over the CSS text for the token claims. No browser; fixtures are synthetic.

Each case names the finding it was written from (k-reviews/K2_L1L2.md): L1-1, L1-2, L1-3, L1-5, L1-7, L1-9, L1-10, L1-11, L2-13."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests._ke_dom import NODE, WEB, run_node

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# the ids flow's own page draws (flow/scripts/flow_cockpit.py renders this template): every one must land on a tab
FLOW_IDS = ["sky", "weather", "brief", "ops", "memory", "inbox", "stream", "roster", "fleet", "decisions", "flow-episodes", "flow-spores"]
TABS = {"morning", "board", "build", "memory"}


def test_tab_tables_default_prototype_keys_and_every_flow_id(tmp_path: Path) -> None:
    out = run_node(r"""
const tabs = (e) => K.modesOf(e).split(" ");
out.unknown = K.modesOf({ id: "nothing-names-this" });
out.protoIds = ["constructor", "__proto__", "toString", "hasOwnProperty", "valueOf"].map((id) => K.modesOf({ id }));
out.protoZones = ["constructor", "__proto__", "toString"].map((zone) => K.modesOf({ id: "x", zone }));
out.flow = Object.fromEntries(%s.map((id) => [id, tabs({ id })]));
out.flowWithRegion = Object.fromEntries(%s.map((id) => [id, tabs({ id, region: "widgets" })]));
out.health = tabs({ id: "health" });
out.mind = tabs({ id: "x", zone: "mind" });
out.identity = tabs({ id: "x", zone: "identity" });
out.tableProto = [Object.getPrototypeOf(K.TABS_OF_ID), Object.getPrototypeOf(K.TABS_OF_REGION)];
""" % (str(FLOW_IDS).replace("'", '"'), str(FLOW_IDS).replace("'", '"')), tmp_path)
    assert out["unknown"] == "board"                                         # the default tab is Board
    assert out["protoIds"] == ["board"] * 5, out["protoIds"]                  # an inherited function never reads as a tab list
    assert out["protoZones"] == ["board"] * 3, out["protoZones"]
    assert out["tableProto"] == [None, None]
    for id_, tabs in {**out["flow"], **out["flowWithRegion"]}.items():
        assert tabs and set(tabs) <= TABS, (id_, tabs)
    assert out["health"] == ["memory"] and out["mind"] == ["memory"] and out["identity"] == ["memory"]   # the Memory tab holds the mind


def test_the_memory_tab_is_the_fourth_and_key_4_reaches_it() -> None:
    src = (WEB / "cockpit_view.js").read_text(encoding="utf-8")
    assert re.search(r'const MODES = \["morning", "board", "build", "memory"\];', src)
    assert '"1234".indexOf(ev.key)' in src and 'MODES["1234".indexOf(ev.key)]' in src
    html = (WEB / "dashboard.html").read_text(encoding="utf-8")
    assert 'data-mode-tab="memory"' in html and "<kbd>4</kbd>Memory" in html


def test_deep_links_choose_the_tab_and_an_unknown_one_chooses_nothing(tmp_path: Path) -> None:
    out = run_node(r"""
const at = (hash) => { globalThis.location = { hash, search: "", pathname: "/" }; return K.hashMode(); };
out.tabs = ["#morning", "#board", "#build", "#memory"].map(at);
out.unknown = [at("#bogus"), at(""), at("#MEMORY"), at("#memory2")];
out.beside = [at("#write_token=abc&build"), at("#memory&write_token=abc")];
// a hash names the tab the page opens on: the one the reader chose in this browser tab is only the fallback
globalThis.location = { hash: "#build", search: "", pathname: "/" };
globalThis.sessionStorage = { s: { levain_cockpit_mode: "memory" }, getItem(k) { return this.s[k] || null; }, setItem(k, v) { this.s[k] = v; }, removeItem(k) { delete this.s[k]; } };
const calls = [];
globalThis.history = { replaceState: (a, b, url) => calls.push(url) };
K.applyMode("memory", true);                       // a reader's choice rewrites the address, keeping other fragment parts
globalThis.location = { hash: "#write_token=abc&build", search: "?x=1", pathname: "/" };
K.applyMode("board", true);
K.applyMode("memory");                             // the first paint never writes it
out.written = calls;
out.mode = document.body.dataset.mode;
""", tmp_path)
    assert out["tabs"] == ["morning", "board", "build", "memory"]
    assert out["unknown"] == ["", "", "", ""]
    assert out["beside"] == ["build", "memory"]
    assert out["written"] == ["/#memory", "/?x=1#write_token=abc&board"], out["written"]
    assert out["mode"] == "memory"


def test_the_theme_has_three_states_and_a_way_back_to_the_system(tmp_path: Path) -> None:
    out = run_node(r"""
const store = {};
globalThis.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; }, removeItem: (k) => { delete store[k]; } };
document.documentElement = { dataset: {} };
const seen = [K.chosenTheme()];                    // nothing chosen: the system's
K.setTheme("dark", true); seen.push(K.chosenTheme(), store.levain_cockpit_theme || null);
K.setTheme("light", true); seen.push(K.chosenTheme(), store.levain_cockpit_theme || null);
K.setTheme("system", true); seen.push(K.chosenTheme(), store.levain_cockpit_theme || null, "theme" in document.documentElement.dataset);
K.setTheme("nonsense", true); seen.push(K.chosenTheme());
out.seen = seen;
""", tmp_path)
    assert out["seen"] == ["system", "dark", "dark", "light", "light", "system", None, False, "system"]
    html = (WEB / "dashboard.html").read_text(encoding="utf-8")
    assert all(f'data-theme-set="{t}"' in html for t in ("system", "dark", "light"))
    # the stored choice is read as the file loads (the <head> script), before the body paints
    assert '<script src="/cockpit_view.js"></script>' in html.split("</head>")[0]
    assert "function earlyTheme()" in (WEB / "cockpit_view.js").read_text(encoding="utf-8")


def test_a_jump_to_a_row_on_the_memory_tab_switches_to_memory(tmp_path: Path) -> None:
    out = run_node(r"""
document.body.dataset.mode = "board";
const crystals = new N("div"); crystals.dataset.modes = "memory";
K.revealTab(crystals);
out.afterMemoryPanel = document.body.dataset.mode;
const tray = new N("div"); tray.dataset.modes = "morning board";
K.revealTab(tray);                                  // from Memory, which it is not on: Board
out.afterBoardPanel = document.body.dataset.mode;
document.body.dataset.mode = "build";
K.revealTab(tray);                                  // a tab that does not show it: Board, which it prefers to the first it names
out.fromBuild = document.body.dataset.mode;
const chrome = new N("div"); K.revealTab(chrome);   // no tab list: page chrome, on every tab
out.chrome = document.body.dataset.mode;
""", tmp_path)
    assert out["afterMemoryPanel"] == "memory"
    assert out["afterBoardPanel"] == "board"         # Memory shows neither tab it names: Board, the one it prefers
    assert out["fromBuild"] == "board" and out["chrome"] == "board"


CORE = (WEB / "dashboard_core.js").read_text(encoding="utf-8")


def _cut(start: str, end: str) -> str:
    return CORE[CORE.index(start):CORE.index(end)]


_PRELUDE = r"""
class Node { constructor(tag) { this.tag = tag; this.kids = []; this.className = ""; this.dataset = {}; this.text = ""; this.hidden = false; }
  append(...ks) { for (const k of ks) this.kids.push(typeof k === "string" ? Object.assign(new Node("#t"), { text: k }) : k); }
  appendChild(k) { this.kids.push(k); return k; }
  set textContent(t) { this.text = String(t); this.kids = []; }
  get textContent() { return this.text + this.kids.map((k) => k.textContent).join(""); } }
const el = (tag, cls, text) => { const n = new Node(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
"""


def _node(body: str, tmp_path: Path, *cuts: str) -> dict:
    import json
    import subprocess

    drv = tmp_path / "shell.js"
    drv.write_text(_PRELUDE + "\n".join(cuts) + "\nconst out = {};\n" + body + "\nconsole.log(JSON.stringify(out));\n", encoding="utf-8")
    r = subprocess.run([NODE, str(drv)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_wrap_line_for_null_a_bad_string_and_a_good_one(tmp_path: Path) -> None:
    fn = _cut("  function wrapLineOf(view) {", "  let heroEpoch = 0;")
    out = _node(r"""
const lines = (view) => { const b = wrapLineOf(view); return b.kids.map((k) => k.textContent); };
out.none = lines({});                                                     // no history at all: never blank
out.noneNamed = lines({ entity_name: "Sage", jar: { today: 2 } });
out.nulls = lines({ wraps: null, entity_name: "Sage" });
out.badStamp = lines({ wraps: [{ wrapped_at: "not a date" }], entity_name: "Sage" });
out.notString = lines({ wraps: [{ wrapped_at: 12345 }], entity_name: "Sage" });   // a number is not a stamp
out.emptyRow = lines({ wraps: [null], entity_name: "Sage" });
out.good = lines({ wraps: [{ wrapped_at: "2026-10-19T06:02:00" }, { wrapped_at: "2026-10-18T06:00:00" }] });
out.total = lines({ wraps: [{ wrapped_at: "2026-10-19T06:02:00" }], health: { total_wraps: 41 } });
out.one = lines({ wraps: [{ wrapped_at: "2026-10-19T06:02:00" }], health: { total_wraps: 1 } });
""", tmp_path, fn)
    assert out["none"] == ["No wraps yet", "a fresh install"]
    assert out["noneNamed"] == ["No wraps yet", "Sage · 2 today"]
    for k in ("nulls", "badStamp", "notString", "emptyRow"):
        assert out[k] == ["No wraps yet", "Sage"], (k, out[k])
    assert out["good"][0].startswith("Last wrap ") and out["good"][0].endswith("06:02")     # 24-hour, never "06:02 AM" or "24:02"
    assert out["good"][1] == "2 wraps held"                                                # the page holds a list, not the total
    assert out["total"][1] == "41 wraps" and out["one"][1] == "1 wrap"                     # the health panel's own total, when sent


def test_an_empty_provider_is_found_by_its_sentinel_key_never_by_its_text(tmp_path: Path) -> None:
    fn = _cut("  function emptyProvider(p) {", "  function dashAction(p) {")
    out = _node(r"""
const panel = (o) => ({ dataset: Object.assign({ ckPanel: "x" }, o.ext === false ? {} : { ckExternal: "1" }),
  querySelectorAll: () => (o.rows || []).map((r) => ({ dataset: { keepKey: r.key || "" }, textContent: r.text || "" })),
  querySelector: (sel) => (sel === ".pbody .empty" && o.empty ? {} : null) });
out.noRowsWithEmptyLine = emptyProvider(panel({ empty: true }));
out.noRowsNoLine = emptyProvider(panel({}));                                           // nothing says it is empty: it is not
out.noTeam = emptyProvider(panel({ rows: [{ key: "no_team", text: "This install has no team ledger." }] }));
out.anyOtherEmptyKey = emptyProvider(panel({ rows: [{ key: "no_inbox", text: "No source connected." }] }));
out.backticksAreNotAnEmptyState = emptyProvider(panel({ rows: [{ key: "state", text: "Nothing yet. Run `levain connect`." }] }));   // text is never sniffed
out.realRowWithCode = emptyProvider(panel({ rows: [{ key: "runbook:1", text: "Deploy using `make release`" }] }));
out.oneRealRow = emptyProvider(panel({ rows: [{ key: "m1", text: "hello from the bridge" }] }));   // a genuine row is never collapsed
out.twoRows = emptyProvider(panel({ rows: [{ key: "no_a", text: "`x`" }, { key: "no_b", text: "`y`" }] }));
out.ownCard = emptyProvider(panel({ ext: false, empty: true }));                        // the page's own card keeps its box
""", tmp_path, fn)
    assert out == {"noRowsWithEmptyLine": True, "noRowsNoLine": False, "noTeam": True, "anyOtherEmptyKey": True,
                   "backticksAreNotAnEmptyState": False, "realRowWithCode": False, "oneRealRow": False, "twoRows": False, "ownCard": False}


def test_a_count_pill_comes_from_the_panels_count_never_from_title_text(tmp_path: Path) -> None:
    fn = _cut("  function ckCountPill(node", "  // The Board is two independent stacks")
    out = _node(r"""
const card = (title) => { const h2 = el("h2", null, title); h2.after = (n) => { h2.sibling = n; }; return { h2, querySelector: () => h2 }; };
const run = (title, count) => { const c = card(title); ckCountPill(c, count); return [c.h2.textContent, c.h2.sibling ? c.h2.sibling.textContent : null, c.h2.sibling ? c.h2.sibling.hidden : null]; };
out.tray = run("Tray (2)", 2); out.zero = run("Recent edits (0)", 0); out.plain = run("Neocortex", null);
out.year = run("Annual plan (2026)", null);        // a number in a title is a title
out.yearWithCount = run("Annual plan (2026)", 3);  // the structural count is 3; the year stays in the name
out.mid = run("A (b) c", undefined);
""", tmp_path, fn)
    assert out["tray"] == ["Tray", "2", False] and out["zero"] == ["Recent edits", "0", True]
    assert out["plain"] == ["Neocortex", None, None] and out["mid"] == ["A (b) c", None, None]
    assert out["year"] == ["Annual plan (2026)", None, None]
    assert out["yearWithCount"] == ["Annual plan (2026)", "3", False]


def _block(css: str, opener: str) -> dict[str, str]:
    start = css.index(opener)
    body = css[css.index("{", start) + 1:css.index("}", start)]
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


def test_the_two_light_blocks_agree_and_the_three_tokens_are_the_phones() -> None:
    css = (WEB / "dashboard.css").read_text(encoding="utf-8")
    by_system = _block(css, ':root:not([data-theme="dark"]) {')
    by_switch = _block(css, ':root[data-theme="light"] {')
    assert by_system == by_switch                                          # one light, reached two ways
    phone = _block((WEB / "phone.css").read_text(encoding="utf-8"), ":root {")
    for token in ("--base", "--accent", "--contrast"):
        assert by_switch[token] == phone[token], token                     # the page's light is the phone's three tokens
    # the fill under white text was 3.3:1: the orange button carries dark text in light, and the dark theme keeps its own
    assert by_switch["--on-accent"] == by_switch["--contrast"]
    assert "color-scheme: dark" in css.split(":root[data-theme=\"light\"]")[0]   # native form chrome follows the theme, not a fixed dark
    assert "background: var(--base); color-scheme: dark;" not in css


def test_no_panel_carries_a_colour_strip_and_orange_is_for_what_waits() -> None:
    css = (WEB / "dashboard.css").read_text(encoding="utf-8")
    assert "stripe" not in css
    assert 'data-ck-panel="tray"], [data-ck-panel="loops"]' not in css       # the strips were selected by panel id


# ---- L3 r1 (K2_L3r1.md): G the band's wrap line through the manifest, H the deep link after the token strip, C-F ----

_FIGS = ["links", "avg strength", "max strength", "density", "local density", "episodes", "episodes since wrap", "tombstones",
         "wraps", "graduated", "demoted"]


def _views(tmp_path: Path) -> dict:
    """SubstrateViews built by cockpit_view.js's fromManifest from a wraps panel and an optional health panel."""
    return run_node(r"""
const FIGS = %s;
const rows = (n) => Array.from({ length: n }, (_, i) => ({ wrapped_at: "2026-10-19T06:%%02d:00" .replace("%%02d", String(10 + (i %% 40))), continuity_chars: 100 }));
const metrics = (wraps) => FIGS.map((l) => ({ label: l, value: l === "wraps" ? wraps : 1 })).concat([{ label: "write path", value: "live" }]);
const build = (withHealth, wrapsPanel, total) => {
  const heads = { wraps: head("wraps", "visual", "Wraps") };
  const payloads = { wraps: wrapsPanel };
  if (withHealth) { heads.health = head("health", "metric", "Health"); payloads.health = head("health", "metric", "Health", { value: { metrics: metrics(total) } }); }
  const v = V.fromManifest(fixture(heads, payloads));
  return { wraps: v.wraps, health: v.health, errors: v.errors, entity_name: "Sage" };
};
const good = (n) => head("wraps", "visual", "Wraps", { value: { data: rows(n) } });
const bad = head("wraps", "visual", "Wraps", { status: "error", error: "boom" });
out.withTotal = build(true, good(50), 312);
out.cappedNoTotal = build(false, good(50), 0);
out.underCapNoTotal = build(false, good(3), 0);
out.emptyWithTotal = build(true, good(0), 7);
out.faultedWithHealth = build(true, bad, 41);
out.faultedNoHealth = build(false, bad, 0);
""" % json.dumps(_FIGS), tmp_path)


def test_the_wrap_line_through_the_manifest_never_claims_what_it_does_not_know(tmp_path: Path) -> None:
    views = _views(tmp_path)
    fn = _cut("  function wrapLineOf(view) {", "  let heroEpoch = 0;")
    out = _node("const V = " + json.dumps(views) + r"""; out.l = {};
for (const k of Object.keys(V)) out.l[k] = wrapLineOf(V[k]).kids.map((x) => x.textContent);
""", tmp_path, fn)["l"]
    assert out["withTotal"][1] == "312 wraps"                  # the health panel's total beats the held list
    assert out["cappedNoTotal"][1] == "50+ wraps"              # a full history list is a floor, not a count
    assert out["underCapNoTotal"][1] == "3 wraps held"
    assert out["emptyWithTotal"][0] == "7 wraps", out["emptyWithTotal"]   # a count with no list is a count, never "No wraps yet"
    for k in ("faultedWithHealth", "faultedNoHealth"):         # an unreadable wraps panel is no evidence of "never"
        assert "No wraps yet" not in out[k][0], (k, out[k])
        assert not any("wrap" in x.lower() for x in out[k]), (k, out[k])
        assert out[k][0], (k, out[k])                          # and the cell is never blank


def test_a_deep_link_survives_the_write_token_strip(tmp_path: Path) -> None:
    """The cockpit is opened as `#write_token=T&memory`; dashboard_boot.js strips the token (URLSearchParams spells the rest
    `memory=`), and cockpit_view.js, which the page loads in the head (before boot, while the document is still loading), must still open that tab."""
    drv = tmp_path / "deep.js"
    drv.write_text(r"""
const fs = require("fs");
const [boot, view] = [fs.readFileSync(process.argv[2], "utf8"), fs.readFileSync(process.argv[3], "utf8")];
const store = {};
globalThis.window = globalThis;
globalThis.location = { hash: process.argv[4], search: "", pathname: "/" };
globalThis.history = { replaceState: (a, b, url) => { globalThis.location.hash = url.slice(url.indexOf("#") >= 0 ? url.indexOf("#") : url.length); } };
globalThis.localStorage = { getItem: (k) => store[k] || null, setItem: (k, v) => { store[k] = v; }, removeItem: (k) => { delete store[k]; } };
globalThis.fetch = () => Promise.reject(new Error("offline"));
const node = () => ({ dataset: {}, classList: { add() {}, contains: () => false, toggle() {}, remove() {} }, addEventListener() {}, setAttribute() {}, appendChild() {}, append() {} });
const ready = [];
globalThis.document = { readyState: "loading", visibilityState: "visible", activeElement: null, body: node(), documentElement: node(),
  addEventListener: (t, f) => { if (t === "DOMContentLoaded") ready.push(f); },
  querySelectorAll: () => [], querySelector: () => null, getElementById: () => null, createElement: node, createElementNS: node, createTextNode: () => node() };
new Function(view)();                 // dashboard.html's order: cockpit_view.js in the head, the document still loading
new Function(boot)();                 // then dashboard_boot.js at the foot
globalThis.document.readyState = "interactive";
for (const f of ready.slice()) f();   // the document is ready: the handlers run in registration order
const K = globalThis.LevainCockpitKinds;
console.log(JSON.stringify({ hash: globalThis.location.hash, mode: K.hashMode(), token: store.levain_write_token || null }));
process.exit(0);                      // the page's repaint interval would keep node alive
""", encoding="utf-8")
    import subprocess
    got = {}
    for hash_ in ("#write_token=T&memory", "#write_token=T&build", "#memory&write_token=T", "#write_token=T"):
        r = subprocess.run([NODE, str(drv), str(WEB / "dashboard_boot.js"), str(WEB / "cockpit_view.js"), hash_], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        got[hash_] = json.loads(r.stdout.strip().splitlines()[-1])
    assert [got[h]["mode"] for h in ("#write_token=T&memory", "#write_token=T&build", "#memory&write_token=T")] == ["memory", "build", "memory"], got
    assert got["#write_token=T"]["mode"] == ""
    assert all(g["token"] == "T" for g in got.values()), got                # the token was taken and kept


def test_the_tab_name_with_a_trailing_equals_is_read_and_rewritten_once(tmp_path: Path) -> None:
    out = run_node(r"""
globalThis.location = { hash: "#memory=", search: "", pathname: "/" };
out.read = K.hashMode();
const calls = [];
globalThis.history = { replaceState: (a, b, url) => calls.push(url) };
K.applyMode("build", true);
out.written = calls;
""", tmp_path)
    assert out["read"] == "memory" and out["written"] == ["/#build"], out


def test_a_fragment_part_that_is_not_a_tab_keeps_its_equals(tmp_path: Path) -> None:
    out = run_node(r"""
globalThis.location = { hash: "#key=&memory=", search: "", pathname: "/" };
const calls = [];
globalThis.history = { replaceState: (a, b, url) => calls.push(url) };
K.applyMode("build", true);
out.written = calls;
""", tmp_path)
    assert out["written"] == ["/#key=&build"], out


def test_the_legacy_page_keeps_only_the_two_mode_rules_it_had() -> None:
    css = (WEB / "dashboard.css").read_text(encoding="utf-8")
    bare = [l for l in css.splitlines() if re.match(r'\s*body\[data-mode="(board|memory)"\] \[data-modes\]', l)]
    assert bare == [], bare                                    # board / memory hide only on the KE page (body.ke)
    assert 'body.ke[data-mode="board"] [data-modes]:not([data-modes~="board"])' in css


def test_a_glance_with_every_tile_hidden_on_a_tab_is_hidden_whole(tmp_path: Path) -> None:
    css = (WEB / "dashboard.css").read_text(encoding="utf-8")
    assert re.search(r'\.ck-widgets\[data-n="0"\]\s*\{\s*display:\s*none', css)
    out = run_node(r"""
globalThis.getComputedStyle = () => ({ display: "none" });
const props = {};
const w = Object.assign(new N("section"), { style: { setProperty: (k, v) => { props[k] = v; } }, children: [Object.assign(new N("div"), { dataset: {} })] });
globalThis.document.body.dataset.mode = "build";
globalThis.document.querySelectorAll = (sel) => (sel === ".ck-widgets" ? [w] : []);
K.fillGrids();
out.n = w.dataset.n;
""", tmp_path)
    assert out["n"] == "0"


def test_a_panel_that_stops_being_dashed_loses_its_open_state_and_action(tmp_path: Path) -> None:
    fn = _cut("  function dashAction(p) {", "  // Tabs of one card:") + "\n" + _cut("  function emptyProvider(p) {", "  function dashAction(p) {")
    out = _node(r"""
const cls = new Set(["ck-dash", "ck-dash-open"]); let act = { removed: false, remove() { this.removed = true; } };
const p = { dataset: { ckPanel: "x", ckExternal: "1" }, classList: { toggle: (c, on) => { on ? cls.add(c) : cls.delete(c); }, remove: (c) => cls.delete(c), add: (c) => cls.add(c) },
  querySelectorAll: () => [{ dataset: { keepKey: "m1" }, textContent: "a real row" }], querySelector: (sel) => (/ck-dash-act/.test(sel) ? act : null) };
dashPass({ querySelectorAll: () => [p] });
out.cls = Array.from(cls); out.actRemoved = act.removed;
""", tmp_path, fn)
    assert out["cls"] == [] and out["actRemoved"] is True, out


def test_the_grid_layout_is_rerun_from_the_one_debounced_path(tmp_path: Path) -> None:
    assert "function scheduleLayout()" in CORE, "dashboard_core.js has no scheduleLayout()"
    fn = _cut("  let layoutTimer = null;", "  // Tabs of one card:")
    out = _node(r"""
let calls = 0; const timers = [];
globalThis.window = { LevainCockpitKinds: { fillGrids: () => { calls++; } }, setTimeout: (f) => { timers.push(f); return timers.length; }, clearTimeout: (i) => { timers[i - 1] = null; } };
scheduleLayout(); scheduleLayout(); scheduleLayout();
for (const f of timers) if (f) f();
out.calls = calls;
""", tmp_path, fn)
    assert out["calls"] == 1
    assert "scheduleLayout" in re.search(r"new MutationObserver\([^\n]*", CORE).group(0)


def test_the_band_wrap_cap_is_the_kernels_wrap_history_limit():
    """dashboard_core.js reads a full wraps list as "N+ wraps"; its cap must be the kernel's default history length."""
    import re
    from pathlib import Path

    from levain import dashboard

    js = (Path(dashboard.__file__).parent / "templates" / "web" / "dashboard_core.js").read_text()
    page_cap = int(re.search(r"const WRAP_LIST_CAP = (\d+);", js).group(1))
    src = Path(dashboard.__file__).read_text()
    kernel_cap = int(re.search(r"max_wraps: int = (\d+)", src).group(1))
    assert page_cap == kernel_cap


def test_the_glance_tiles_alone_ask_for_the_layout_again() -> None:
    src = (WEB / "dashboard_core.js").read_text(encoding="utf-8")
    assert re.search(r'new MutationObserver\(scheduleLayout\)\.observe\(region, \{ childList: true \}\)', src)
