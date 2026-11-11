"""The cold first board draws as its panels land (cost table #1): the manifest, then the panels the page's top is made of,
then the rest as each answers, with "connecting..." cleared on the first useful content. The pure parts (read order, the
manifest narrowed to the panels in hand, the pacing of the partial draws) are run under node; the wiring is held by
reading the boot source for the two calls that matter."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"

_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const a = src.indexOf("// BEGIN progressive draw"), b = src.indexOf("// END progressive draw");
if (a < 0 || b < 0) { console.log(JSON.stringify({ missing: true })); process.exit(0); }
const lib = new Function(src.slice(a, b) + "; return { drawOrder, manifestSubset, partialPacer, PARTIAL_GAP_MS, timedAttempt, readPanelBody, runPartial, mayDrawWhole, PANEL_TIMEOUT_MS };")();
(async () => {
const out = {};
const manifest = {
  panels: { activity: {}, state: {}, begin: {}, decisions: {}, loops: {}, tray: {}, crystals: {} },
  regions: {
    hero: ["activity"], brief: ["state", "begin"], widgets: ["decisions"], header: ["activity", "state", "begin", "decisions"],
    zones: [
      { id: "operate", panels: ["tray", "loops"], cards: [{ panels: ["tray"] }, { panels: ["loops"] }] },
      { id: "mind", panels: ["crystals"], cards: [{ panels: ["crystals"] }] },
    ],
  },
};
// the manifest lists the page's own order; a manifest with the Needs strip's panel late still reads it in the first wave
const ids = ["loops", "tray", "activity", "state", "begin", "crystals", "decisions"];
out.order = lib.drawOrder(manifest, ids);
out.noRegions = lib.drawOrder({ panels: {} }, ["a", "b"]);
const sub = lib.manifestSubset(manifest, new Set(["activity", "decisions", "tray"]));
out.sub = { panels: Object.keys(sub.panels), hero: sub.regions.hero, brief: sub.regions.brief, widgets: sub.regions.widgets,
            zones: sub.regions.zones.map((z) => [z.id, z.panels, z.cards.map((c) => c.panels)]) };
out.whole = Object.keys(manifest.panels).length === 7 && manifest.regions.zones.length === 2;   // the source manifest is not mutated

// the pacer, on a fake clock
let t = 0, timers = [];
const clock = { now: () => t, setTimeout: (f, ms) => { const h = { f, at: t + ms }; timers.push(h); return h; }, clearTimeout: (h) => { timers = timers.filter((x) => x !== h); } };
const advance = (ms) => { t += ms; for (const h of timers.filter((x) => x.at <= t)) { timers = timers.filter((x) => x !== h); h.f(); } };
const draws = [];
const p = lib.partialPacer(["a", "b"], 6, (n) => draws.push([t, n]), clock);
p.settled("x");                                  // not core
p.settled("a");                                  // core still waiting on b
out.beforeCore = draws.length;
p.settled("b");                                  // the core is in: the first draw is at once
out.atCore = draws.slice();
p.settled("c");                                  // inside the gap: one timer, no draw yet
p.settled("d");                                  // coalesced into the same timer
out.inGap = { draws: draws.length, timers: timers.length };
advance(lib.PARTIAL_GAP_MS);
out.afterGap = draws.slice();
p.settled("e");                                  // the last one (6 of 6): the full draw is the caller's
advance(1000);
out.lastNeverDraws = draws.length;

// a draw that takes long widens the gap
t = 0; timers = []; const d2 = [];
const slow = lib.partialPacer(["a"], 10, (n) => { d2.push(t); t += 200; }, clock);
slow.settled("a"); slow.settled("b");           // draw 1 takes 200 ms; the next waits 4 x 200 = 800 ms from its end
advance(799); out.slowEarly = d2.length;
advance(1); out.slowOnTime = d2.length;

// stop cancels a pending draw
t = 0; timers = []; const d3 = [];
const q = lib.partialPacer([], 10, (n) => d3.push(n), clock);
q.settled("a"); q.settled("b"); q.stop(); advance(5000);
out.stopped = { draws: d3.length, timers: timers.length };

// a draw that throws is swallowed, the pacing state stays sane and a later draw still happens
t = 0; timers = []; const d4 = []; let boom = true;
const w = lib.partialPacer([], 10, (n) => { d4.push(n); if (boom) { boom = false; throw new Error("draw failed"); } }, clock);
const quiet = console.warn; console.warn = () => {};
let threw = false;
try { w.settled("a"); } catch (e) { threw = true; }
w.settled("b"); advance(lib.PARTIAL_GAP_MS);
console.warn = quiet;
out.throwingDraw = { threw, draws: d4 };

// a zone or card with no panels list, and a null one, do not break the narrowing
const bad = { panels: { a: {} }, regions: { hero: ["a"], brief: [], widgets: [], zones: [null, { id: "z" }, { id: "y", panels: ["a"], cards: [null, { }, { panels: ["a"] }] }] } };
const bs = lib.manifestSubset(bad, new Set(["a"]));
out.malformed = bs.regions.zones.map((z) => [z.id, z.panels, z.cards.map((c) => c.panels)]);

// runPartial: a superseded or editing page never maps (the mapper keeps state); a throwing build leaves the board
let built = 0, shown = 0;
out.gateShut = [lib.runPartial(() => false, () => { built++; return 1; }, () => { shown++; }), built, shown];
console.warn = () => {};
out.buildThrows = [lib.runPartial(() => true, () => { throw new Error("x"); }, () => { shown++; }), shown];
out.showThrows = lib.runPartial(() => true, () => 1, () => { throw new Error("y"); });
console.warn = quiet;
out.gateOpen = [lib.runPartial(() => true, () => 7, (v) => { shown += v; }), shown];
out.mayDraw = [lib.mayDrawWhole(false, false, true), lib.mayDrawWhole(true, false, true), lib.mayDrawWhole(false, true, true), lib.mayDrawWhole(false, true, false)];

// timeouts per ACTUAL fetch attempt (async, on the fake clock)
const setTimeout0 = (f, ms) => clock.setTimeout(f, ms);
const sleepTick = () => new Promise((r) => setImmediate(r));
const mkFetch = (script) => (url, signal) => new Promise((res, rej) => {
  const step = script.shift();
  if (signal) signal.addEventListener("abort", () => rej(Object.assign(new Error("aborted"), { name: "AbortError" })));
  if (step.hang) return;
  const body = new Promise((bres, brej) => {
    if (signal) signal.addEventListener("abort", () => brej(Object.assign(new Error("aborted"), { name: "AbortError" })));
    if (step.bodyHang) return;                      // headers sent, the body never completes
    if (step.bodyAfter) { setTimeout0(() => bres(JSON.stringify(step.body)), step.bodyAfter); return; }
    bres(JSON.stringify(step.body));
  });
  res({ status: step.status, ok: step.status === 200, text: () => body });
});
const seen = [];
async function reauthScenario(waitMs) {
  t = 0; timers = [];
  const f = mkFetch([{ status: 403 }, { status: 200, body: { ok: 1 } }]);
  let release;
  const io = {
    attempt: (u) => lib.timedAttempt((url, sig) => { seen.push(sig); return f(url, sig); }, u, clock),
    isReject: async () => true, token: () => "t", drop: () => {},
    reauth: () => new Promise((r) => { release = () => r(true); }),
  };
  seen.length = 0;
  const pr = lib.readPanelBody("/p", io).then((v) => ({ v }), (e) => ({ err: e.message }));
  await sleepTick();
  advance(waitMs);                      // the token prompt stays open this long
  await sleepTick(); release(); await sleepTick();
  const r = await pr;
  return { r, aborted: seen.map((s) => s.aborted), pending: timers.length };
}
out.reauthShort = await reauthScenario(5000);
out.reauthLong = await reauthScenario(lib.PANEL_TIMEOUT_MS + 1000);
// a hung read is aborted at the timeout, with its own words
t = 0; timers = [];
const hung = lib.readPanelBody("/h", { attempt: (u) => lib.timedAttempt(mkFetch([{ hang: true }]), u, clock), isReject: async () => false, reauth: async () => false, token: () => "", drop: () => {} })
  .then((v) => ({ v }), (e) => ({ err: e.message }));
await sleepTick(); advance(lib.PANEL_TIMEOUT_MS - 1); await sleepTick(); out.hungEarly = timers.length;
advance(1); out.hung = await hung; out.hungTimers = timers.length;
// headers answered but the body stalls: timed out like any hung read (the failure of a timer cleared at the headers)
for (const [name, step] of [["bodyHung", { status: 200, bodyHang: true }], ["bodyHung403", { status: 403, bodyHang: true }]]) {
  t = 0; timers = [];
  const pr = lib.readPanelBody("/b", { attempt: (u) => lib.timedAttempt(mkFetch([step]), u, clock), isReject: async (r) => (await r.json(), true), reauth: async () => false, token: () => "", drop: () => {} })
    .then((v) => ({ v }), (e) => ({ err: e.message }));
  await sleepTick(); advance(lib.PANEL_TIMEOUT_MS - 1); await sleepTick(); out[name + "Early"] = timers.length;
  advance(1); out[name] = await pr; out[name + "Timers"] = timers.length;
}
// a slow body that finishes inside the window succeeds, and its timer is gone
t = 0; timers = [];
const slowBody = lib.readPanelBody("/s", { attempt: (u) => lib.timedAttempt(mkFetch([{ status: 200, bodyAfter: 20000, body: { slow: 1 } }]), u, clock), isReject: async () => false, reauth: async () => false, token: () => "", drop: () => {} })
  .then((v) => ({ v }), (e) => ({ err: e.message }));
await sleepTick(); advance(20000); await sleepTick(); out.slowBody = await slowBody; out.slowBodyTimers = timers.length;
console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_first_board_reads_its_top_first_narrows_the_manifest_and_paces_its_partial_draws(tmp_path: Path) -> None:
    driver = tmp_path / "drive.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    run = subprocess.run([shutil.which("node"), str(driver), str(WEB / "dashboard_boot.js")],
                         capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(run.stdout)
    assert not out.get("missing"), "the boot has no progressive draw block"
    # hero, brief, then the Needs strip's panel come first, the rest keep the manifest's order
    assert out["order"] == {"core": ["activity", "state", "begin", "decisions"],
                            "order": ["activity", "state", "begin", "decisions", "loops", "tray", "crystals"]}
    assert out["noRegions"] == {"core": [], "order": ["a", "b"]}
    # the board in hand: only the panels read, a zone or card with none left is gone
    assert out["sub"] == {"panels": ["activity", "decisions", "tray"], "hero": ["activity"], "brief": [], "widgets": ["decisions"],
                          "zones": [["operate", ["tray"], [["tray"]]]]}
    assert out["whole"] is True
    # nothing draws before the core is in, the core's arrival draws at once, later landings coalesce within the gap
    assert out["beforeCore"] == 0
    assert out["atCore"] == [[0, 3]]
    assert out["inGap"] == {"draws": 1, "timers": 1}
    assert out["afterGap"] == [[0, 3], [250, 5]]
    assert out["lastNeverDraws"] == 2
    assert (out["slowEarly"], out["slowOnTime"]) == (1, 2)
    assert out["stopped"] == {"draws": 1, "timers": 0}
    # a partial draw that throws never reaches the read that landed it, and the pacer keeps going
    assert out["throwingDraw"] == {"threw": False, "draws": [1, 2]}
    # a zone or card with no panels list, or a null one, narrows to nothing instead of throwing
    assert out["malformed"] == [["y", ["a"], [["a"]]]]
    # a superseded or editing page never runs the (stateful) mapping; a throwing mapping or draw leaves the board alone
    assert out["gateShut"] == [False, 0, 0]
    assert out["buildThrows"] == [False, 0]
    assert out["showThrows"] is False
    assert out["gateOpen"] == [True, 7]
    # the whole board's draw defers to an open edit once a partial board was drawn, as a passive one always did
    assert out["mayDraw"] == [True, False, False, True]
    # the 30 s timer belongs to one fetch attempt: a token prompt held past it does not expire the retry
    assert out["reauthShort"] == {"r": {"v": {"ok": 1}}, "aborted": [False, False], "pending": 0}
    assert out["reauthLong"] == {"r": {"v": {"ok": 1}}, "aborted": [False, False], "pending": 0}
    # a hung read is aborted at the timeout and says so; the timer is gone after
    assert out["hungEarly"] == 1
    assert out["hung"] == {"err": "no answer in 30 s"} and out["hungTimers"] == 0
    # headers answered, body stalled: the same timeout (200 and a 403 whose body is read for the token check); a slow body inside it succeeds
    for k in ("bodyHung", "bodyHung403"):
        assert out[k + "Early"] == 1 and out[k] == {"err": "no answer in 30 s"} and out[k + "Timers"] == 0
    assert out["slowBody"] == {"v": {"slow": 1}} and out["slowBodyTimers"] == 0


# The WHOLE boot under node, with the real view mapper, a fake server and a fake page: a read that fails AFTER a partial board was drawn.
# (L1 finding 1: the failure released the part and set the "whole" signal but queued no retry, because the first partial draw had already
# latched `everDrawn`; the reader saw a live-looking board with "read failed" in the line until they pressed Sync.) Only the retry timers
# (the boot's own 2/4/8/15 s backoff) are faked: the test fires them by hand and records their delays.
_PAGE = r"""
const fs = require("fs");
const View = require(process.argv[2]);
const bootSrc = fs.readFileSync(process.argv[3], "utf8");
const REGIONS = ["ck-hero", "ck-needs", "ck-notes", "ck-brief", "ck-widgets", "state", "board"];
const real = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
const sleep = (ms) => new Promise((r) => real.setTimeout(r, ms));
const settle = () => sleep(80);
const RETRY = new Set([2000, 4000, 8000, 15000]);

function head(id, kind) { return { id, kind, title: id, status: "ok", as_of: "2026-10-11T12:00:00+00:00" }; }
function fixture() {
  const manifest = { schema: "levain.cockpit/1", entity: { name: "t", governance: "reported", brand: {} }, generated_at: "2026-10-11T12:00:00+00:00",
    regions: { header: ["state"], zones: [{ id: "operate", title: "Operate", panels: ["loops", "decisions"] }] },
    panels: { state: head("state", "line"), loops: head("loops", "triage-list"), decisions: head("decisions", "triage-list") },
    verbs: {}, errors: [], transport: { writable: true, write_token_required: false } };
  const panels = {
    state: Object.assign(head("state", "line"), { value: { lines: [{ label: "state", text: "hi", at: "2026-10-11T11:00:00.000Z", source: "web" }], value_version: "sv1" }, actions: [] }),
    loops: Object.assign(head("loops", "triage-list"), { rows: [], actions: [] }),
    decisions: Object.assign(head("decisions", "triage-list"), { groups: [], rows: [], actions: [] }),
  };
  return { manifest, panels };
}

// opts: holdLoops (the first read of the `loops` panel answers only when release() is called), failManifest (that many manifest reads answer 500),
// throwOnRender (the render call numbers, 1-based, that throw once each)
function makeEnv(opts) {
  opts = opts || {};
  const fx = fixture();
  const renders = [], marks = [], timers = [], listeners = {};
  let loopsReads = 0, failManifest = opts.failManifest || 0, release = null;
  const held = new Promise((r) => { release = r; });
  const resp = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body, text: async () => JSON.stringify(body), headers: { get: () => null }, clone() { return this; } });
  async function fetchFn(url) {
    if (url.indexOf("/cockpit/manifest.json") >= 0) { if (failManifest > 0) { failManifest--; return resp(500, {}); } return resp(200, fx.manifest); }
    const m = /\/cockpit\/panel\/([^/.]+)\.json/.exec(url);
    if (m && fx.panels[m[1]]) {
      if (m[1] === "loops" && opts.holdLoops && loopsReads++ === 0) await held;
      return resp(200, fx.panels[m[1]]);
    }
    return resp(404, {});
  }
  const attrs = {};
  const node = (id) => { const n = { id, textContent: "", hidden: false, dataset: {}, a: {}, setAttribute(k, v) { this.a[k] = String(v); }, removeAttribute(k) { delete this.a[k]; }, hasAttribute(k) { return k in this.a; } }; return n; };
  const store = node("store"), stamp = node("stamp"), regions = {};
  for (const id of REGIONS) regions[id] = node(id);
  const html = node("html");
  const ls = { levain_write_token: "tok" };
  const localStorage = { getItem: (k) => (k in ls ? ls[k] : null), setItem: (k, v) => { ls[k] = String(v); }, removeItem: (k) => { delete ls[k]; } };
  const sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  let renderN = 0;
  const win = { LevainCockpitView: View, localStorage, sessionStorage, location: { search: "?source=manifest", hash: "", pathname: "/" },
    crypto: { randomUUID: () => "k" }, prompt: () => "",
    LevainDashboard: { render: (view, o) => { renderN++; if ((opts.throwOnRender || []).indexOf(renderN) >= 0) throw new Error("render " + renderN + " threw"); renders.push(view); }, hasUnsavedEdit: () => false, jobsActive: () => false },
    dispatchEvent() {} };
  const doc = { getElementById: (id) => (id === "store" ? store : id === "stamp" ? stamp : regions[id] || null), documentElement: html, dispatchEvent() {},
    addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); }, querySelectorAll: () => [], createElement: () => ({ className: "", textContent: "" }),
    visibilityState: "visible", body: { dataset: {} } };
  const fakeSet = (f, ms) => { if (RETRY.has(ms)) { const h = { fake: true, f, ms }; timers.push(h); return h; } return real.setTimeout(f, ms); };
  const fakeClear = (h) => { if (h && h.fake) { const i = timers.indexOf(h); if (i >= 0) timers.splice(i, 1); } else real.clearTimeout(h); };
  const env = { renders, timers, listeners, store, stamp, regions, html, release, fx, armFail: (n) => { failManifest = n; } };
  env.start = async () => {
    new Function("window", "document", "location", "history", "fetch", "console", "setTimeout", "clearTimeout", bootSrc)(win, doc, win.location, { replaceState() {} }, fetchFn, console, fakeSet, fakeClear);
    await settle();
    return env;
  };
  env.whole = () => html.hasAttribute("data-board-whole");
  env.inert = () => REGIONS.filter((id) => regions[id].hasAttribute("inert")).length;
  env.fire = async () => { const h = timers.shift(); if (h) h.f(); await settle(); return h ? h.ms : null; };
  env.snap = () => ({ renders: renders.length, whole: env.whole(), inert: env.inert(), timers: timers.map((t) => t.ms), store: store.textContent, stamp: stamp.textContent });
  return env;
}

(async () => {
  const out = {};
  const quiet = console.warn; console.warn = () => {};

  // control: a read that fails nowhere draws a part, then the whole board, and queues no retry at all
  { const e = await makeEnv({ holdLoops: true }).start();
    out.controlPart = e.snap(); e.release(); await settle(); out.controlWhole = e.snap(); }

  // A: a part is drawn (the rest of the panels still out), the page is foregrounded (that read is superseded) and the NEW read's manifest
  // fails once. Then the retry timer fires: it must draw the whole board, once, and the chain must end there.
  { const e = await makeEnv({ holdLoops: true }).start();
    out.aPart = e.snap();
    e.armFail(1);
    e.listeners["levain:resume"][0]({ detail: { hiddenAt: Date.now() + 5000 } });
    await settle();
    out.aFailed = e.snap();
    out.aRetryDelay = await e.fire();
    out.aRetried = e.snap();
    e.release(); await settle();                 // the superseded read's late panel answers into a dead generation
    out.aLate = e.snap(); }

  // B: a part is drawn, the whole board's draw throws ONCE (the second render call), then the retry draws it
  { const e = await makeEnv({ holdLoops: true, throwOnRender: [2] }).start();
    out.bPart = e.snap();
    e.release(); await settle();
    out.bFailed = e.snap();
    out.bRetryDelay = await e.fire();
    out.bRetried = e.snap(); }

  // C: what paces the chain. A server that keeps failing: the delays back off 2, 4, 8, 15 s and then hold at 30 s (the table's last entry, which
  // is also the panel timeout, so it is a real timer here and shows as no fake one); only a whole draw (or a refusal) ends it
  { const e = await makeEnv({ holdLoops: true }).start();
    e.armFail(99);
    e.listeners["levain:resume"][0]({ detail: { hiddenAt: Date.now() + 5000 } });
    await settle();
    out.cDelays = [];
    for (let i = 0; i < 4; i++) { out.cDelays.push(e.timers.map((t) => t.ms)[0]); await e.fire(); }
    out.cAfter = e.snap(); }
  console.warn = quiet;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_read_that_fails_after_a_partial_board_is_retried_until_a_whole_board_is_drawn(tmp_path: Path) -> None:
    """L1 finding 1. The failure releases the part and sets the signal (a page left inert forever on a failing server is worse), but the
    retry chain must still run: no WHOLE board has been drawn, so the reader is looking at a part under a live-looking page."""
    driver = tmp_path / "page.js"
    driver.write_text(_PAGE, encoding="utf-8")
    run = subprocess.run([shutil.which("node"), str(driver), str(WEB / "cockpit_view.js"), str(WEB / "dashboard_boot.js")],
                         capture_output=True, text=True, timeout=60, check=True)
    out = json.loads(run.stdout)
    # control: nothing fails, so a part is drawn inert and unsigned, then the whole board is drawn live and signed, with no retry ever queued
    cp = out["controlPart"]
    assert (cp["renders"], cp["whole"], cp["inert"], cp["timers"]) == (1, False, 7, []), cp
    assert (out["controlWhole"]["renders"], out["controlWhole"]["whole"], out["controlWhole"]["inert"], out["controlWhole"]["timers"]) == (2, True, 0, []), out["controlWhole"]
    # A: supersede + a failed manifest read after a part: released and signed (kept), AND a retry is queued
    assert (out["aPart"]["renders"], out["aPart"]["whole"], out["aPart"]["inert"]) == (1, False, 7), out["aPart"]
    assert out["aFailed"]["whole"] is True and out["aFailed"]["inert"] == 0 and out["aFailed"]["renders"] == 1, out["aFailed"]
    assert out["aFailed"]["timers"] == [2000], out["aFailed"]               # RED before the fix: [] (everDrawn latched by the part)
    assert "can't reach the cockpit yet" in out["aFailed"]["store"], out["aFailed"]
    # the retry draws the whole board once, the line is clean, nothing else is queued, and the superseded read's late panel changes nothing
    assert out["aRetryDelay"] == 2000
    assert (out["aRetried"]["renders"], out["aRetried"]["whole"], out["aRetried"]["inert"], out["aRetried"]["timers"], out["aRetried"]["store"]) == (2, True, 0, [], ""), out["aRetried"]
    assert out["aLate"]["renders"] == 2 and out["aLate"]["timers"] == [], out["aLate"]
    # B: the whole board's draw throws once after a part: released, signed, retried, and the retry lands
    assert (out["bPart"]["renders"], out["bPart"]["whole"]) == (1, False), out["bPart"]
    assert out["bFailed"]["whole"] is True and out["bFailed"]["inert"] == 0 and out["bFailed"]["timers"] == [2000], out["bFailed"]
    assert out["bRetryDelay"] == 2000
    assert (out["bRetried"]["renders"], out["bRetried"]["whole"], out["bRetried"]["timers"], out["bRetried"]["store"]) == (2, True, [], ""), out["bRetried"]
    # C: a server that keeps failing is retried on the boot's own backoff (2, 4, 8, 15 s, then 30 s), never in a tight loop, and the page is left live
    assert out["cDelays"] == [2000, 4000, 8000, 15000], out["cDelays"]
    assert out["cAfter"]["whole"] is True and out["cAfter"]["inert"] == 0, out["cAfter"]


def test_the_cold_load_draws_partial_boards_and_a_panel_read_is_bounded() -> None:
    boot = (WEB / "dashboard_boot.js").read_text(encoding="utf-8")
    # only a page that has drawn nothing yet draws partial boards; a refresh keeps its board until the new one is whole
    assert "const onPartial = everDrawn ? null :" in boot
    assert "loadManifestView(passive, onPartial)" in boot
    # the cold placeholder goes with the first useful content, not after the last panel
    assert re.search(r'removeAttribute\("data-cold"\);\s*drawnOnce\(\);', boot)
    # a panel that does not answer is its own failure state, and frees its read slot
    assert "timedAttempt(fetchPanel, url, clock)" in boot and "mayDrawWhole(passive, partialDrawn" in boot
