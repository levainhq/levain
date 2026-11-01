// Levain's push service worker (the cockpit's, since W1b): show a pushed nudge, and on a tap open the page and mark the
// tap. The worker holds NO credential and makes no network call in a tap: it writes the tapped ping's id into its own
// IndexedDB and nudges the page, which holds the token and records the open (pair_card.js). The page/worker contract:
// IndexedDB `levain-sw-2` version 1, object store `opened` (key = the ping id as a string, value = the tap time in ms),
// window message {type: 'levain-opened'}. The page opens the same database with the same version and upgrade, so
// whichever opens first creates the store; correctness depends on no timing (an unread key waits for the next trigger).
const DB = 'levain-sw-2', STORE = 'opened';
// Cache Storage names this worker keeps: `levain-open` holds the pending tap target (see notificationclick); a later row
// (W5b) adds its own versioned names here.
const OPEN_CACHE = 'levain-open', OPEN_KEY = '/__levain_open';
const CACHES_KEEP = [OPEN_CACHE];

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
self.addEventListener('activate', (e) => e.waitUntil((async () => {
  await Promise.allSettled([dropOldDatabase(), dropOldCaches()]);
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

// The pending target: one cache entry {url, nonce, at}. Whoever deletes it (cache.delete resolves true for exactly one
// caller) owns the open, so a late ack, a load-time check and the worker's fallback can never open the card twice.
async function putPending(entry) {
  const c = await caches.open(OPEN_CACHE);
  await c.put(OPEN_KEY, new Response(JSON.stringify(entry)));
}
async function claimPending() {
  const c = await caches.open(OPEN_CACHE);
  return c.delete(OPEN_KEY);
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
// same origin and path: a fragment or an equivalent spelling of the target still counts as arrived
function sameTarget(a, b) {
  try { const x = new URL(a), y = new URL(b); return x.origin === y.origin && x.pathname === y.pathname; } catch (_) { return false; }
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
    // 1. the pending target is written BEFORE any focus or openWindow: a page that is (re)loaded or resumed claims it
    //    even if no message ever reaches it (iOS can open the root url, or drop a message to a resumed app)
    let nonce = newNonce(), pending = true;
    try { await putPending({ url, nonce, at: Date.now() }); } catch (err) {
      pending = false;
      console.warn(`levain sw: the pending target was not written (${err}); the tap is message-only`);
    }
    const openNew = async (why) => {
      try {
        const w = await self.clients.openWindow(url);
        console.info(`levain sw: tap -> openWindow${w ? '' : ' (no client returned)'}${why ? ` after ${why}` : ''}`);
      } catch (err) {
        console.warn(`levain sw: openWindow(${url}) failed: ${err}`);
        console.info('levain sw: tap -> openWindow-failed');
      }
    };
    // 2. the window: a controlled cockpit-type page, else any controlled one, else any same-origin one
    const ctl = (await self.clients.matchAll({ type: 'window' })).filter(sameOrigin);
    const pool = ctl.length ? ctl : (await self.clients.matchAll({ type: 'window', includeUncontrolled: true })).filter(sameOrigin);
    const c = pool.find(isCockpitPath) || pool[0];
    // 4. no window
    if (!c) { await openNew(''); return; }
    // 3. ask the page to go itself (sw_open.js). WindowClient.navigate rejects on an uncontrolled window and can resolve
    //    without moving a page on iOS WebKit. No focus before the wait.
    if (await askToOpen(c, url, pending ? nonce : null)) {
      try { await c.focus(); } catch (_) { /* best effort */ }
      console.info('levain sw: tap -> ack');
      return;
    }
    // no ack: take the open over, or find the page already took it (a late ack claimed the pending target)
    if (pending) {
      let mine = false;
      try { mine = await claimPending(); } catch (_) { mine = true; }
      if (!mine) { console.info('levain sw: tap -> ack (late)'); return; }
    }
    if (ctl.includes(c)) {
      try { await c.focus(); } catch (_) { /* navigate anyway */ }
      try {
        // accepted when the resolved client is at the target (origin + path); null or another page is a navigation that did not happen
        const r = await c.navigate(url);
        if (r && sameTarget(r.url, url)) { console.info('levain sw: tap -> navigate'); return; }
      } catch (_) { /* fall through to a new window */ }
    }
    // a fresh nonce: a late ack from the window that was asked can no longer claim, while the new window's page
    // (which may land on '/' on iOS) still finds the target
    if (pending) { nonce = newNonce(); try { await putPending({ url, nonce, at: Date.now() }); } catch (_) { /* best effort */ } }
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
