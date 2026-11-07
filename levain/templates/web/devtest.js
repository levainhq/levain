// The notification-action device test page (band 299). "Run test" shows one notification WITH Approve/Deny action buttons
// from this page through the service worker registration (no push), prints what the phone accepted, and reports 'shown' to
// POST /webpush/devtest with the paired device's key. A tap is recorded by the worker in the trace ring ('devtest-click');
// this page reports every such entry it has not yet reported, whenever it opens (the tap opens /devtest).
(() => {
  "use strict";
  const log = document.getElementById("dt-log");
  const TRACE_CACHE = "levain-trace", TRACE_PREFIX = "/__levain_trace/", SENT_KEY = "levain_devtest_sent";
  const say = (t) => { const p = document.createElement("p"); p.className = "pair-msg"; p.textContent = t; log.prepend(p); };
  const ls = (k, v) => { try { if (v === undefined) return window.localStorage.getItem(k) || ""; window.localStorage.setItem(k, v); } catch (_) { /* none */ } return ""; };
  const maxActions = () => { try { return typeof Notification !== "undefined" && Number.isInteger(Notification.maxActions) ? Notification.maxActions : null; } catch (_) { return null; } };
  const ua = () => (navigator.userAgent || "").replace(/[^\x20-\x7e]/g, "?").slice(-80);

  async function report(body) {
    const key = ls("levain_write_token");
    if (!key) { say("Not reported: this browser holds no cockpit key (open the cockpit once, paired)."); return false; }
    try {
      const r = await fetch("/webpush/devtest", { method: "POST", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-Levain-Write-Token": key }, body: JSON.stringify(Object.assign({ ua: ua(), maxActions: maxActions() }, body)) });
      if (r.status !== 200) say(`The report was refused: HTTP ${r.status}.`);
      return r.status === 200;
    } catch (err) { say("The report could not be sent: " + (err && err.name || "error")); return false; }
  }

  // taps the worker recorded, not yet reported
  async function drainTaps() {
    if (typeof caches === "undefined") return;
    let last = Number(ls(SENT_KEY)) || 0, top = last;
    try {
      const c = await caches.open(TRACE_CACHE), rows = [];
      for (const req of await c.keys()) {
        if (new URL(req.url).pathname.indexOf(TRACE_PREFIX) !== 0) continue;
        try { const e = await (await c.match(req)).json(); if (e && e.step === "devtest-click" && Number.isFinite(e.t) && e.t > last) rows.push(e); } catch (_) { /* skipped */ }
      }
      rows.sort((a, b) => a.t - b.t);
      for (const e of rows) {
        const action = String(e.action || "").slice(0, 20), state = String(e.state || "").slice(0, 20);
        say(`TAP SEEN: ${action} (the app was: ${state}).`);
        if (await report({ event: "click", action, state })) top = Math.max(top, e.t);
      }
    } catch (_) { /* the ring is unreadable */ }
    if (top > last) ls(SENT_KEY, String(top));
  }

  async function run(delaySecs) {
    say(`Notification API: ${typeof Notification === "undefined" ? "ABSENT" : "present"}; standalone (Home Screen): ${navigator.standalone === true || (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches)}.`);
    if (typeof Notification === "undefined") return;
    say(`Notification.maxActions = ${maxActions()}; 'actions' in Notification.prototype = ${"actions" in Notification.prototype}.`);
    let perm = Notification.permission;
    if (perm === "default") { try { perm = await Notification.requestPermission(); } catch (err) { say("requestPermission failed: " + (err && err.name || "error")); } }
    say("Notification.permission = " + perm + ".");
    if (perm !== "granted") return;
    let reg;
    try {
      reg = await navigator.serviceWorker.getRegistration();
      if (!reg) { reg = await navigator.serviceWorker.register("/sw.js"); await navigator.serviceWorker.ready; }
    } catch (err) { say("No service worker registration: " + (err && err.name || "error")); return; }
    say(`Service worker registration: ${reg.scope}. Close, suspend or keep this app open now; the notification shows in ${delaySecs} s.`);
    setTimeout(async () => {
      const tag = "devtest-" + Date.now(), opts = { body: "Tap Approve, Deny, or the body. This asks nothing of you.", tag, requireInteraction: false,
        actions: [{ action: "approve", title: "Approve" }, { action: "deny", title: "Deny" }], data: { devtest: true, url: "/devtest" } };
      try {
        await reg.showNotification("levain device test", opts);
        say("showNotification resolved with actions in the options (no error).");
        let held = null;
        try { held = (await reg.getNotifications({ tag }))[0] || null; } catch (_) { /* unsupported */ }
        say(held ? `The phone holds it: actions on the object = ${JSON.stringify(held.actions === undefined ? "(field absent)" : held.actions)}.` : "The phone does not list it (getNotifications found none).");
        await report({ event: "shown", state: document.visibilityState });
      } catch (err) { say("showNotification FAILED: " + (err && err.name || "error") + " " + String(err && err.message || "").slice(0, 80)); }
    }, delaySecs * 1000);
  }

  document.getElementById("dt-run10").addEventListener("click", () => run(10));
  document.getElementById("dt-run60").addEventListener("click", () => run(60));
  drainTaps();
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") drainTaps(); });
})();
