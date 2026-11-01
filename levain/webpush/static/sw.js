// Levain's push service worker (the cockpit's, since W1b): show a pushed nudge, and on a tap open the page and mark the
// tap. The worker holds NO credential and makes no network call in a tap: it writes the tapped ping's id into its own
// IndexedDB and nudges the page, which holds the token and records the open (pair_card.js). The page/worker contract:
// IndexedDB `levain-sw-2` version 1, object store `opened` (key = the ping id as a string, value = the tap time in ms),
// window message {type: 'levain-opened'}. The page opens the same database with the same version and upgrade, so
// whichever opens first creates the store; correctness depends on no timing (an unread key waits for the next trigger).
const DB = 'levain-sw-2', STORE = 'opened';
// A short id of this worker's code, recorded in the trace so a device's trace says which worker ran.
const SW_VERSION = '1101-receipts-1';
// The TRACE: an append-only ring in its own cache, one entry per step, key TRACE_PREFIX + <time ms, zero padded> + '-' +
// <seq>, value JSON {t, src, step, ...detail}. The /tap_trace page and sw_open.js read and write the same ring. Writes are
// best effort, serialized on one chain, never awaited by the tap, and carry only urls without fragments, counts and step
// names: no token, no ping body.
const TRACE_CACHE = 'levain-trace', TRACE_PREFIX = '/__levain_trace/', TRACE_CAP = 60;
// Cache Storage names this worker keeps: `levain-open` holds the pending tap target (see notificationclick); a later row
// (W5b) adds its own versioned names here.
const OPEN_CACHE = 'levain-open', OPEN_PREFIX = '/__levain_open/';
// A device receipt (see sendReceipt) that could not be delivered waits in its own cache, one entry per ping and kind.
const RECEIPT_CACHE = 'levain-receipts', RECEIPT_PREFIX = '/__levain_receipt/', RECEIPT_MS = 3000, RECEIPT_DRAIN = 20;
const CACHES_KEEP = [OPEN_CACHE, TRACE_CACHE, RECEIPT_CACHE];
// How long a 'for: new' entry lives (and how long the worker keeps one after a successful openWindow): the window between
// openWindow and the opened page's first script run. The page's own limit for 'new' entries is the same figure (sw_open.js).
const NEW_TTL_MS = 30000;  // a cold-started Home Screen app can take seconds to run its first script; the claim is
                           // already limited to a page at '/' or at the target, so a longer window is cheap

let traceChain = Promise.resolve(), traceSeq = 0;
// a url reduced to its path, an allowlisted `?view=<value>` (clipped) and markers: other query parameters can carry a
// credential (written `?...`), and a fragment can carry a sign token (written `#...`)
function traceUrl(u) {
  try {
    const x = new URL(u, self.location.origin);
    const v = x.searchParams.get('view');
    const others = Array.from(x.searchParams.keys()).some((k) => k !== 'view');
    const q = v !== null ? '?view=' + encodeURIComponent(v.slice(0, 40)) : (others || x.search ? '?...' : '');
    return x.pathname + q + (x.hash ? '#...' : '');
  } catch (_) { return null; }
}
// a ping or tag recorded only as a string or a finite number (the tag clipped); anything else is null
function traceScalar(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  return typeof v === 'string' ? v.slice(0, 60) : null;
}
// total: nothing in here (the clock, the key, the JSON, the promise chain) can throw into the tap
function trace(step, detail) {
  try {
    const t = Date.now(), seq = String(traceSeq++).padStart(4, '0');
    traceChain = traceChain.then(async () => {
      if (typeof caches === 'undefined') return;
      const c = await caches.open(TRACE_CACHE);
      await c.put(TRACE_PREFIX + String(t).padStart(13, '0') + '-' + seq, new Response(JSON.stringify({ t, src: 'sw', step, ...detail })));
      const keys = (await c.keys()).map((r) => new URL(r.url).pathname).filter((k) => k.indexOf(TRACE_PREFIX) === 0).sort();
      for (const k of keys.slice(0, Math.max(0, keys.length - TRACE_CAP))) await c.delete(k);
    }).catch(() => { /* best effort: a failed trace write never reaches the tap */ });
  } catch (_) { /* best effort */ }
}
// what a click's waitUntil may hold on the trace: bounded, so a stuck cache cannot keep the worker alive; the timer is
// cleared when the chain settles first
const traceSettled = () => {
  let timer;
  return Promise.race([traceChain, new Promise((r) => { timer = setTimeout(r, 3000); })]).finally(() => clearTimeout(timer));
};

// --- device receipts -----------------------------------------------------------------------------------------------
// A push the device got is reported to the server: `received` when the push event fired, `shown` when showNotification
// resolved. The worker holds no credential, so a report names the SHA-256 of its own push-subscription endpoint (the
// secret only the device and the server share) and carries ids and times only. Every step is total: nothing in here
// throws into the notification, and each wait is bounded.
let endpointHashP = null;
function endpointHash() {
  if (!endpointHashP) {
    endpointHashP = (async () => {
      const sub = await self.registration.pushManager.getSubscription();
      if (!sub || !sub.endpoint) return null;
      const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(sub.endpoint));
      return Array.from(new Uint8Array(d)).map((b) => b.toString(16).padStart(2, '0')).join('');
    })().catch(() => null);
  }
  return endpointHashP;
}
// the HTTP status of one POST, or throws after RECEIPT_MS
async function postReceipt(body) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), RECEIPT_MS);
  try {
    const r = await fetch('/webpush/receipt', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                credentials: 'same-origin', cache: 'no-store', body: JSON.stringify(body), signal: ac.signal });
    return r.status;
  } finally { clearTimeout(timer); }
}
// a status that retrying cannot change: delivered, or refused for this report itself
const receiptSettled = (status) => status === 200 || (status >= 400 && status < 500 && status !== 408 && status !== 429);
async function queueReceipt(body) {
  const c = await caches.open(RECEIPT_CACHE);
  await c.put(RECEIPT_PREFIX + body.ping + '-' + body.kind, new Response(JSON.stringify(body)));
}
// true when the server answered (so the network is up and a drain is worth its time), false otherwise; never throws
async function sendReceipt(ping, kind, at) {
  try {
    if (!Number.isInteger(ping) || ping <= 0) { trace('receipt', { kind, ping: null, result: 'no-ping' }); return false; }
    const hash = await endpointHash();
    if (!hash) { trace('receipt-no-sub', { kind, ping }); return false; }
    const body = { ping, kind, at, endpoint_sha256: hash };
    let status = null;
    try { status = await postReceipt(body); } catch (err) { status = null; }
    if (status === 200) { trace('receipt', { kind, ping, result: 'ok' }); return true; }
    if (status !== null && receiptSettled(status)) { trace('receipt', { kind, ping, result: 'refused', status }); return true; }
    try { await queueReceipt(body); trace('receipt', { kind, ping, result: 'queued', status }); } catch (_) { trace('receipt', { kind, ping, result: 'lost', status }); }
    return status !== null;
  } catch (_) { return false; /* a receipt never reaches the notification */ }
}
// re-send queued receipts, oldest first, up to RECEIPT_DRAIN; one that cannot be delivered stops the drain (the network is
// the likely cause), one the server settled is dropped
async function drainReceipts() {
  try {
    if (typeof caches === 'undefined') return;
    const c = await caches.open(RECEIPT_CACHE);
    const keys = (await c.keys()).map((r) => new URL(r.url).pathname).filter((k) => k.indexOf(RECEIPT_PREFIX) === 0);
    const items = [];
    for (const k of keys) {
      try { const m = await c.match(k); items.push({ k, body: JSON.parse(await m.text()) }); } catch (_) { await c.delete(k); }
    }
    items.sort((a, b) => (a.body.at || 0) - (b.body.at || 0));
    for (const { k, body } of items.slice(0, RECEIPT_DRAIN)) {
      let status = null;
      try { status = await postReceipt(body); } catch (_) { status = null; }
      if (status !== null && receiptSettled(status)) {
        await c.delete(k);
        trace('receipt', { kind: body.kind, ping: body.ping, result: status === 200 ? 'drained' : 'refused', status });
      } else { trace('receipt', { kind: body.kind, ping: body.ping, result: 'still-queued', status }); break; }
    }
  } catch (_) { /* best effort */ }
}

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
  const t0 = Date.now();
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: e.data ? e.data.text() : '' }; }
  const title = d.title || 'Levain';
  trace('sw-version', { version: SW_VERSION, on: 'push' });
  trace('push', { tag: traceScalar(d.tag || 'levain'), ping: traceScalar(d.ping) });
  const ping = typeof d.ping === 'number' ? d.ping : Number(d.ping);
  // a fixed tag with renotify: a re-ping for the same item replaces the earlier notification and alerts again. The
  // notification is asked for first and never waits on a receipt: the reports run beside it, inside the same waitUntil
  const shown = Promise.resolve().then(() => self.registration.showNotification(title, {
    body: d.body || '',
    tag: d.tag || 'levain',
    renotify: true,
    icon: '/icon-192.png',
    badge: '/badge-96.png',   // monochrome alpha: Android draws only the alpha channel in the status bar
    data: { url: d.url || '/', ping: d.ping },
  })).then(() => sendReceipt(ping, 'shown', Date.now()));
  const received = sendReceipt(ping, 'received', t0);
  // the queue is re-sent after this push's own reports, and only when the server answered one of them
  e.waitUntil(Promise.allSettled([shown, received]).then((r) => (r.some((x) => x.value === true) ? drainReceipts() : null)).finally(traceSettled));
  shown.catch((err) => trace('show-failed', { error: err && err.name || 'error' }));
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
  trace('sw-version', { version: SW_VERSION, on: 'notificationclick' });
  trace('click', { url: traceUrl(url), ping: traceScalar(data.ping) });
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
    const winInfo = (c) => ({ url: traceUrl(c.url), visibility: c.visibilityState || null, focused: !!c.focused });
    trace('matchAll', { controlled: ctlAll.length, any: anyAll.length, controlledWindows: ctlAll.map(winInfo), anyWindows: anyAll.map(winInfo) });
    trace('asked', { written: !!askedKey });
    // the asked entry is for nobody when no window exists: dropped best effort
    const dropAsked = async () => { try { return await dropPending(askedKey); } catch (_) { return false; } };
    // 2. the window: a controlled cockpit-type page, else any controlled one, else any same-origin one
    const ctl = ctlAll.filter(sameOrigin);
    // the trace page itself is never the window to take over (it would be navigated away from the ring it shows)
    const notTrace = (c) => { try { return new URL(c.url).pathname !== '/tap_trace'; } catch (_) { return true; } };
    const ctlP = ctl.filter(notTrace);
    const pool = ctlP.length ? ctlP : anyAll.filter(sameOrigin).filter(notTrace);
    const c = pool.find(isCockpitPath) || pool[0];
    // the fallback: a 'for: new' entry (one new nonce), then openWindow; a refused or empty openWindow deletes that entry,
    // so a plain visit to '/' later is never sent to a card nobody opened
    const openNew = async (why) => {
      let key = null;
      try { key = await putPending({ url, nonce: newNonce(), at: Date.now(), for: 'new' }); } catch (_) { /* best effort: the page may not forward itself */ }
      try {
        const w = await self.clients.openWindow(url);
        trace('openWindow', { why: why || null, client: w ? traceUrl(w.url) : null });
        if (w) {
          console.info(`levain sw: tap -> openWindow${why ? ` after ${why}` : ''}`);
          // held inside the click's waitUntil: an entry the opened page never claimed (it has no listener) is not left behind
          await new Promise((r) => setTimeout(r, NEW_TTL_MS));
          let dropped = false;
          try { dropped = await dropPending(key); } catch (_) { /* best effort */ }
          trace('openNew-dropped', { dropped });
          return;
        }
        console.warn(`levain sw: openWindow(${url}) returned no client`);
      } catch (err) {
        trace('openWindow', { why: why || null, client: null, error: err && err.name || 'error' });
        console.warn(`levain sw: openWindow(${url}) failed: ${err}`);
      }
      console.info('levain sw: tap -> openWindow-failed');
      let dropped = false;
      try { dropped = await dropPending(key); } catch (_) { /* nothing more to do */ }
      trace('openNew-dropped', { dropped, after: 'no client' });
    };
    // 4. no window (the asked entry was written for nobody)
    if (!c) { await dropAsked(); await openNew(''); return; }
    // 3. ask the page to go itself (sw_open.js). WindowClient.navigate rejects on an uncontrolled window and can resolve
    //    without moving a page on iOS WebKit. No focus before the wait.
    const acked = await askToOpen(c, url, askedKey ? nonce : null);
    trace('ack', { acked, window: traceUrl(c.url) });
    if (acked) {
      try { await c.focus(); } catch (_) { /* best effort */ }
      console.info('levain sw: tap -> ack');
      return;
    }
    // no ack: claim the asked entry (the exact key this tap wrote) or find the page already took it (a late ack)
    if (askedKey) {
      let mine;
      // a cache that throws cannot have been claimed by a page that needs the same cache: the worker owns the open
      try { mine = await dropPending(askedKey); } catch (_) { mine = true; }
      trace('no-ack-claim', { mine });
      if (!mine) { console.info('levain sw: tap -> ack (late)'); return; }
    }
    if (ctl.includes(c)) {
      try { await c.focus(); } catch (_) { /* navigate anyway */ }
      try {
        // accepted when the resolved client is at the target (origin + path + query); null or another page is a navigation that did not happen
        const r = await c.navigate(url);
        trace('navigate', { result: r ? traceUrl(r.url) : null, ok: !!(r && sameTarget(r.url, url)) });
        if (r && sameTarget(r.url, url)) { console.info('levain sw: tap -> navigate'); return; }
      } catch (err) { trace('navigate', { error: err && err.name || 'error' }); /* fall through to a new window */ }
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
  e.waitUntil(Promise.allSettled([opened, mark, drainReceipts()]).then(traceSettled));
});
