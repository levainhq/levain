// The page half of the push tap (web push, ONESURFACE OS-4): the worker cannot reliably steer a window that already
// exists (WindowClient.navigate rejects on an uncontrolled one, and on iOS WebKit can resolve without moving the page),
// so on a tap it asks the page to go to the card itself. Message {type: 'levain-open', url} with a reply port; this
// page acks {ok: true} on that port BEFORE it navigates (the navigation unloads the page), then assigns the location.
// A url that is not on this origin is ignored without an ack (the worker checks too; this page does not trust it).
// Loaded by every page this origin serves a window can be showing: the cockpit (/), the sign sheet and /pair.
// The worker also writes PENDING TARGETS to Cache Storage, one entry per open under its own key '/__levain_open/<nonce>',
// value {url, nonce, at, for}. Whoever deletes that exact key (cache.delete resolves true for one caller only) owns the
// open, and nobody deletes a key it did not name or read. for 'asked' (written before the worker asks a window): only the
// message path claims it, by the nonce the worker named. for 'new' (written just before openWindow): only this document's
// first script run claims it (one scan per document, never repeated on pageshow or resume), because iOS can land a tapped
// window on the root url; and only a page at '/' or at the entry's own target does, so an unrelated first load never takes
// it. An already loaded page that merely resumes claims nothing, so a window the worker gave up on never moves late.

(() => {
  "use strict";
  if (!("serviceWorker" in navigator)) return;
  const CACHE = "levain-open", PREFIX = "/__levain_open/";
  // a target older than this is a tap nobody followed up (the card it points at is stale, and a later plain visit must not jump).
  // 'new': the window between openWindow and the opened page's first script run. 'asked': the worker's ack wait plus slack.
  const TTL_MS = { asked: 30000, new: 10000 };
  const haveCache = typeof caches !== "undefined";
  // same origin, path and query; only a fragment may differ
  const sameTarget = (a, b) => a.origin === b.origin && a.pathname === b.pathname && a.search === b.search;

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
    // only the key this message names, and only a nonce shaped like the worker's: a lost claim means another path (or the
    // worker) already owns this open, so no ack and no navigation
    if (!/^[a-z0-9]+$/i.test(d.nonce)) return;
    caches.open(CACHE).then((c) => c.delete(PREFIX + d.nonce)).then((won) => { if (won) go(); })
      .catch(() => { /* cache unreadable: the worker's path runs */ });
  });

  // the first-load check: a page that opens right after the tap (iOS can open the root url) forwards itself
  let checking = false;
  async function checkPending() {
    if (!haveCache || checking) return;
    checking = true;
    try {
      const cache = await caches.open(CACHE);
      const found = [];
      for (const req of await cache.keys()) {
        if (new URL(req.url).pathname.indexOf(PREFIX) !== 0) continue;
        let e = null;
        try { const r = await cache.match(req); e = r && await r.json(); } catch (_) { e = null; }
        // unreadable or expired: deleted (the exact key read); the worker deletes its own on the paths it owns
        if (!e || typeof e.url !== "string" || typeof e.at !== "number" || Date.now() - e.at > (TTL_MS[e.for] || TTL_MS.asked)) { await cache.delete(req); continue; }
        if (e.for !== "new") continue;                       // 'asked' belongs to the message path
        let u;
        try { u = new URL(e.url, location.href); } catch (_) { continue; }
        if (u.origin !== location.origin) continue;
        found.push({ req, e, u });
      }
      // only a page at '/' (the iOS root landing) or at the entry's own target may take a 'new' entry
      const here = new URL(location.href);
      for (let i = found.length - 1; i >= 0; i--) if (here.pathname !== "/" && !sameTarget(found[i].u, here)) found.splice(i, 1);
      // the entry for the page already showing, else the newest; a lost delete means another page took it: try the next
      found.sort((x, y) => (sameTarget(y.u, here) - sameTarget(x.u, here)) || (y.e.at - x.e.at));
      for (const f of found) {
        if (!(await cache.delete(f.req))) continue;
        if (!sameTarget(f.u, here)) location.assign(f.u.href);   // already here: consumed, the page stays
        return;
      }
    } catch (_) { /* no cache: nothing pending */ } finally { checking = false; }
  }
  // once, at this document's first script run: a pageshow call would let a page opened for one tap consume a later tap's entry
  checkPending();

  // an uncontrolled page (opened before the worker took over) queues worker messages until this is called
  try { navigator.serviceWorker.startMessages(); } catch (_) { /* older engines deliver without it */ }
})();
