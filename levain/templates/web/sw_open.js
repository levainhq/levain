// The page half of the push tap (web push, ONESURFACE OS-4): the worker cannot reliably steer a window that already
// exists (WindowClient.navigate rejects on an uncontrolled one, and on iOS WebKit can resolve without moving the page),
// so on a tap it asks the page to go to the card itself. Message {type: 'levain-open', url} with a reply port; this
// page acks {ok: true} on that port BEFORE it navigates (the navigation unloads the page), then assigns the location.
// A url that is not on this origin is ignored without an ack (the worker checks too; this page does not trust it).
// Loaded by every page this origin serves a window can be showing: the cockpit (/), the sign sheet and /pair.
// The worker also writes a PENDING TARGET {url, nonce, at} to Cache Storage before it asks (iOS can open the root url on a
// tap, or drop a message to a just-resumed app). Whoever deletes that entry (cache.delete resolves true for one caller
// only) owns the open: the message path claims it by nonce, and a load / pageshow / visible page claims whatever is
// pending, so a late ack, a resumed page and the worker's fallback can never open the card twice.

(() => {
  "use strict";
  if (!("serviceWorker" in navigator)) return;
  const CACHE = "levain-open", KEY = "/__levain_open";
  // a target older than this is a tap nobody followed up (the card it points at is stale, and a later plain visit must not jump)
  const PENDING_TTL_MS = 30000;
  const haveCache = typeof caches !== "undefined";

  // delete the pending entry; true only for the caller whose delete removed it
  async function takePending(want) {
    const cache = await caches.open(CACHE);
    const r = await cache.match(KEY);
    if (!r) return null;
    let e;
    try { e = await r.json(); } catch (_) { await cache.delete(KEY); return null; }
    if (!e || typeof e.url !== "string") { await cache.delete(KEY); return null; }
    if (want && !want(e)) return null;
    return (await cache.delete(KEY)) ? e : null;
  }

  navigator.serviceWorker.addEventListener("message", (ev) => {
    const d = ev.data;
    if (!d || d.type !== "levain-open" || typeof d.url !== "string") return;
    let u;
    try { u = new URL(d.url, location.href); } catch (_) { return; }
    if (u.origin !== location.origin) return;
    const port = ev.ports && ev.ports[0];
    const go = () => {
      if (port) { try { port.postMessage({ ok: true }); } catch (_) { /* the worker falls back to its own path */ } }
      location.assign(u.href);
    };
    // no cache, or the worker could not write a target: message-only, as before
    if (!haveCache || typeof d.nonce !== "string") { go(); return; }
    // a lost claim means another path (or the worker) already owns this open: no ack, no navigation
    takePending((e) => e.nonce === d.nonce).then((e) => { if (e) go(); }).catch(() => { /* cache unreadable: the worker's path runs */ });
  });

  // the load-time check: a page that opens (or comes back to the front) after the tap forwards itself to the target
  let checking = false;
  async function checkPending() {
    if (!haveCache || checking) return;
    checking = true;
    try {
      const e = await takePending(null);
      if (!e) return;
      let u;
      try { u = new URL(e.url, location.href); } catch (_) { return; }
      if (u.origin !== location.origin) return;
      if (typeof e.at !== "number" || Date.now() - e.at > PENDING_TTL_MS) return;   // expired: already deleted by the take
      if (u.href === location.href) return;                                          // already here: consumed, nothing to do
      location.assign(u.href);
    } catch (_) { /* no cache: nothing pending */ } finally { checking = false; }
  }
  checkPending();
  window.addEventListener("pageshow", checkPending);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") checkPending(); });

  // an uncontrolled page (opened before the worker took over) queues worker messages until this is called
  try { navigator.serviceWorker.startMessages(); } catch (_) { /* older engines deliver without it */ }
})();
