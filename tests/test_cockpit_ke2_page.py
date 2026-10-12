"""KE2: the page draws the KE regions. Run in node.

* a manifest declaring hero / brief / widgets maps those panels as bare typed panels in `view.ke`, with the zone
  cards beside them; a widget named `health` is NOT the built-in Health panel (found live: it drew "cannot be
  drawn here" because the region was never passed to placeOnce);
* an older kernel's `focus` line, which the KE1 contract normalises into the brief, is still skipped;
* a manifest with no regions of its own keeps the old header path (`view.ke` absent);
* a reading carrying a `symbol` and a sub-reading with a `band` paints its icon and an AQI pill, one icon per
  token of READING_SYMBOLS, and an unknown band renders neutral.

Fixtures are synthetic; no kernel, store or browser is involved."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from levain.cockpit.engine import READING_SYMBOLS

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
NODE = shutil.which("node")

DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const SYMBOLS = JSON.parse(process.argv[3]);
class N {
  constructor(tag) { this.tag = tag; this.kids = []; this.className = ""; this.attrs = {}; this.dataset = {}; this.text = "";
    const self = this; this.classList = { add: (c) => { self.className = (self.className + " " + c).trim(); }, contains: () => false, toggle() {}, remove() {} }; }
  set textContent(t) { this.text = String(t); this.kids = []; }
  get childNodes() { return this.kids; }
  get textContent() { return this.text + this.kids.map((k) => k.textContent).join(""); }
  appendChild(k) { this.kids.push(k); return k; }
  append(...ks) { for (const k of ks) this.appendChild(typeof k === "string" ? Object.assign(new N("#t"), { text: k }) : k); }
  replaceChildren(...ks) { this.kids = []; this.append(...ks); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener() {}
  all(pred, out) { out = out || []; if (pred(this)) out.push(this); for (const k of this.kids) k.all(pred, out); return out; }
}
globalThis.document = { readyState: "complete", activeElement: null, body: new N("body"), addEventListener() {}, querySelectorAll: () => [],
  querySelector: () => null, createElement: (t) => new N(t), createElementNS: (ns, t) => new N(t), createTextNode: (t) => Object.assign(new N("#t"), { text: t }) };
new Function(src)();
const V = globalThis.LevainCockpitView, K = globalThis.LevainCockpitKinds;
const head = (id, kind, title, extra) => Object.assign({ id, kind, title, status: "ok", as_of: "2026-10-12T12:00:00+00:00" }, extra || {});
const manifest = (regions) => ({ schema: "levain.cockpit/2", entity: { name: "t", governance: "reported", brand: {} }, generated_at: "2026-10-12T12:00:00+00:00",
  regions, panels: { weather: head("weather", "reading", "Weather"), health: head("health", "status-dots", "Health"), decisions: head("decisions", "list", "Decisions", { face: "count" }),
    brief: head("brief", "brief", "Brief"), state: head("state", "line", "State"), focus: head("focus", "line", "Focus"), tray: head("tray", "list", "Tray") }, verbs: {}, errors: [], transport: { writable: false } });
const panels = { weather: head("weather", "reading", "Weather", { value: { value: 66, unit: "F", condition: "Cloudy", symbol: "cloudy", sub: [{ label: "air", value: 55, band: "Moderate" }, { label: "feels like", value: "64F" }] } }),
  health: head("health", "status-dots", "Health", { value: { metrics: [{ label: "write path", value: "live", status: "ok" }] } }),
  decisions: head("decisions", "list", "Decisions", { face: "count", count: 2, rows: [] }), brief: head("brief", "brief", "Brief", { value: { lines: [{ label: "a", text: "b" }] } }),
  state: head("state", "line", "State", { value: { lines: [{ label: "state", text: "hi", at: "2026-10-12T11:00:00.000Z", source: "web" }], value_version: "v" } }),
  focus: head("focus", "line", "Focus", { value: { lines: [{ label: "focus", text: "x", at: "2026-10-12T11:00:00.000Z", source: "s" }] } }),
  tray: head("tray", "list", "Tray", { rows: [], actions: [] }) };
const out = {};
const keRegions = { hero: ["weather"], brief: ["focus", "brief", "state"], widgets: ["health", "decisions"], header: ["weather", "focus", "brief", "state"],
  zones: [{ id: "operate", title: "Work", grid: 3, panels: ["tray"], cards: [{ panels: ["tray"], size: "tall", span: 1, tabs: null }] }] };
const ke = V.fromManifest({ manifest: manifest(keRegions), panels });
out.ke = ke.ke;
out.kinds = Object.fromEntries(ke.layout.map((e) => [e.id, e.kind]));
out.regionOf = Object.fromEntries(ke.layout.map((e) => [e.id, e.region || null]));
out.face = ke.extra_panels.decisions.face;
const old = V.fromManifest({ manifest: manifest({ header: ["state", "weather"], zones: [{ id: "operate", title: "Operate", panels: ["tray"] }] }), panels });
out.old = { ke: old.ke === undefined, layout: old.layout.map((e) => e.id) };
// the reading
const paint = (value) => {
  const host = new N("div");
  const s = K.fill(host, { kind: "reading", pid: "weather", typed: true, reading: value, search: {}, note: "", empty: "" }, { entry: { title: "Weather" } });
  return host;
};
const base = ke.extra_panels.weather.reading;
const host = paint(base);
out.icon = host.all((n) => n.attrs["data-ck-weather"] === "icon").length;
const pills = host.all((n) => n.attrs["data-ck-weather"] === "aqi");
out.pill = pills.map((p) => [p.textContent, p.className]);
out.symbols = {};
for (const sym of SYMBOLS) { const h = paint(Object.assign({}, base, { symbol: sym })); const svg = h.all((n) => n.tag === "svg" && n.attrs["class"] === "wx-icon")[0]; out.symbols[sym] = svg ? svg.kids.length : 0; }
const odd = paint(Object.assign({}, base, { sub: [{ label: "air", value: 9, band: "mauve" }] }));
out.odd = odd.all((n) => n.attrs["data-ck-weather"] === "aqi").map((p) => [p.textContent, p.className]);
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_ke_regions_map_and_the_reading_draws_its_icon_and_pill(tmp_path: Path) -> None:
    driver = tmp_path / "driver.js"
    driver.write_text(DRIVER)
    src = tmp_path / "view.js"
    src.write_text((WEB / "cockpit_view.js").read_text())
    run = subprocess.run([NODE, str(driver), str(src), json.dumps(list(READING_SYMBOLS))], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["ke"]["hero"] == ["weather"] and out["ke"]["widgets"] == ["health", "decisions"]
    assert out["ke"]["brief"] == ["brief"], "focus is retired data and state is the operator's line, not a card"
    assert out["ke"]["zones"][0]["cards"] == [{"panels": ["tray"], "size": "tall", "span": 1, "tabs": ""}]
    # a widget named health is a typed external panel, never the built-in Health component
    assert out["kinds"]["health"] == "external" and out["regionOf"]["health"] == "widgets"
    assert out["face"] == "count"
    assert out["old"]["ke"] and "focus" not in out["old"]["layout"]
    assert out["icon"] == 1
    assert out["pill"] == [["Moderate", "pill hot"]]
    assert all(n > 0 for n in out["symbols"].values()) and set(out["symbols"]) == set(READING_SYMBOLS)
    assert out["odd"] == [["mauve", "pill"]], "an unknown band renders neutral, in its own words"
