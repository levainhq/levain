"""CK4: the change stream's client logic, run in node against a small fake window: a faulted refetch is
not success and is retried on a bounded schedule; a declined refetch makes a reload PENDING that survives a
deferred reload; a hidden tab gives its stream back and a visible one names the last event it saw."""

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
let failed = 0;
const ok = (c, w) => { if (!c) { failed++; console.log("FAIL " + w); } };
const tick = () => new Promise((r) => setImmediate(r));
const settle = async () => { for (let i = 0; i < 8; i++) await tick(); };

function boot(opts) {
  const timers = [];
  const listeners = {};
  const fetches = [], manifests = [];
  const doc = { readyState: "complete", hidden: !!opts.hidden, querySelector: () => null,
    createElement: () => ({ dataset: {}, setAttribute() {}, isConnected: true }),
    addEventListener: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); } };
  const win = {
    LevainCockpitHeld: opts.undrawn ? undefined : { a: { etag: "1" }, b: { etag: "1" } },
    location: { search: "" }, document: doc,
    setTimeout: (fn, ms) => { const t = { fn, ms, off: false }; timers.push(t); return t; },
    clearTimeout: (t) => { if (t) t.off = true; },
    setInterval: (fn, ms) => { const t = { fn, ms, off: false, every: true }; timers.push(t); return t; },
    clearInterval: (t) => { if (t) t.off = true; },
    AbortController: class { constructor() { this.signal = {}; this.aborted = false; } abort() { this.aborted = true; } },
    fetch: (url, o) => {
      if (url === "/cockpit/manifest.json") {
        manifests.push({ url, headers: o.headers });
        const m = opts.manifest ? opts.manifest() : { panels: { a: { etag: "1" }, b: { etag: "1" } } };
        return Promise.resolve({ ok: true, status: 200, headers: { get: () => "W/m" }, json: () => Promise.resolve(m) });
      }
      const f = { url, headers: o.headers, ctl: null, push: null };
      fetches.push(f);
      let wake = null, q = [];
      f.push = (text) => { q.push(text); if (wake) { const w = wake; wake = null; w(); } };
      return Promise.resolve({ ok: true, status: 200, body: { getReader: () => ({ read: () => {
        const next = () => q.length ? Promise.resolve({ done: false, value: Buffer.from(q.shift()) }) : new Promise((r) => { wake = r; }).then(next);
        return next();
      } }) } });
    },
  };
  win.LevainCockpitTransport = { headers: () => ({ "X-T": "tok" }), reload: () => Promise.resolve(false), readJson: () => Promise.resolve({}) };
  win.LevainCockpitView = { refetchPanel: async () => "applied" };
  globalThis.window = win; globalThis.document = doc; globalThis.fetch = win.fetch; globalThis.AbortController = win.AbortController; globalThis.URLSearchParams = URLSearchParams;
  new Function("window", "document", src)(win, doc);
  return { win, doc, timers, listeners, fetches, manifests, live: () => timers.filter((t) => !t.off) };
}
const frame = (event, id, data) => "id: " + id + "\nevent: " + event + "\ndata: " + JSON.stringify(data) + "\n\n";

(async () => {
  // ---- a faulted refetch is not success: it is retried on the bounded schedule, then it waits ----
  {
    const h = boot({});
    const calls = [];
    h.win.LevainCockpitView.refetchPanel = async (id) => { calls.push(id); return "fault"; };
    await settle();
    ok(h.fetches.length === 1, "the stream connected once drawn");
    h.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: false }));
    h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "2" }));
    await settle();
    ok(calls.join() === "a", "the event refetched the panel: " + calls);
    ok(h.live().some((t) => t.ms === 5000), "a fault schedules a 5 s retry");
    ok(!h.live().some((t) => t.ms === 500), "a fault does not ask for a reload");
    for (const ms of [5000, 15000, 60000]) {
      const t = h.live().find((x) => x.ms === ms);
      ok(!!t, "retry step " + ms);
      if (!t) break;
      t.off = true; t.fn(); await settle();
    }
    ok(calls.length === 4, "three retries after the first read: " + calls.length);
    ok(!h.live().some((t) => [5000, 15000, 60000].includes(t.ms)), "after the schedule it waits for the next event");
  }
  // ---- a fault that heals clears its retry ----
  {
    const h = boot({});
    let n = 0;
    h.win.LevainCockpitView.refetchPanel = async () => (++n === 1 ? "fault" : "applied");
    await settle();
    h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "2" }));
    await settle();
    const t = h.live().find((x) => x.ms === 5000);
    t.off = true; t.fn(); await settle();
    ok(n === 2 && !h.live().some((x) => x.ms === 15000), "a healed retry schedules nothing further");
  }
  // ---- a declined refetch is a PENDING reload: it survives deferral and clears only on a render ----
  {
    const h = boot({});
    h.win.LevainCockpitView.refetchPanel = async () => false;
    let reloads = 0, result = false;
    h.win.LevainCockpitTransport.reload = () => { reloads++; return Promise.resolve(result); };
    await settle();
    h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "2" }));
    await settle();
    let t = h.live().find((x) => x.ms === 500);
    ok(!!t, "a declined refetch asks for a reload");
    t.off = true; t.fn(); await settle();
    ok(reloads === 1, "the reload was attempted");
    t = h.live().find((x) => x.ms === 5000);
    ok(!!t, "a deferred reload (false) stays pending and retries: " + JSON.stringify(h.timers.map((x) => x.ms)));
    t.off = true; t.fn(); await settle();
    ok(reloads === 2, "and it retried");
    t = h.live().find((x) => x.ms === 15000);
    ok(!!t, "with a longer wait each time");
    result = true;
    t.off = true; t.fn(); await settle();
    ok(reloads === 3 && h.live().filter((x) => !x.every && [500, 5000, 15000, 60000].includes(x.ms)).length === 0, "a render clears it");
    // a fresh event after the clear starts over
    h.fetches[0].push(frame("manifest-changed", "E.1:2", {}));
    await settle();
    ok(h.live().some((x) => x.ms >= 500 && x.ms <= 10000), "manifest-changed asks for a reload again");
  }
  // ---- the hidden edge: the stream is given back; visible reconnects naming the last event ----
  {
    const h = boot({});
    await settle();
    h.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: false }));
    h.fetches[0].push(frame("panel-changed", "E.1:7", { id: "a", etag: "2" }));
    await settle();
    ok(h.fetches[0].headers["X-T"] === "tok" && !h.fetches[0].headers["Last-Event-ID"], "the first connect carries the credential and no position");
    h.doc.hidden = true;
    h.listeners.visibilitychange.forEach((f) => f());
    await settle();
    ok(h.fetches.length === 1 && !h.live().some((t) => t.ms === 1000), "hidden: no reconnect is scheduled");
    h.doc.hidden = false;
    h.listeners.visibilitychange.forEach((f) => f());
    await settle();
    ok(h.fetches.length === 2, "visible: a new stream opens");
    ok(h.fetches[1].headers["Last-Event-ID"] === "E.1:7", "and names the last event seen: " + JSON.stringify(h.fetches[1].headers));
  }
  // ---- a page hidden at load does not connect until it is shown ----
  {
    const h = boot({ hidden: true });
    await settle();
    ok(h.fetches.length === 0, "a hidden page holds no connection");
    h.doc.hidden = false;
    h.listeners.visibilitychange.forEach((f) => f());
    await settle();
    ok(h.fetches.length === 1, "shown: it connects");
  }
  // ---- the first-draw wait is bounded: a page that never draws says it is not live ----
  {
    const h = boot({ undrawn: true });
    await settle();
    ok(h.fetches.length === 0, "no stream before a draw");
    const waits = h.live().filter((t) => t.ms === 200);
    ok(waits.length === 1, "it polls for the draw");
    const realNow = Date.now;
    Date.now = () => realNow() + 31000;
    waits[0].off = true; waits[0].fn(); await settle();
    Date.now = realNow;
    ok(h.live().filter((t) => t.ms === 200).length === 0, "past the bound it stops waiting");
  }
  // ---- a first hello (no position) reconciles once through the manifest and refetches only what moved ----
  {
    const h = boot({ manifest: () => ({ panels: { a: { etag: "1" }, b: { etag: "2" } } }) });
    const calls = [];
    h.win.LevainCockpitView.refetchPanel = async (id) => { calls.push(id); return "applied"; };
    await settle();
    h.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: false }));
    await settle();
    ok(h.manifests.length === 1, "one manifest fetch on the first hello: " + h.manifests.length);
    ok(h.manifests[0].headers["X-T"] === "tok" && !h.manifests[0].headers["If-None-Match"], "through the page's credential, unconditional");
    ok(calls.join() === "b", "only the moved panel is refetched: " + calls);
    ok(h.win.LevainCockpitHeld.b.etag === "2", "applied records the etag from the manifest");
  }
  // ---- a replay hello (a position, no resync) does not reconcile; a resync hello does ----
  {
    const h = boot({});
    await settle();
    h.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: false }));
    h.fetches[0].push(frame("panel-changed", "E.1:5", { id: "a", etag: "1" }));
    await settle();
    h.doc.hidden = true; h.listeners.visibilitychange.forEach((f) => f()); await settle();
    h.doc.hidden = false; h.listeners.visibilitychange.forEach((f) => f()); await settle();
    const before = h.manifests.length;
    h.fetches[1].push(frame("hello", "E.1:5", { epoch: "E.1", resync: false }));
    await settle();
    ok(h.manifests.length === before, "a replay hello reads no manifest");
    h.fetches[1].push(frame("hello", "E.2:0", { epoch: "E.2", resync: true }));
    await settle();
    ok(h.manifests.length === before + 1, "a resync hello reads the manifest");
  }
  // ---- a manifest whose id set differs asks for a reload ----
  {
    const h = boot({ manifest: () => ({ panels: { a: { etag: "1" }, b: { etag: "1" }, c: { etag: "1" } } }) });
    const calls = [];
    h.win.LevainCockpitView.refetchPanel = async (id) => { calls.push(id); return "applied"; };
    await settle();
    h.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: false }));
    await settle();
    ok(h.live().some((t) => t.ms === 500), "an added panel makes a reload pending");
    ok(calls.length === 0, "and refetches nothing");
  }
  // ---- a removed panel (fewer ids, none new) asks for a reload too ----
  {
    const h = boot({ manifest: () => ({ panels: { a: { etag: "1" } } }) });
    await settle();
    h.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: false }));
    await settle();
    ok(h.live().some((t) => t.ms === 500), "a removed panel makes a reload pending");
  }
  // ---- a draw after the wait gave up starts the stream ----
  {
    const h = boot({ undrawn: true });
    await settle();
    const realNow = Date.now;
    Date.now = () => realNow() + 31000;
    h.live().filter((t) => t.ms === 200)[0].fn(); await settle();
    Date.now = realNow;
    ok(h.fetches.length === 0, "gave up: no stream");
    h.win.LevainCockpitHeld = { a: { etag: "1" }, b: { etag: "1" } };
    (h.listeners["levain:cockpit-held"] || []).forEach((f) => f());
    await settle();
    ok(h.fetches.length === 1, "a later draw starts the stream");
  }
  // ---- applied records the event's etag in held ----
  {
    const h = boot({});
    await settle();
    h.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: false }));
    h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "9" }));
    await settle();
    ok(h.win.LevainCockpitHeld.a.etag === "9", "held carries the applied etag: " + h.win.LevainCockpitHeld.a.etag);
  }
  // ---- a reload wanted during a reload is not lost when that reload returns true ----
  {
    const h = boot({});
    let release = null;
    h.win.LevainCockpitView.refetchPanel = async () => false;
    h.win.LevainCockpitTransport.reload = () => new Promise((r) => { release = r; });
    await settle();
    h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "2" }));
    await settle();
    const t = h.live().find((x) => x.ms === 500);
    t.off = true; t.fn(); await settle();
    ok(!!release, "the reload is in flight");
    h.fetches[0].push(frame("manifest-changed", "E.1:2", {}));
    await settle();
    release(true); await settle();
    ok(h.live().some((x) => !x.every && x.ms >= 500 && x.ms <= 10000), "after that reload returns, a reload is still scheduled");
  }
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("THREW " + (e && e.stack || e)); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_stream_client_logic(tmp_path: Path) -> None:
    drv = tmp_path / "stream_client_driver.js"
    drv.write_text(DRIVER, encoding="utf-8")
    r = subprocess.run([NODE, str(drv), str(WEB / "cockpit_stream.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
