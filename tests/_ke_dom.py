"""A fake DOM and a node runner for page tests that need no browser: cockpit_view.js is loaded the way
test_cockpit_ke2_page.py loads it, and a test body (JS) runs against `V` (LevainCockpitView), `K` (the kinds),
`N` (the fake node), `mount(id)` (a node `document.getElementById(id)` returns), `head` and `fixture`."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
NODE = shutil.which("node")

PRELUDE = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
class N {
  constructor(tag) { this.tag = tag; this.kids = []; this.className = ""; this.attrs = {}; this.dataset = {}; this.text = ""; this.hidden = false; this.placeholder = "";
    const self = this; this.classList = { add: (c) => { self.className = (self.className + " " + c).trim(); }, contains: (c) => (" " + self.className + " ").indexOf(" " + c + " ") >= 0, toggle() {}, remove() {} }; }
  set textContent(t) { this.text = String(t); this.kids = []; }
  get childNodes() { return this.kids; }
  get textContent() { return this.text + this.kids.map((k) => k.textContent).join(""); }
  appendChild(k) { this.kids.push(k); return k; }
  append(...ks) { for (const k of ks) this.appendChild(typeof k === "string" ? Object.assign(new N("#t"), { text: k }) : k); }
  replaceChildren(...ks) { this.kids = []; this.append(...ks); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener() {}
  all(pred, out) { out = out || []; if (pred(this)) out.push(this); for (const k of this.kids) k.all(pred, out); return out; }
  hasClass(c) { return this.classList.contains(c); }
}
const mounts = {};
const mount = (id) => (mounts[id] = mounts[id] || new N("div"));
globalThis.document = { readyState: "complete", activeElement: null, body: new N("body"), addEventListener() {}, querySelectorAll: () => [],
  querySelector: () => null, getElementById: (id) => mounts[id] || null, createElement: (t) => new N(t), createElementNS: (ns, t) => new N(t),
  createTextNode: (t) => Object.assign(new N("#t"), { text: t }) };
new Function(src)();
const V = globalThis.LevainCockpitView, K = globalThis.LevainCockpitKinds;
const head = (id, kind, title, extra) => Object.assign({ id, kind, title, status: "ok", as_of: "2026-10-12T12:00:00+00:00" }, extra || {});
// a read-only manifest of the given panel heads, one operate zone; panels = {id: payload}
const fixture = (heads, payloads, over) => ({ manifest: Object.assign({ schema: "levain.cockpit/2", entity: { name: "t", governance: "reported", brand: {} },
  generated_at: "2026-10-12T12:00:00+00:00", regions: { header: [], zones: [{ id: "operate", title: "Operate", panels: Object.keys(heads) }] },
  panels: heads, verbs: {}, errors: [], transport: { writable: false } }, over || {}), panels: payloads });
const out = {};
"""


def run_node(body: str, tmp_path: Path) -> dict:
    drv = tmp_path / "driver.js"
    drv.write_text(PRELUDE + body + "\nconsole.log(JSON.stringify(out));\n", encoding="utf-8")
    r = subprocess.run([NODE, str(drv), str(WEB / "cockpit_view.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout.strip().splitlines()[-1])
