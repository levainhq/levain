// Levain's push service worker (the cockpit's, since W1b): show a pushed nudge, and on a tap open the page and mark the
// tap. The worker holds NO credential and makes no network call in a tap: it writes the tapped ping's id into its own
// IndexedDB and nudges the page, which holds the token and records the open (pair_card.js). The page/worker contract:
// IndexedDB `levain-sw-2` version 1, object store `opened` (key = the ping id as a string, value = the tap time in ms),
// window message {type: 'levain-opened'}. The page opens the same database with the same version and upgrade, so
// whichever opens first creates the store; correctness depends on no timing (an unread key waits for the next trigger).
const DB = 'levain-sw-2', STORE = 'opened';
// Cache Storage names this worker keeps. W1b keeps none; a later row (W5b) adds its own versioned names here.
const CACHES_KEEP = [];

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
// needs the click's user-activation window, and a page that has the listener acks within a few ms.
const OPEN_ACK_MS = 800;

// Ask a window to go to url itself; true only if it acked {ok: true} on the reply port within OPEN_ACK_MS
function askToOpen(c, url) {
  return new Promise((resolve) => {
    let ch;
    try { ch = new MessageChannel(); } catch (_) { resolve(false); return; }
    const t = setTimeout(() => done(false), OPEN_ACK_MS);
    const done = (ok) => { clearTimeout(t); ch.port1.onmessage = null; try { ch.port1.close(); } catch (_) { /* gone */ } resolve(ok); };
    ch.port1.onmessage = (m) => { if (m.data && m.data.ok === true) done(true); };
    try { c.postMessage({ type: 'levain-open', url }, [ch.port2]); } catch (_) { done(false); }
  });
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
    // prefer a window this worker controls, else any same-origin window (an uncontrolled one still hears a message)
    const ctl = (await self.clients.matchAll({ type: 'window' })).filter(sameOrigin);
    const pool = ctl.length ? ctl : (await self.clients.matchAll({ type: 'window', includeUncontrolled: true })).filter(sameOrigin);
    const c = pool[0];
    if (!c) { await self.clients.openWindow(url); return; }
    // focus() can be refused without user activation; the card still goes to this window
    try { await c.focus(); } catch (_) { /* ask anyway */ }
    // the page navigates itself (sw_open.js): WindowClient.navigate rejects on an uncontrolled window and can resolve
    // without moving a page on iOS WebKit. An ack means the page took it.
    if (await askToOpen(c, url)) return;
    // no ack (a page without the listener, e.g. an old cached one): the worker's own paths
    if (ctl.length) {
      try {
        // accepted only if the resolved client is at the target; null or another url is a navigation that did not happen
        const r = await c.navigate(url);
        if (r && r.url === url) return;
      } catch (_) { /* fall through to a new window */ }
    }
    await self.clients.openWindow(url);
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
