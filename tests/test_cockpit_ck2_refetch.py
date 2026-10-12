"""CK2: refetchPanel keeps a panel whose chrome is unchanged and repaints only its body; a changed shape declines
so the caller reloads through the one mount path. Run in node against a small fake DOM."""

from __future__ import annotations

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
  constructor(tag) { this.tag = tag; this.kids = []; this.parent = null; this.className = ""; this.dataset = {}; this.attrs = {}; this.text = ""; this.live = false; this.listeners = {};
    const self = this; this.classList = { add: (c) => { if (!self.className.split(" ").includes(c)) self.className = (self.className + " " + c).trim(); },
      contains: (c) => self.className.split(" ").includes(c), toggle() {}, remove() {} }; }
  get isConnected() { let n = this; while (n) { if (n.live) return true; n = n.parent; } return false; }
  get children() { return this.kids; }
  set textContent(t) { this.text = String(t); this.kids = []; }
  get textContent() { return this.text + this.kids.map((k) => k.textContent).join(""); }
  appendChild(k) { k.parent = this; this.kids.push(k); return k; }
  append(...ks) { for (const k of ks) this.appendChild(typeof k === "string" ? Object.assign(new N("#t"), { text: k }) : k); }
  replaceChildren(...ks) { this.kids.forEach((k) => { k.parent = null; }); this.kids = []; this.append(...ks); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  querySelector(sel) { const c = sel.replace(".", ""); const walk = (n) => { for (const k of n.kids) { if (k.tag === c || k.className.split(" ").includes(c)) return k; const r = walk(k); if (r) return r; } return null; }; return walk(this); }
  closest(sel) { const cs = sel.split(",").map((s) => s.trim().replace(".", "")); let n = this; while (n) { if (cs.some((c) => n.className.split(" ").includes(c))) return n; n = n.parent; } return null; }
}
globalThis.document = { readyState: "complete", activeElement: null, body: Object.assign(new N("body"), {}), addEventListener() {}, querySelectorAll: () => [],
  querySelector: () => null, createElement: (t) => new N(t) };
new Function(src)();
const V = globalThis.LevainCockpitView, K = globalThis.LevainCockpitKinds;
const head = (kind, title) => ({ id: "p", kind, title, status: "ok", as_of: "2026-10-11T12:00:00+00:00" });
const rows = (n) => Array.from({ length: n }, (_, i) => ({ id: "r" + i, version: "v", title: "row " + i, group: null, facets: {}, actions: [] }));
const payload = (n, title) => Object.assign(head("list", title || "Things"), { rows: rows(n), actions: [] });
const manifest = (title) => ({ schema: "levain.cockpit/1", entity: { name: "t", governance: "reported", brand: {} }, generated_at: "2026-10-11T12:00:00+00:00",
  regions: { header: [], zones: [{ id: "operate", title: "Operate", panels: ["p"] }] }, panels: { p: head("list", title || "Things") }, verbs: {}, errors: [], transport: { writable: false } });
let failed = 0;
const ok = (c, w) => { if (!c) { failed++; console.log("FAIL " + w); } };
(async () => {
  const view = V.fromManifest({ manifest: manifest(), panels: { p: payload(3) } });
  const entry = view.layout.find((e) => e.id === "p");
  const p = new N("div"); p.live = true; p.className = "panel";
  const h = new N("h2"); h.text = "Things"; p.appendChild(h);
  let renders = 0, measures = 0, replaced = 0;
  p.replaceWith = () => { replaced++; };
  globalThis.LevainDashboard = { renderPanel: () => { renders++; return new N("div"); }, measureClauses: () => { measures++; }, measureOverflow() {} };
  const painted = [];
  const s = K.fill(p, view.extra_panels.p, { rows: (host, lines) => { painted.push(lines.length); }, entry });
  ok(painted[painted.length - 1] === 3, "first paint drew 3 rows: " + painted);
  const host = new N("div"); host.className = "pbody"; host.appendChild(s.body); host.live = true; s.body.parent = host; host.kids = [s.body];
  p.kids.pop(); // the body now lives under the host, as finalisation puts it
  p.appendChild(host); s.p = p;
  globalThis.LevainCockpitTransport = { readJson: async () => globalThis.__next };
  // unchanged shape: only the rows moved
  globalThis.__next = payload(5);
  let r = await K.refetchPanel("p");
  ok(r === true, "same shape returns true");
  ok(renders === 0 && replaced === 0, "no renderPanel and no replaceWith on a same-shape refetch");
  ok(painted[painted.length - 1] === 5, "the body repainted the new rows: " + painted);
  ok(measures >= 1, "the host was re-measured");
  ok(s.body.parent === host, "the body node was kept in its host");
  // changed shape: the title is chrome
  globalThis.__next = payload(5, "Other things");
  r = await K.refetchPanel("p");
  ok(r === false, "a changed title declines");
  ok(renders === 0 && replaced === 0, "a declined refetch builds nothing");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("THREW " + (e && e.stack || e)); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_refetch_updates_a_same_shape_panel_in_place_and_declines_a_changed_shape(tmp_path: Path) -> None:
    drv = tmp_path / "refetch_driver.js"
    drv.write_text(DRIVER, encoding="utf-8")
    r = subprocess.run([NODE, str(drv), str(WEB / "cockpit_view.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_second_mount_path_is_gone() -> None:
    js = (WEB / "cockpit_view.js").read_text(encoding="utf-8")
    body = js[js.index("async function refetchPanel"):js.index("// ---- the modes and the keys ----")]
    assert "renderPanel" not in body and "replaceWith" not in body and "keepFocus" not in body and "hoverPanel" not in body
    assert "finalizeNode" not in (WEB / "dashboard_core.js").read_text(encoding="utf-8")
