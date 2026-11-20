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
  remove() { if (this.parent) { this.parent.kids = this.parent.kids.filter((k) => k !== this); this.parent = null; } }
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
  ok(r === "applied", "same shape returns applied");
  ok(renders === 0 && replaced === 0, "no renderPanel and no replaceWith on a same-shape refetch");
  ok(painted[painted.length - 1] === 5, "the body repainted the new rows: " + painted);
  ok(measures >= 1, "the host was re-measured");
  ok(s.body.parent === host, "the body node was kept in its host");
  // the held etag follows the panel read that was applied, not any event: only the page's own read records it
  globalThis.LevainCockpitHeld = { p: { etag: "drawn" } };
  globalThis.__next = Object.assign(payload(6), { etag: "own-read" });
  r = await K.refetchPanel("p");
  ok(r === "applied" && globalThis.LevainCockpitHeld.p.etag === "own-read", "an applied refetch records its own read's etag: " + JSON.stringify(globalThis.LevainCockpitHeld));
  // a superseded refetch (an older read finishing after a newer one) is neither applied nor a fault, and records nothing
  {
    const gates = [];
    globalThis.LevainCockpitTransport = { readJson: () => new Promise((res) => gates.push(res)) };
    globalThis.LevainCockpitHeld = { p: { etag: "drawn" } };
    const older = K.refetchPanel("p"), newer = K.refetchPanel("p");
    gates[1](Object.assign(payload(7), { etag: "newer" }));
    ok(await newer === "applied", "the newer refetch applies");
    gates[0](Object.assign(payload(4), { etag: "older" }));
    const ro = await older;
    ok(ro === "superseded", "the older refetch is superseded, not applied: " + ro);
    ok(globalThis.LevainCockpitHeld.p.etag === "newer", "a superseded read records nothing: " + globalThis.LevainCockpitHeld.p.etag);
    globalThis.LevainCockpitTransport = { readJson: async () => globalThis.__next };
  }
  // changed shape: the title is chrome
  globalThis.__next = payload(5, "Other things");
  r = await K.refetchPanel("p");
  ok(r === false, "a changed title declines");
  ok(renders === 0 && replaced === 0, "a declined refetch builds nothing");
  // levain#173: a read that fails (WebKit's bare TypeError "Load failed" is the real one) keeps the rows the panel shows and says so quietly;
  // a panel that was drawn as unavailable stays unavailable; the next good read clears the line
  {
    const shown = painted[painted.length - 1], before = painted.length;
    globalThis.LevainCockpitTransport = { readJson: async () => { throw new TypeError("Load failed"); } };
    const rf = await K.refetchPanel("p");
    ok(rf === "fault", "a failed read answers fault: " + rf);
    ok(painted.length > before && painted[painted.length - 1] === shown, "the rows are painted again, none lost: " + painted.slice(before - 1));
    ok(!s.fault && s.stale === "Load failed", "the panel keeps its rows and records why it is stale: " + JSON.stringify([s.fault, s.stale]));
    const note = s.body.querySelector(".stale-note");
    ok(!!note && /showing the last read/.test(note.textContent), "one quiet line says the refresh failed: " + (note && note.textContent));
    const rf2 = await K.refetchPanel("p");
    ok(rf2 === "fault" && s.body.kids.filter((k) => k.className.split(" ").includes("stale-note")).length === 1, "a second failure keeps ONE line");
    globalThis.LevainCockpitTransport = { readJson: async () => globalThis.__next };
    globalThis.__next = payload(7);
    ok(await K.refetchPanel("p") === "applied" && !s.stale && !s.body.querySelector(".stale-note"), "a good read clears the line and the reason");
    s.fault = "never read";
    globalThis.LevainCockpitTransport = { readJson: async () => { throw new TypeError("Load failed"); } };
    await K.refetchPanel("p");
    ok(s.fault === "Load failed" && !s.stale && !s.body.querySelector(".stale-note"), "a panel drawn as unavailable stays unavailable: " + JSON.stringify([s.fault, s.stale]));
    s.fault = "";
    globalThis.LevainCockpitTransport = { readJson: async () => globalThis.__next };
  }
  // a modal copy ends with its modal body: pruneModal tears down only the detached modal state
  const mp = new N("div"); mp.live = true; mp.className = "modal-body";
  const ms = K.fill(mp, view.extra_panels.p, { rows: () => {}, entry, modal: true });
  const seqLive = ms.seq, seqBoard = s.seq;
  K.pruneModal();
  ok(ms.seq === seqLive, "a modal state still on the page is kept");
  mp.live = false;
  K.pruneModal();
  ok(ms.seq > seqLive, "a detached modal state is torn down");
  ok(s.seq === seqBoard, "a board state is not touched by pruneModal");
  // a brief-region panel is not drawn: its announcement is answered "applied" (no board reload), and an id the manifest does not
  // place in the brief region still answers false
  {
    const ke = { schema: "levain.cockpit/1", entity: { name: "t", governance: "reported", brand: {} }, generated_at: "2026-10-11T12:00:00+00:00",
      regions: { header: [], hero: [], brief: ["begin"], widgets: [], zones: [] }, panels: { begin: Object.assign(head("brief", "Where to begin"), { id: "begin" }) }, verbs: {}, errors: [], transport: { writable: false } };
    V.fromManifest({ manifest: ke, panels: { begin: Object.assign(head("brief", "Where to begin"), { id: "begin", brief: { headline: "x", lines: [] } }) } });
    globalThis.LevainCockpitHeld = { begin: { etag: "old" } };
    globalThis.__next = Object.assign(head("brief", "Where to begin"), { id: "begin", brief: { headline: "x", lines: [] }, etag: "new" });
    ok(await K.refetchPanel("begin") === "applied", "an undrawn brief-region panel needs no reload");
    ok(globalThis.LevainCockpitHeld.begin.etag === "new", "its own read records the held etag, so the manifest diff converges: " + JSON.stringify(globalThis.LevainCockpitHeld));
    ok(await K.refetchPanel("ghost") === false, "a panel the manifest does not place still asks for a reload");
  }
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("THREW " + (e && e.stack || e)); process.exit(1); });
"""


# levain#168: an age tick or a count in a title is data, not chrome: it repaints in place and never reloads the board
DRIVER_CHROME_TEXT = DRIVER[:DRIVER.index("const V = globalThis")] + r"""
N.prototype.querySelector = function (sel) {
  let cur = [this];
  for (const part of sel.split(" ")) {
    const c = part.replace(".", ""), nx = [];
    const walk = (x) => { for (const k of x.kids) { if (k.tag === c || k.className.split(" ").includes(c)) nx.push(k); walk(k); } };
    cur.forEach(walk); cur = nx;
  }
  return cur[0] || null;
};
N.prototype.querySelectorAll = function (sel) {
  const out = [], walk = (x) => { for (const k of x.kids) { if (k.dataset && k.dataset.bn !== undefined) out.push(k); walk(k); } };
  walk(this); return out;
};
const V = globalThis.LevainCockpitView, K = globalThis.LevainCockpitKinds;
const mk = (title, status, asOf) => ({ id: "p", kind: "list", title, status, as_of: asOf, rows: [], actions: [] });
const manifest = (hd) => ({ schema: "levain.cockpit/1", entity: { name: "t", governance: "reported", brand: {} }, generated_at: "2026-10-11T12:00:00+00:00",
  regions: { header: [], zones: [{ id: "operate", title: "Operate", panels: ["p"] }] }, panels: { p: hd }, verbs: {}, errors: [], transport: { writable: false } });
let failed = 0;
const ok = (c, w) => { if (!c) { failed++; console.log("FAIL " + w); } };
(async () => {
  const first = mk("Inbox (3)", "stale", "2026-10-11T11:55:00+00:00");
  const view = V.fromManifest({ manifest: manifest(first), panels: { p: first } });
  const entry = view.layout.find((e) => e.id === "p");
  const bannerText = entry.banner && entry.banner[0] && entry.banner[0].text;
  ok(/^STALE: last read/.test(bannerText || ""), "the fixture panel carries a stale banner: " + bannerText);
  // the panel as the draw leaves it: a head with the title, then one ext-note per banner line marked data-bn
  const p = new N("div"); p.live = true; p.className = "panel";
  const head = new N("div"); head.className = "phead"; const h2 = new N("h2"); h2.text = entry.title; head.appendChild(h2); p.appendChild(head);
  const note = new N("div"); note.className = "ext-note stale"; note.dataset.bn = "0"; note.text = bannerText; p.appendChild(note);
  let renders = 0;
  p.replaceWith = () => {};
  globalThis.LevainDashboard = { renderPanel: () => { renders++; return new N("div"); }, measureClauses() {}, measureOverflow() {},
    setRichText: (n, t) => { n.textContent = t; } };
  const s = K.fill(p, view.extra_panels.p, { rows: () => {}, entry });
  const host = new N("div"); host.className = "pbody"; host.appendChild(s.body); host.live = true; s.body.parent = host; host.kids = [s.body];
  p.appendChild(host); s.p = p;
  globalThis.LevainCockpitTransport = { readJson: async () => globalThis.__next };
  // the same panel read later: the age digits moved, a count in the title moved
  globalThis.__next = mk("Inbox (4)", "stale", "2026-10-11T11:40:00+00:00");
  const r = await K.refetchPanel("p");
  const want = V.fromManifest({ manifest: manifest(globalThis.__next), panels: { p: globalThis.__next } }).layout.find((e) => e.id === "p");
  ok(want.banner[0].text !== bannerText, "the fixture's second read has a different age: " + want.banner[0].text);
  ok(r === "applied", "an age tick and a count in the title answer applied, not a reload: " + r);
  ok(renders === 0, "nothing was rebuilt");
  ok(h2.textContent === "Inbox (4)", "the head's title repainted: " + h2.textContent);
  ok(s.title === "Inbox (4)", "the state's title follows: " + s.title);
  ok(note.textContent === want.banner[0].text, "the banner line shows the new age: " + note.textContent);
  // a change in words is still chrome: it reloads
  globalThis.__next = mk("Other inbox (4)", "stale", "2026-10-11T11:40:00+00:00");
  ok(await K.refetchPanel("p") === false, "a title change in words still declines");
  // the banner going away is still chrome
  globalThis.__next = mk("Inbox (4)", "ok", "2026-10-11T11:40:00+00:00");
  ok(await K.refetchPanel("p") === false, "a banner that disappears still declines");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("THREW " + (e && e.stack || e)); process.exit(1); });
"""


# levain#168 fix round: a zone card's " (N)" tail lives in a sibling .ck-count pill (repaint both, never re-add the tail); a focus-view
# copy has no .phead h2 to repaint, so a title change there still reloads
DRIVER_PILL = DRIVER_CHROME_TEXT[:DRIVER_CHROME_TEXT.index("(async () => {")] + r"""
Object.defineProperty(N.prototype, "nextElementSibling", { get() { if (!this.parent) return null; const k = this.parent.kids; return k[k.indexOf(this) + 1] || null; } });
(async () => {
  const mkc = (t, n) => Object.assign(mk(t, "ok", "2026-10-11T12:00:00+00:00"), { count: n });
  const first = mkc("Tray (2)", 2);
  const view = V.fromManifest({ manifest: manifest(first), panels: { p: first } });
  const entry = view.layout.find((e) => e.id === "p");
  const p = new N("div"); p.live = true; p.className = "panel";
  const head = new N("div"); head.className = "phead"; const h2 = new N("h2"); h2.text = "Tray";
  const pill = new N("span"); pill.className = "ck-count"; pill.text = "2"; pill.hidden = false;
  head.appendChild(h2); head.appendChild(pill); p.appendChild(head);
  globalThis.LevainDashboard = { renderPanel: () => new N("div"), measureClauses() {}, measureOverflow() {}, setRichText: (n, t) => { n.textContent = t; } };
  const s = K.fill(p, view.extra_panels.p, { rows: () => {}, entry });
  const host = new N("div"); host.className = "pbody"; host.appendChild(s.body); host.live = true; s.body.parent = host; host.kids = [s.body];
  p.appendChild(host); s.p = p;
  globalThis.LevainCockpitTransport = { readJson: async () => globalThis.__next };
  globalThis.__next = mkc("Tray (3)", 3);
  const r = await K.refetchPanel("p");
  ok(r === "applied", "a count 2 -> 3 on a card with a pill answers applied: " + r);
  ok(h2.textContent === "Tray", "the heading keeps no count tail: " + h2.textContent);
  ok(pill.textContent === "3", "the pill shows the new count: " + pill.textContent);
  ok(pill.hidden === false, "the pill is shown");
  globalThis.__next = mkc("Tray (0)", 0);
  ok(await K.refetchPanel("p") === "applied", "a count to 0 applies");
  ok(h2.textContent === "Tray" && pill.textContent === "0" && pill.hidden === true, "count 0 hides the pill: " + h2.textContent + "/" + pill.textContent + "/" + pill.hidden);
  // a modal copy: its chrome is not the panel's own head, so a changed title cannot be repainted in place
  s.modal = true; head.remove(); s.title = "Tray (0)"; s.banner = [];
  globalThis.__next = mkc("Tray (4)", 4);
  ok(await K.refetchPanel("p") === false, "a modal copy whose title digits change declines (full reload)");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("THREW " + (e && e.stack || e)); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_an_age_tick_or_a_count_in_a_title_repaints_in_place(tmp_path: Path) -> None:
    drv = tmp_path / "chrome_text_driver.js"
    drv.write_text(DRIVER_CHROME_TEXT, encoding="utf-8")
    r = subprocess.run([NODE, str(drv), str(WEB / "cockpit_view.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_count_pill_repaints_and_a_modal_copy_reloads(tmp_path: Path) -> None:
    drv = tmp_path / "pill_driver.js"
    drv.write_text(DRIVER_PILL, encoding="utf-8")
    r = subprocess.run([NODE, str(drv), str(WEB / "cockpit_view.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
