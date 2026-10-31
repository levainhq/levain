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

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const data = e.notification.data || {};
  let u;
  try { u = new URL(data.url || '/', self.location.origin); } catch (_) { u = null; }
  // a cross-origin (or unparseable) url is never opened: the push body is the server's, the tap is the page's
  if (!u || u.origin !== self.location.origin) u = new URL('/', self.location.origin);
  const url = u.href;
  const opened = (async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      if (new URL(c.url).origin === self.location.origin) {
        await c.focus();
        if ('navigate' in c) { try { await c.navigate(url); } catch (_) { /* keep the focused window */ } }
        return;
      }
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
