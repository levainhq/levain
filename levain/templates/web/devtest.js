// The notification-action device test page (band 299). "Send test notification" asks the SERVER (POST /webpush/devtest/send) to
// push one notification to this phone after a delay, so it arrives while the app is open, suspended or closed. The service
// worker shows it WITH Approve/Deny buttons and records what it did and what a tap did in the trace ring ('devtest-shown',
// 'devtest-click'); this page reports every ring entry it has not yet reported (with the paired key from localStorage) whenever
// it opens, becomes visible, or the worker tells it, stopping at the first report the server does not accept.
(() => {
  "use strict";
  const log = document.getElementById("dt-log");
  const TRACE_CACHE = "levain-trace", TRACE_PREFIX = "/__levain_trace/", SENT_KEY = "levain_devtest_sent_key";
  const say = (t) => { const p = document.createElement("p"); p.className = "pair-msg"; p.textContent = t; log.prepend(p); };
  const ls = (k, v) => { try { if (v === undefined) return window.localStorage.getItem(k) || ""; window.localStorage.setItem(k, v); } catch (_) { /* none */ } return ""; };
  const maxActions = () => { try { return typeof Notification !== "undefined" && Number.isInteger(Notification.maxActions) ? Notification.maxActions : null; } catch (_) { return null; } };
  const ua = () => (navigator.userAgent || "").replace(/[^\x20-\x7e]/g, "?").slice(-80);
  const plain = (v) => String(v == null ? "" : v).replace(/[^\x20-\x7e]/g, "?").slice(0, 20);

  async function post(path, payload, quiet) {
    const key = ls("levain_write_token");
    if (!key) { say("Not sent: this browser holds no cockpit key (open the cockpit once, paired)."); return null; }
    try {
      const r = await fetch(path, { method: "POST", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-Levain-Write-Token": key }, body: JSON.stringify(payload) });
      if (r.status !== 200 && !quiet) say(`The server refused: HTTP ${r.status}.`);
      return r;
    } catch (err) { if (!quiet) say("The request could not be sent: " + (err && err.name || "error")); return null; }
  }
  async function report(body) {
    const r = await post("/webpush/devtest", Object.assign({ ua: ua(), maxActions: maxActions() }, body));
    return !!r && r.status === 200;
  }

  // Ring entries (shown / tapped) not yet reported, oldest first, by cache key (zero-padded time + sequence, so key order is
  // time order). ONE drain at a time (a request arriving meanwhile asks for another pass); the watermark moves past a key only
  // once the server accepted its report, and the pass stops at the first failure, so nothing is skipped or sent twice.
  let draining = null, again = false;
  async function drainOnce() {
    if (typeof caches === "undefined") return;
    try {
      const c = await caches.open(TRACE_CACHE);
      const last = ls(SENT_KEY);
      const keys = (await c.keys()).map((r) => new URL(r.url).pathname).filter((k) => k.indexOf(TRACE_PREFIX) === 0 && k > last).sort();
      for (const k of keys) {
        let e = null;
        try { e = await (await c.match(k)).json(); } catch (_) { /* unreadable: passed over */ }
        const click = e && e.step === "devtest-click", shown = e && e.step === "devtest-shown";
        if (click || shown) {
          const action = plain(e.action), state = plain(e.state);
          if (click) say(`TAP SEEN: ${action} (the app was: ${state}).`); else say(`The push arrived and was shown: ${action} (the app was: ${state}).`);
          if (!(await report({ event: click ? "click" : "shown", action, state }))) return;
        }
        ls(SENT_KEY, k);
      }
    } catch (_) { /* the ring is unreadable */ }
  }
  function drainTaps() {
    if (draining) { again = true; return draining; }
    draining = (async () => { do { again = false; await drainOnce(); } while (again); })().finally(() => { draining = null; });
    return draining;
  }

  // The ACTIVE worker must be this build (an older one has no devtest branch and never answers the question).
  function activeIsNew(reg) {
    return new Promise((resolve) => {
      const w = reg && reg.active;
      if (!w) return resolve(null);
      const ch = new MessageChannel(), timer = setTimeout(() => resolve(null), 2500);
      ch.port1.onmessage = (ev) => { clearTimeout(timer); resolve(ev.data && ev.data.devtest === true ? String(ev.data.version || "?") : null); };
      try { w.postMessage({ type: "devtest-version" }, [ch.port2]); } catch (_) { clearTimeout(timer); resolve(null); }
    });
  }
  async function readyWorker() {
    let reg = await navigator.serviceWorker.getRegistration();
    if (!reg) reg = await navigator.serviceWorker.register("/sw.js");
    try { await reg.update(); } catch (_) { /* offline or busy: the active one is checked below */ }
    if (reg.waiting) { try { reg.waiting.postMessage({ type: "levain-skip-waiting" }); } catch (_) { /* best effort */ } }
    reg = await Promise.race([navigator.serviceWorker.ready, new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 8000))]);
    return reg;
  }

  let busy = false;
  async function run(delaySecs) {
    if (busy) return;
    busy = true;
    try {
      say(`Notification API: ${typeof Notification === "undefined" ? "ABSENT" : "present"}; standalone (Home Screen): ${navigator.standalone === true || (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches)}.`);
      if (typeof Notification === "undefined") return;
      say(`Notification.maxActions = ${maxActions()}; 'actions' in Notification.prototype = ${"actions" in Notification.prototype}.`);
      let perm = Notification.permission;
      if (perm === "default") { try { perm = await Notification.requestPermission(); } catch (err) { say("requestPermission failed: " + (err && err.name || "error")); } }
      say("Notification.permission = " + perm + ".");
      if (perm !== "granted") return;
      let reg;
      try { reg = await readyWorker(); } catch (err) { say("No service worker ready: " + (err && err.message || err && err.name || "error")); return; }
      const version = await activeIsNew(reg);
      if (!version) { say("The active service worker is not this build yet: reopen the app once to pick up the new worker (swipe it away, open it again), then come back here."); return; }
      say(`SW ready, build ${version}.`);
      const r = await post("/webpush/devtest/send", { delay_s: delaySecs }, true);
      if (!r) { say("The request could not be sent."); return; }
      if (r.status === 200) say(`Requested. The server sends the notification in ${delaySecs} s. DO IT NOW: lock the phone, or press Home, or swipe this app away (as the step says). Then tap Approve if there is a button, else the body.`);
      else if (r.status === 409) say("A test notification is already on its way to this phone. Wait for it.");
      else if (r.status === 429) say("Six test notifications in the last hour: wait a while.");
      else if (r.status === 403) say("This phone is not paired with pings on. Turn notifications on from the cockpit first.");
      else say(`The server refused: HTTP ${r.status}.`);
    } finally { busy = false; }
  }

  document.getElementById("dt-run10").addEventListener("click", () => run(10));
  document.getElementById("dt-run60").addEventListener("click", () => run(60));
  drainTaps();
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") drainTaps(); });
  if (navigator.serviceWorker) navigator.serviceWorker.addEventListener("message", (ev) => { if (ev.data && ev.data.type === "devtest-drain") drainTaps(); });
})();
