// Levain's push service worker (the cockpit's, since W1b): show a pushed nudge, and on a tap open the page and mark the
// tap. The worker holds NO credential and makes no network call in a tap: it writes the tapped ping's id into its own
// IndexedDB and nudges the page, which holds the token and records the open (pair_card.js). The page/worker contract:
// IndexedDB `levain-sw-2` version 1, object store `opened` (key = the ping id as a string, value = the tap time in ms),
// window message {type: 'levain-opened'}. The page opens the same database with the same version and upgrade, so
// whichever opens first creates the store; correctness depends on no timing (an unread key waits for the next trigger).
const DB = 'levain-sw-2', STORE = 'opened';
// Cache Storage names this worker keeps: `levain-open` holds the pending tap target (see notificationclick); a later row
// (W5b) adds its own versioned names here.
const OPEN_CACHE = 'levain-open', OPEN_PREFIX = '/__levain_open/';
const CACHES_KEEP = [OPEN_CACHE];
// How long a 'for: new' entry lives (and how long the worker keeps one after a successful openWindow): the window between
// openWindow and the opened page's first script run. The page's own limit for 'new' entries is the same figure (sw_open.js).
const NEW_TTL_MS = 30000;  // a cold-started Home Screen app can take seconds to run its first script; the claim is
                           // already limited to a page at '/' or at the target, so a longer window is cheap

function open() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => { if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE); };
    req.onerror = () => reject(req.error);
    req.onsuccess = () => resolve(req.result);
  });
}

// the ping id, as a string key, with the tap's time
async function markOpened(ping) {
  const db = await open();
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).put(Date.now(), String(ping));
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally { db.close(); }
}

// the retired phone-port database (it held the phone token) is deleted whole; a blocked delete stays queued in the
// browser and completes when the last connection closes, so activation never waits on it
function dropOldDatabase() {
  return new Promise((resolve) => {
    let req;
    try { req = indexedDB.deleteDatabase('levain-sw'); } catch (_) { resolve(); return; }
    req.onsuccess = () => resolve();
    req.onerror = () => resolve();
    req.onblocked = () => resolve();
  });
}

async function dropOldCaches() {
  if (typeof caches === 'undefined') return;
  const names = await caches.keys();
  await Promise.all(names.filter((n) => n.startsWith('levain-') && !CACHES_KEEP.includes(n)).map((n) => caches.delete(n)));
}

self.addEventListener('install', () => self.skipWaiting());
// the pre-fix4 pending target: ONE singleton key in the same cache; no claim path reads it any more, so it is deleted
async function dropLegacyPending() {
  if (typeof caches === 'undefined') return;
  const c = await caches.open(OPEN_CACHE);
  await c.delete('/__levain_open');
}

self.addEventListener('activate', (e) => e.waitUntil((async () => {
  await Promise.allSettled([dropOldDatabase(), dropOldCaches(), dropLegacyPending()]);
  await self.clients.claim();
})()));

self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: e.data ? e.data.text() : '' }; }
  const title = d.title || 'Levain';
  // a fixed tag with renotify: a re-ping for the same item replaces the earlier notification and alerts again
  e.waitUntil(self.registration.showNotification(title, {
    body: d.body || '',
    tag: d.tag || 'levain',
    renotify: true,
    icon: '/icon-192.png',
    badge: '/badge-96.png',   // monochrome alpha: Android draws only the alpha channel in the status bar
    data: { url: d.url || '/', ping: d.ping },
  }));
});

// How long a tap waits for the page's ack before the worker's own paths run. Short on purpose: clients.openWindow()
// must still be inside the click's user-activation window when it runs, and Chrome may grant only ONE focus() or
// openWindow() per click, so nothing is awaited before this wait (focus included). A page with the listener acks in ms.
const OPEN_ACK_MS = 400;

// The pending targets: ONE cache entry PER OPEN, key OPEN_PREFIX + nonce, value {url, nonce, at, for}. A claim is
// cache.delete(<that exact key>) resolving true: whoever gets true owns the open, and nothing ever deletes a key it did
// not read or name, so a second tap's entry, or a fallback entry meant for another window, is never touched.
// for: 'asked' = written before a window is asked; only the message path (the nonce the worker named) may claim it.
//      'new'   = written immediately before openWindow; only a page's FIRST load may claim it (sw_open.js), and only a page
//                at '/' (where iOS lands a tapped window) or at the entry's own target; it lives NEW_TTL_MS, as 'asked' does.
async function putPending(entry) {
  const c = await caches.open(OPEN_CACHE);
  const key = OPEN_PREFIX + entry.nonce;
  await c.put(key, new Response(JSON.stringify(entry)));
  return key;
}
async function dropPending(key) {
  if (!key) return false;
  const c = await caches.open(OPEN_CACHE);
  return c.delete(key);
}
const newNonce = () => Math.random().toString(36).slice(2) + Date.now().toString(36);

// Ask a window to go to url itself; true only if it acked {ok: true} on the reply port within OPEN_ACK_MS
function askToOpen(c, url, nonce) {
  return new Promise((resolve) => {
    let ch;
    try { ch = new MessageChannel(); } catch (_) { resolve(false); return; }
    const t = setTimeout(() => done(false), OPEN_ACK_MS);
    const done = (ok) => { clearTimeout(t); ch.port1.onmessage = null; try { ch.port1.close(); } catch (_) { /* gone */ } resolve(ok); };
    ch.port1.onmessage = (m) => { if (m.data && m.data.ok === true) done(true); };
    try { c.postMessage({ type: 'levain-open', url, nonce }, [ch.port2]); } catch (_) { done(false); }
  });
}

// the cockpit '/', a sign sheet or /pair: the pages that load sw_open.js
function isCockpitPath(c) {
  try { const p = new URL(c.url).pathname; return p === '/' || p === '/pair' || p.startsWith('/sign/'); } catch (_) { return false; }
}
// same origin, path and query: only a fragment may differ (a client still at '/' is not at '/?view=needs')
function sameTarget(a, b) {
  try { const x = new URL(a), y = new URL(b); return x.origin === y.origin && x.pathname === y.pathname && x.search === y.search; } catch (_) { return false; }
}

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const data = e.notification.data || {};
  let u;
  try { u = new URL(data.url || '/', self.location.origin); } catch (_) { u = null; }
  // a cross-origin (or unparseable) url is never opened: the push body is the server's, the tap is the page's
  if (!u || u.origin !== self.location.origin) u = new URL('/', self.location.origin);
  const url = u.href;
  const opened = (async () => {
    const sameOrigin = (c) => { try { return new URL(c.url).origin === self.location.origin; } catch (_) { return false; } };
    // 1. the 'asked' entry is written in PARALLEL with the window lookups (nothing else is awaited before the ack wait), and
    //    before any focus or message: the page claims it by the nonce the message names
    const nonce = newNonce();
    const [askedKey, ctlAll, anyAll] = await Promise.all([
      putPending({ url, nonce, at: Date.now(), for: 'asked' }).catch((err) => {
        console.warn(`levain sw: the pending target was not written (${err}); the tap is message-only`);
        return null;
      }),
      self.clients.matchAll({ type: 'window' }).catch(() => []),
      self.clients.matchAll({ type: 'window', includeUncontrolled: true }).catch(() => []),
    ]);
    // the asked entry is for nobody when no window exists: dropped best effort
    const dropAsked = async () => { try { return await dropPending(askedKey); } catch (_) { return false; } };
    // 2. the window: a controlled cockpit-type page, else any controlled one, else any same-origin one
    const ctl = ctlAll.filter(sameOrigin);
    const pool = ctl.length ? ctl : anyAll.filter(sameOrigin);
    const c = pool.find(isCockpitPath) || pool[0];
    // the fallback: a 'for: new' entry (one new nonce), then openWindow; a refused or empty openWindow deletes that entry,
    // so a plain visit to '/' later is never sent to a card nobody opened
    const openNew = async (why) => {
      let key = null;
      try { key = await putPending({ url, nonce: newNonce(), at: Date.now(), for: 'new' }); } catch (_) { /* best effort: the page may not forward itself */ }
      try {
        const w = await self.clients.openWindow(url);
        if (w) {
          console.info(`levain sw: tap -> openWindow${why ? ` after ${why}` : ''}`);
          // held inside the click's waitUntil: an entry the opened page never claimed (it has no listener) is not left behind
          await new Promise((r) => setTimeout(r, NEW_TTL_MS));
          try { await dropPending(key); } catch (_) { /* best effort */ }
          return;
        }
        console.warn(`levain sw: openWindow(${url}) returned no client`);
      } catch (err) {
        console.warn(`levain sw: openWindow(${url}) failed: ${err}`);
      }
      console.info('levain sw: tap -> openWindow-failed');
      try { await dropPending(key); } catch (_) { /* nothing more to do */ }
    };
    // 4. no window (the asked entry was written for nobody)
    if (!c) { await dropAsked(); await openNew(''); return; }
    // 3. ask the page to go itself (sw_open.js). WindowClient.navigate rejects on an uncontrolled window and can resolve
    //    without moving a page on iOS WebKit. No focus before the wait.
    if (await askToOpen(c, url, askedKey ? nonce : null)) {
      try { await c.focus(); } catch (_) { /* best effort */ }
      console.info('levain sw: tap -> ack');
      return;
    }
    // no ack: claim the asked entry (the exact key this tap wrote) or find the page already took it (a late ack)
    if (askedKey) {
      let mine;
      // a cache that throws cannot have been claimed by a page that needs the same cache: the worker owns the open
      try { mine = await dropPending(askedKey); } catch (_) { mine = true; }
      if (!mine) { console.info('levain sw: tap -> ack (late)'); return; }
    }
    if (ctl.includes(c)) {
      try { await c.focus(); } catch (_) { /* navigate anyway */ }
      try {
        // accepted when the resolved client is at the target (origin + path + query); null or another page is a navigation that did not happen
        const r = await c.navigate(url);
        if (r && sameTarget(r.url, url)) { console.info('levain sw: tap -> navigate'); return; }
      } catch (_) { /* fall through to a new window */ }
    }
    await openNew('no ack');
  })();
  // the mark is local and best effort: a failed write loses one open's record, never the page. It is said on the
  // console, not swallowed: a VersionError means the page opened `levain-sw-2` at a version other than this pin (1)
  const mark = (async () => {
    if (data.ping === undefined || data.ping === null) return;
    try { await markOpened(data.ping); } catch (err) {
      const why = err && err.name === 'VersionError' ? `${DB} is not at version 1 (the page/worker pin)` : String(err);
      console.warn(`levain sw: the open of ping ${data.ping} was not recorded: ${why}`);
      return;
    }
    // after the write commits: a nudge only, no data; the page reads the store itself
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of wins) c.postMessage({ type: 'levain-opened' });
  })();
  e.waitUntil(Promise.allSettled([opened, mark]));
});
