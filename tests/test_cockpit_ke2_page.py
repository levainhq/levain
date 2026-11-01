"""KE2: the page draws the KE regions. Run in node.

* a manifest declaring hero / brief / widgets maps those panels as bare typed panels in `view.ke`, with the zone
  cards beside them; a widget named `health` is NOT the built-in Health panel (found live: it drew "cannot be
  drawn here" because the region was never passed to placeOnce);
* an older kernel's `focus` line, which the KE1 contract normalises into the brief, is still skipped;
* a manifest with no regions of its own keeps the old header path (`view.ke` absent);
* a reading with a sub-reading that carries a `band` paints it as a plain side line, band in words, with no glyph and no pill.

Fixtures are synthetic; no kernel, store or browser is involved."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
NODE = shutil.which("node")

DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
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
  regions, panels: { "reading-a": head("reading-a", "reading", "Reading A"), health: head("health", "status-dots", "Health"), decisions: head("decisions", "list", "Decisions", { face: "count" }),
    brief: head("brief", "brief", "Brief"), state: head("state", "line", "State"), focus: head("focus", "line", "Focus"), tray: head("tray", "list", "Tray") }, verbs: {}, errors: [], transport: { writable: false } });
const panels = { "reading-a": head("reading-a", "reading", "Reading A", { value: { value: 66, unit: "F", condition: "Cloudy", symbol: "cloudy", sub: [{ label: "air", value: 55, band: "Moderate" }, { label: "feels like", value: "64F" }] } }),
  health: head("health", "status-dots", "Health", { value: { metrics: [{ label: "write path", value: "live", status: "ok" }] } }),
  decisions: head("decisions", "list", "Decisions", { face: "count", count: 2, rows: [] }), brief: head("brief", "brief", "Brief", { value: { lines: [{ label: "a", text: "b" }] } }),
  state: head("state", "line", "State", { value: { lines: [{ label: "state", text: "hi", at: "2026-10-12T11:00:00.000Z", source: "web" }], value_version: "v" } }),
  focus: head("focus", "line", "Focus", { value: { lines: [{ label: "focus", text: "x", at: "2026-10-12T11:00:00.000Z", source: "s" }] } }),
  tray: head("tray", "list", "Tray", { rows: [], actions: [] }) };
const out = {};
const keRegions = { hero: ["reading-a"], brief: ["focus", "brief", "state"], widgets: ["health", "decisions"], header: ["reading-a", "focus", "brief", "state"],
  zones: [{ id: "operate", title: "Work", grid: 3, panels: ["tray"], cards: [{ panels: ["tray"], size: "tall", span: 1, tabs: null }] }] };
const ke = V.fromManifest({ manifest: manifest(keRegions), panels });
out.ke = ke.ke;
out.kinds = Object.fromEntries(ke.layout.map((e) => [e.id, e.kind]));
out.regionOf = Object.fromEntries(ke.layout.map((e) => [e.id, e.region || null]));
out.modes = Object.fromEntries(ke.layout.map((e) => [e.id, e.modes]));
out.face = ke.extra_panels.decisions.face;
const old = V.fromManifest({ manifest: manifest({ header: ["state", "reading-a"], zones: [{ id: "operate", title: "Operate", panels: ["tray"] }] }), panels });
out.old = { ke: old.ke === undefined, layout: old.layout.map((e) => e.id) };
// the reading
const paint = (value) => {
  const host = new N("div");
  const s = K.fill(host, { kind: "reading", pid: "reading-a", typed: true, reading: value, search: {}, note: "", empty: "" }, { entry: { title: "Reading A" } });
  return host;
};
const base = ke.extra_panels["reading-a"].reading;
const host = paint(base);
out.svgs = host.all((n) => n.tag === "svg").length;
out.pills = host.all((n) => n.className === "pill" || /pill/.test(n.className || "")).length;
out.text = host.textContent;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_ke_regions_map_and_the_reading_draws_generically(tmp_path: Path) -> None:
    driver = tmp_path / "driver.js"
    driver.write_text(DRIVER)
    src = tmp_path / "view.js"
    src.write_text((WEB / "cockpit_view.js").read_text())
    run = subprocess.run([NODE, str(driver), str(src)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["ke"]["hero"] == ["reading-a"] and out["ke"]["widgets"] == ["health", "decisions"]
    assert out["ke"]["brief"] == ["brief"], "focus is retired data and state is the operator's line, not a card"
    assert out["ke"]["zones"][0]["cards"] == [{"panels": ["tray"], "size": "tall", "span": 1, "tabs": ""}]
    # a widget named health is a typed external panel, never the built-in Health component
    assert out["kinds"]["health"] == "external" and out["regionOf"]["health"] == "widgets"
    assert out["face"] == "count"
    # the hero is the header in every mode: its panels are not Build-dropped readings (Phill 10-12: the header in Build mode
    # once lost its hero panel); the brief, an orientation read, still leaves Build
    assert out["modes"]["reading-a"].split() == ["morning", "board", "build", "memory"]
    assert "build" not in out["modes"]["brief"].split()
    assert out["old"]["ke"] and "focus" not in out["old"]["layout"]
    assert out["svgs"] == 0 and out["pills"] == 0
    assert "air 55 Moderate" in out["text"] and "feels like 64F" in out["text"]


DRIVER2 = DRIVER.split("const out = {};")[0] + r"""
const out = {};
const wm = (pid, kind, value, extra) => ({ manifest: manifest({ hero: [], brief: [], widgets: [pid], header: [], zones: [{ id: "operate", title: "Work", grid: 3, panels: [], cards: [] }] }),
  panels: { [pid]: head(pid, kind, pid, Object.assign({ value }, extra || {})) } });
const draw = (pid, kind, value, ctx, extra) => {
  const m = wm(pid, kind, value, extra);
  m.manifest.panels = { [pid]: head(pid, kind, pid, extra) };
  const v = V.fromManifest(m);
  const host = new N("div");
  K.fill(host, v.extra_panels[pid], Object.assign({ entry: { title: pid } }, ctx || {}));
  return { v, host, text: host.textContent };
};
// W1: every metric of a status-dots widget is drawn once, whatever its status or value shape
const w1 = draw("ops", "status-dots", { metrics: [{ label: "load", value: "3 of 4" }, { label: "ratio", value: 0.25, status: "unknown" }, { label: "disk", value: "ok-ish", status: "warn" }] }, { widget: true });
out.w1 = w1.text;
out.w1empty = draw("ops", "status-dots", { metrics: [] }, { widget: true }, { empty: "no dots" }).text;
// W3: the write path read off a widget's own metrics
const wv = (live) => draw("health", "status-dots", { metrics: [{ label: "write path", value: live, status: "ok" }] }, { widget: true }).v;
out.w3 = [wv("live").write_path_live, wv("dark").write_path_live, draw("ops", "status-dots", { metrics: [] }, { widget: true }).v.write_path_live, wv("no wraps yet").write_path_live, wv("no wraps yet").write_path_status];
// W4: a repeated label keeps its own heading and clamp
const lines = [];
for (let i = 0; i < 8; i++) lines.push({ label: "Tasks", text: "t" + i });
for (let i = 0; i < 2; i++) lines.push({ label: "Risks", text: "r" + i });
const b = draw("brief", "brief", { lines }, { region: "brief" });
out.w4 = b.host.all((n) => n.className === "k").map((n) => n.textContent);
out.w4side = b.host.all((n) => /side-line/.test(n.className)).map((n) => n.textContent);
out.w4more = b.host.all((n) => n.className === "xnote").map((n) => n.textContent);
console.log(JSON.stringify(out));
"""


def _run2(tmp_path: Path) -> dict:
    driver = tmp_path / "driver2.js"
    driver.write_text(DRIVER2)
    src = tmp_path / "view.js"
    src.write_text((WEB / "cockpit_view.js").read_text())
    run = subprocess.run([NODE, str(driver), str(src), "[]"], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_widgets_draw_every_metric_and_the_brief_groups_by_label(tmp_path: Path) -> None:
    out = _run2(tmp_path)
    for needle in ("load", "3 of 4", "ratio", "0.25", "disk", "ok-ish"):
        assert needle in out["w1"], f"{needle!r} missing from the widget: {out['w1']!r}"
    assert "no dots" in out["w1empty"] and "nothing" not in out["w1"]
    assert out["w3"] == [True, False, None, False, "no-wraps"], "liveness comes from the widget's own write-path metric, or is unknown"
    assert out["w4"] == ["Tasks", "Risks"]
    assert out["w4side"] == [f"t{i}" for i in range(6)] + ["r0", "r1"]
    assert out["w4more"] == ["+2 more"]
