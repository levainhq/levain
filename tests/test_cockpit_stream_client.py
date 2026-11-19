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
      let wake = null, q = [], broken = false;
      f.push = (text) => { q.push(text); if (wake) { const w = wake; wake = null; w(); } };
      f.fail = () => { broken = true; if (wake) { const w = wake; wake = null; w(); } };
      return Promise.resolve({ ok: true, status: 200, body: { getReader: () => ({ read: () => {
        const next = () => broken ? Promise.reject(new Error("dropped")) : q.length ? Promise.resolve({ done: false, value: Buffer.from(q.shift()) }) : new Promise((r) => { wake = r; }).then(next);
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
    // the view records the etag its own panel read returned, as the real refetchPanel does
    h.win.LevainCockpitView.refetchPanel = async (id) => { calls.push(id); h.win.LevainCockpitHeld[id].etag = "own-" + id; return "applied"; };
    await settle();
    h.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: false }));
    await settle();
    ok(h.manifests.length === 1, "one manifest fetch on the first hello: " + h.manifests.length);
    ok(h.manifests[0].headers["X-T"] === "tok" && !h.manifests[0].headers["If-None-Match"], "through the page's credential, unconditional");
    ok(calls.join() === "b", "only the moved panel is refetched: " + calls);
    ok(h.win.LevainCockpitHeld.b.etag === "own-b", "the etag held is the one the panel read returned");
  }
  // ---- a superseded refetch clears no fault: a newer one's retry survives the older answer ----
  {
    const h = boot({});
    const gates = [];
    h.win.LevainCockpitView.refetchPanel = (id) => { const k = gates.length; if (k === 0) return new Promise((res) => gates.push(res)); gates.push(null); return Promise.resolve("fault"); };
    await settle();
    h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "2" }));
    await settle();
    h.fetches[0].push(frame("panel-changed", "E.1:2", { id: "a", etag: "3" }));
    await settle();
    ok(h.live().some((x) => x.ms === 5000), "the newer refetch's fault scheduled a retry");
    gates[0]("superseded");
    await settle();
    ok(h.live().some((x) => x.ms === 5000), "a superseded answer does not clear the newer fault's retry");
  }
  // ---- an announced etag is another credential's: the stream never stores it ----
  {
    const h = boot({});
    h.win.LevainCockpitView.refetchPanel = async () => "applied";
    await settle();
    h.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: false }));
    h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "class-none-etag" }));
    await settle();
    ok(h.win.LevainCockpitHeld.a.etag === "1", "an event's etag is not planted: " + h.win.LevainCockpitHeld.a.etag);
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
  // ---- a fault owed across hide/show is retried when the tab is visible again ----
  {
    const h = boot({});
    await settle();
    const retried = [];
    h.win.LevainCockpitView.refetchPanel = async () => "fault";
    h.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: false }));
    h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "7" }));
    h.fetches[0].push(frame("panel-changed", "E.1:2", { id: "b", etag: "8" }));
    await settle();
    h.win.LevainCockpitView.refetchPanel = async (id) => { retried.push(id); return "applied"; };
    h.doc.hidden = true; h.listeners.visibilitychange.forEach((f) => f()); await settle();
    h.doc.hidden = false; h.listeners.visibilitychange.forEach((f) => f()); await settle();
    ok(retried.sort().join() === "a,b", "owed retries run for both panels: " + retried);
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
  // ---- a panel that stays unavailable costs the schedule's reads, not a board read every step for as long as the page is open ----
  {
    const h = boot({});
    h.win.LevainCockpitHeld.a.err = "unavailable";
    let reloads = 0, reads = 0;
    // an unavailable panel has no node to redraw in place: the page's refetchPanel makes ONE read of it and answers "fault" while that read fails
    // (never false without a read, which asked for a whole reload at every step)
    h.win.LevainCockpitView.refetchPanel = async () => { reads++; return "fault"; };
    // a reload draws the whole board again: the page says so, and the panel is still unavailable
    h.win.LevainCockpitTransport.reload = () => { reloads++; (h.listeners["levain:cockpit-held"] || []).forEach((f) => f()); return Promise.resolve(true); };
    await settle();
    const fire = async () => {
      const t = h.live().find((x) => !x.every && x.ms !== 8000 && x.ms !== 32000);
      if (!t) return false;
      t.off = true; t.fn(); await settle(); return true;
    };
    (h.listeners["levain:cockpit-held"] || []).forEach((f) => f());
    for (let i = 0; i < 40 && await fire(); i++) { /* fake time: every pending timer fires in turn */ }
    ok(reloads === 0, "an always-unavailable panel is read once per retry step and never reloads the board: " + reloads);
    ok(reads === 3, "the schedule's three single-panel reads, then it waits: " + reads);
    // it recovers on a draw: its count starts over, so the next failure gets the first step again
    delete h.win.LevainCockpitHeld.a.err;
    (h.listeners["levain:cockpit-held"] || []).forEach((f) => f());
    h.win.LevainCockpitHeld.a.err = "unavailable";
    (h.listeners["levain:cockpit-held"] || []).forEach((f) => f());
    ok(h.live().some((x) => x.ms === 5000), "a panel that recovered starts the schedule over");
  }
  // ---- a panel unreadable at the whole draw whose retry read lands: refetchPanel answers false (only a whole draw can mount it), ONE reload ----
  {
    const h = boot({});
    h.win.LevainCockpitHeld.a.err = "unavailable";
    let reloads = 0, reads = 0;
    h.win.LevainCockpitView.refetchPanel = async () => { reads++; return reads < 2 ? "fault" : false; };
    h.win.LevainCockpitTransport.reload = () => { reloads++; delete h.win.LevainCockpitHeld.a.err; (h.listeners["levain:cockpit-held"] || []).forEach((f) => f()); return Promise.resolve(true); };
    await settle();
    const fire = async () => {
      const t = h.live().find((x) => !x.every && x.ms !== 8000 && x.ms !== 32000);
      if (!t) return false;
      t.off = true; t.fn(); await settle(); return true;
    };
    (h.listeners["levain:cockpit-held"] || []).forEach((f) => f());
    for (let i = 0; i < 40 && await fire(); i++) { /* fake time */ }
    ok(reads === 2 && reloads === 1, "one failed read, then the read that lands asks for the one whole draw: reads " + reads + " reloads " + reloads);
  }
  // ---- a new event for an exhausted panel starts its schedule over ----
  {
    const h = boot({});
    h.win.LevainCockpitView.refetchPanel = async () => "fault";
    await settle();
    h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "2" }));
    await settle();
    for (const ms of [5000, 15000, 60000]) { const t = h.live().find((x) => x.ms === ms); t.off = true; t.fn(); await settle(); }
    ok(!h.live().some((t) => t.ms === 5000), "exhausted");
    h.fetches[0].push(frame("panel-changed", "E.1:2", { id: "a", etag: "3" }));
    await settle();
    ok(h.live().some((t) => t.ms === 5000), "a new cause starts the schedule over");
  }
  // ---- a fault scheduled while the tab is hidden is owed, not lost to a timer that runs unseen ----
  {
    const h = boot({});
    await settle();
    h.doc.hidden = true; h.listeners.visibilitychange.forEach((f) => f()); await settle();
    h.win.LevainCockpitHeld.a.err = "unavailable";
    (h.listeners["levain:cockpit-held"] || []).forEach((f) => f());
    ok(!h.live().some((t) => t.ms === 5000), "paused: no retry timer runs");
    const retried = [];
    h.win.LevainCockpitView.refetchPanel = async (id) => { retried.push(id); return "applied"; };
    h.doc.hidden = false; h.listeners.visibilitychange.forEach((f) => f()); await settle();
    ok(retried.join() === "a", "the owed panel is read once when the tab returns: " + retried);
  }
  // ---- reconciliation is not a cause: a never-read panel under a stream-down poll whose manifest keeps changing reads the board the schedule's times, not once per change ----
  {
    let n = 0;
    const h = boot({ manifest: () => ({ panels: { a: { etag: "m" + (++n) }, b: { etag: "1" } } }) });
    delete h.win.LevainCockpitHeld.a.etag;
    h.win.LevainCockpitHeld.a.err = "unavailable";
    let reloads = 0;
    h.win.LevainCockpitView.refetchPanel = async (id) => (id === "a" ? false : "applied");
    h.win.LevainCockpitTransport.reload = () => { reloads++; (h.listeners["levain:cockpit-held"] || []).forEach((f) => f()); return Promise.resolve(true); };
    await settle();
    h.fetches[0].push(frame("hello", "E.1:0", { epoch: "E.1", resync: true }));
    await settle();
    h.fetches[0].fail();
    await settle();
    const poll = h.live().find((x) => x.every);
    ok(!!poll, "the stream is down: the manifest poll runs");
    (h.listeners["levain:cockpit-held"] || []).forEach((f) => f());
    for (let i = 0; i < 40 && poll; i++) {
      poll.fn(); await settle();
      for (const t of h.live().filter((x) => !x.every && ![1000, 8000, 32000].includes(x.ms))) { t.off = true; t.fn(); await settle(); }
    }
    ok(n >= 30, "the manifest changed on every poll: " + n);
    ok(reloads <= 3, "reconciliation reloads the board at most the retry steps, not once per change: " + reloads);
  }
  // ---- the visible edge reads an owed panel without giving it a fresh budget ----
  {
    const h = boot({});
    const calls = [];
    h.win.LevainCockpitView.refetchPanel = async (id) => { calls.push(id); return "fault"; };
    await settle();
    h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "2" }));
    await settle();
    let t = h.live().find((x) => x.ms === 5000);
    t.off = true; t.fn(); await settle();
    ok(h.live().some((x) => x.ms === 15000), "two steps taken, the third (15 s) is pending");
    h.doc.hidden = true; h.listeners.visibilitychange.forEach((f) => f()); await settle();
    h.doc.hidden = false; h.listeners.visibilitychange.forEach((f) => f()); await settle();
    ok(!h.live().some((x) => x.ms === 5000), "the owed read after a hide does not start the schedule over");
    for (let i = 0; i < 6; i++) { t = h.live().find((x) => [5000, 15000, 60000].includes(x.ms)); if (!t) break; t.off = true; t.fn(); await settle(); }
    ok(calls.length === 5, "the event, the first retry, the owed read (a read, not a retry), then the two steps it had not taken, then it waits: " + calls.length);
  }
  // ---- a step is spent when its timer FIRES: timers cancelled before that (a whole draw, a hide) spend nothing ----
  {
    const h = boot({});
    const calls = [];
    h.win.LevainCockpitView.refetchPanel = async (id) => { calls.push(id); return "fault"; };
    await settle();
    h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "2" }));
    await settle();
    h.win.LevainCockpitHeld.a.err = "unavailable";
    for (let i = 0; i < 5; i++) {
      (h.listeners["levain:cockpit-held"] || []).forEach((f) => f());     // a whole draw cancels the pending timer and schedules again
      await settle();
    }
    for (let i = 0; i < 5; i++) {
      h.doc.hidden = true; h.listeners.visibilitychange.forEach((f) => f()); await settle();     // a hide cancels it too
      h.doc.hidden = false; h.listeners.visibilitychange.forEach((f) => f()); await settle();
    }
    const before = calls.length;
    const t = h.live().find((x) => x.ms === 5000);
    ok(!!t, "after ten cancelled timers the first step is still the next one: " + JSON.stringify(h.live().filter((x) => !x.every).map((x) => x.ms)));
    if (t) { t.off = true; t.fn(); await settle(); }
    ok(calls.length === before + 1 && h.live().some((x) => x.ms === 15000), "a retry still happens, and the second step follows it");
  }
  // ---- a panel recovered by a direct refetchPanel (no stream path) starts its budget over ----
  {
    const h = boot({ manifest: () => ({ panels: { a: { etag: "m" }, b: { etag: "1" } } }) });
    const calls = [];
    h.win.LevainCockpitView.refetchPanel = async (id) => { calls.push(id); return "fault"; };
    await settle();
    h.fetches[0].push(frame("panel-changed", "E.1:1", { id: "a", etag: "2" }));
    await settle();
    for (const ms of [5000, 15000, 60000]) { const t = h.live().find((x) => x.ms === ms); t.off = true; t.fn(); await settle(); }
    ok(!h.live().some((t) => [5000, 15000, 60000].includes(t.ms)), "the budget is spent");
    // a write's refetch read the panel well: the record is clean and the page says so (the stream path was not involved)
    delete h.win.LevainCockpitHeld.a.err;
    (h.listeners["levain:fresh"] || []).forEach((f) => f());
    // a later transient fault leaves the panel never read; the next reconciliation reads it again, which it would not with a spent budget
    delete h.win.LevainCockpitHeld.a.etag;
    h.win.LevainCockpitHeld.a.err = "unavailable";
    const before = calls.length;
    h.fetches[0].push(frame("hello", "E.1:2", { epoch: "E.1", resync: true }));
    await settle();
    ok(calls.length === before + 1 && h.live().some((t) => [5000, 15000, 60000].includes(t.ms)), "a fresh fault is read again and has retry steps left: " + (calls.length - before));
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
