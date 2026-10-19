// Levain phone page service worker: show a pushed nudge, and on a tap open the page and record the open.
// A service worker cannot read localStorage, so the page hands its credential over with postMessage and the
// worker keeps it in IndexedDB; the credential is only ever sent back to this origin.
const DB = 'levain-sw', STORE = 'kv';

function kv(mode, fn) {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open(DB, 1);
    open.onupgradeneeded = () => open.result.createObjectStore(STORE);
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
      const tx = open.result.transaction(STORE, mode);
      const req = fn(tx.objectStore(STORE));
      tx.oncomplete = () => { open.result.close(); resolve(req && req.result); };
      tx.onerror = () => reject(tx.error);
    };
  });
}

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('message', (e) => {
  const d = e.data || {};
  if (d.type === 'levain-token' && typeof d.token === 'string') {
    e.waitUntil(kv('readwrite', (s) => s.put(d.token, 'token')));
  }
});

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
    badge: '/icon-192.png',
    data: { url: d.url || '/', ping: d.ping },
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const data = e.notification.data || {};
  const url = new URL(data.url || '/', self.location.origin).href;
  e.waitUntil((async () => {
    try {
      const token = await kv('readonly', (s) => s.get('token'));
      if (data.ping !== undefined) {
        await fetch('/webpush/opened', {
          method: 'POST',
          headers: Object.assign({ 'Content-Type': 'application/json' }, token ? { 'X-Levain-Phone-Token': token } : {}),
          body: JSON.stringify({ ping: data.ping }),
        });
      }
    } catch (_) { /* the open is a record, never a reason to skip opening the page */ }
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      if (new URL(c.url).origin === self.location.origin) {
        await c.focus();
        if ('navigate' in c) { try { await c.navigate(url); } catch (_) { /* keep the focused window */ } }
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
