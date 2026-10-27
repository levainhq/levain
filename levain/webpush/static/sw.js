// Levain's push service worker (the phone port's, and the cockpit's since ONESURFACE OS-4): show a pushed nudge, and
// on a tap open the page and record the open. A service worker cannot read localStorage, so the page hands its
// credential over with postMessage and the worker keeps it in IndexedDB; the credential is only ever sent back to this
// origin, under the header the page named (the cockpit's write-token header; the phone port's when none is named).
const DB = 'levain-sw', STORE = 'kv';
const HEADERS = ['X-Levain-Phone-Token', 'X-Levain-Write-Token'];

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
    const header = HEADERS.includes(d.header) ? d.header : HEADERS[0];
    e.waitUntil(kv('readwrite', (s) => { s.put(header, 'header'); return s.put(d.token, 'token'); }));
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
  const open = (async () => {
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
  // the open is a record, never a reason to wait on the network or to skip opening the page
  const record = (async () => {
    if (data.ping === undefined) return;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 4000);
    try {
      const token = await kv('readonly', (s) => s.get('token'));
      const named = await kv('readonly', (s) => s.get('header'));
      const header = HEADERS.includes(named) ? named : HEADERS[0];
      const r = await fetch('/webpush/opened', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, token ? { [header]: token } : {}),
        body: JSON.stringify({ ping: data.ping }),
        signal: ctl.signal,
      });
      // a refused credential (revoked device): stop sending it; the page hands a fresh one over after re-pairing
      if (r.status === 403) await kv('readwrite', (s) => s.delete('token'));
    } catch (_) { /* best effort */ } finally { clearTimeout(timer); }
  })();
  e.waitUntil(Promise.allSettled([open, record]));
});
