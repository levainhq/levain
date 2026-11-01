// Levain's push service worker (the cockpit's, since W1b): show a pushed nudge, and on a tap open the page and mark the
// tap. The worker holds NO credential and makes no network call in a tap: it writes the tapped ping's id into its own
// IndexedDB and nudges the page, which holds the token and records the open (pair_card.js). The page/worker contract:
// IndexedDB `levain-sw-2` version 1, object store `opened` (key = the ping id as a string, value = the tap time in ms),
// window message {type: 'levain-opened'}. The page opens the same database with the same version and upgrade, so
// whichever opens first creates the store; correctness depends on no timing (an unread key waits for the next trigger).
const DB = 'levain-sw-2', STORE = 'opened';
// A short id of this worker's code, recorded in the trace so a device's trace says which worker ran.
const SW_VERSION = '1101-receipts-3';
// The TRACE: an append-only ring in its own cache, one entry per step, key TRACE_PREFIX + <time ms, zero padded> + '-' +
// <seq>, value JSON {t, src, step, ...detail}. The /tap_trace page and sw_open.js read and write the same ring. Writes are
// best effort, serialized on one chain, never awaited by the tap, and carry only urls without fragments, counts and step
// names: no token, no ping body.
const TRACE_CACHE = 'levain-trace', TRACE_PREFIX = '/__levain_trace/', TRACE_CAP = 60;
// Cache Storage names this worker keeps: `levain-open` holds the pending tap target (see notificationclick); a later row
// (W5b) adds its own versioned names here.
const OPEN_CACHE = 'levain-open', OPEN_PREFIX = '/__levain_open/';
// A device receipt (see sendReceipt) waits in its own cache until the server answered it: one entry per REPORT, key
// RECEIPT_PREFIX + <ping> + '-' + <kind> + '-' + <time ms, zero padded> + '-' + <seq>, so two deliveries of one ping are two
// entries. At most RECEIPT_QUEUE_CAP entries (the oldest dropped); at most RECEIPT_DRAIN re-sent per event; all receipt
// work of one event ends RECEIPT_BUDGET_MS after the event started.
const RECEIPT_CACHE = 'levain-receipts', RECEIPT_PREFIX = '/__levain_receipt/';
const RECEIPT_BUDGET_MS = 4000, RECEIPT_DRAIN = 5, RECEIPT_QUEUE_CAP = 200, RECEIPT_MAX_AGE_MS = 86400000;
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
// throws into the notification. ONE budget per event bounds all of it (receiptBudget): every fetch shares its abort
// signal, and every subscription, digest and Cache Storage wait races its deadline.
const LATE = Object.freeze({ late: true });   // what a wait under a budget yields when the deadline came first
// The receipt budget of one event: RECEIPT_BUDGET_MS from t0 (the event's start). `within(f)` runs f and yields its
// result, or LATE once the deadline passed; `signal` aborts every fetch at the deadline; `over` says it passed.
function receiptBudget(t0) {
  const ac = new AbortController();
  const b = { signal: ac.signal, over: false };
  let timer;
  const expired = new Promise((r) => {
    timer = setTimeout(() => { b.over = true; try { ac.abort(); } catch (_) { /* gone */ } r(LATE); },
                       Math.max(0, RECEIPT_BUDGET_MS - (Date.now() - t0)));
  });
  b.within = (f) => Promise.race([Promise.resolve().then(f), expired]);
  b.end = () => clearTimeout(timer);
  return b;
}
// the SHA-256 (hex) of this worker's push-subscription endpoint, asked NOW (never cached across events: a rotated
// subscription has a new endpoint); null without a subscription, LATE past the budget
async function endpointHash(b) {
  try {
    const sub = await b.within(() => self.registration.pushManager.getSubscription());
    if (sub === LATE) return LATE;
    if (!sub || !sub.endpoint) return null;
    const d = await b.within(() => crypto.subtle.digest('SHA-256', new TextEncoder().encode(sub.endpoint)));
    if (d === LATE) return LATE;
    return Array.from(new Uint8Array(d)).map((x) => x.toString(16).padStart(2, '0')).join('');
  } catch (_) { return null; }
}
// the HTTP status of one POST; throws when the network failed or the budget aborted it
async function postReceipt(body, b) {
  const r = await b.within(() => fetch('/webpush/receipt', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                            credentials: 'same-origin', cache: 'no-store', body: JSON.stringify(body), signal: b.signal }));
  if (r === LATE) throw new Error('receipt budget');
  return r.status;
}
// a status that retrying cannot change: delivered, or refused for this report itself (an unknown hash or ping included)
const receiptSettled = (status) => status === 200 || (status >= 400 && status < 500 && status !== 408 && status !== 429);
let receiptSeq = 0;
const receiptKey = (body) => RECEIPT_PREFIX + body.ping + '-' + body.kind + '-' + String(body.at).padStart(13, '0') + '-' +
  String(receiptSeq++).padStart(4, '0');
// [time, seq] from a key, for oldest-first; a key of an older form sorts first
function receiptAge(k) {
  const m = /-(\d+)-(\d+)$/.exec(k.slice(RECEIPT_PREFIX.length));
  return m ? [Number(m[1]), Number(m[2])] : [0, 0];
}
const byAge = (x, y) => { const a = receiptAge(x), c = receiptAge(y); return a[0] - c[0] || a[1] - c[1] || (x < y ? -1 : x > y ? 1 : 0); };
const receiptKeys = async (c) => (await c.keys()).map((r) => new URL(r.url).pathname).filter((k) => k.indexOf(RECEIPT_PREFIX) === 0).sort(byAge);
// write one report under its own key, then drop the oldest past RECEIPT_QUEUE_CAP; true when written
async function putReceipt(key, body) {
  if (typeof caches === 'undefined') return false;
  const c = await caches.open(RECEIPT_CACHE);
  await c.put(key, new Response(JSON.stringify(body)));
  const keys = await receiptKeys(c);
  for (const k of keys.slice(0, Math.max(0, keys.length - RECEIPT_QUEUE_CAP))) await c.delete(k);
  return true;
}
async function dropReceipt(key) {
  const c = await caches.open(RECEIPT_CACHE);
  return c.delete(key);
}
const traceRefused = (kind, ping, status, result) => trace('receipt-refused', { kind, ping, result, status });
// Report one receipt: written to the queue under its own key while the POST runs beside it, and removed only when the
// server answered 200 or refused it (a refused report is dropped: retrying cannot change it); a network failure, a 5xx,
// 408 or 429, or the budget leaves it queued for a later event. True when the server answered (the network is up, so a
// drain is worth its time); never throws.
async function sendReceipt(ping, kind, at, b, hashP) {
  try {
    if (!Number.isInteger(ping) || ping <= 0) { trace('receipt', { kind, ping: null, result: 'no-ping' }); return false; }
    const hash = await hashP;
    if (hash === LATE || b.over) { trace('receipt', { kind, ping, result: 'late' }); return false; }
    if (!hash) { trace('receipt-no-sub', { kind, ping }); return false; }
    const body = { ping, kind, at, endpoint_sha256: hash };
    const key = receiptKey(body);
    const [put, post] = await Promise.allSettled([b.within(() => putReceipt(key, body)), postReceipt(body, b)]);
    const queued = put.status === 'fulfilled' && put.value === true;
    const status = post.status === 'fulfilled' ? post.value : null;
    if (status !== null && receiptSettled(status)) {
      if (queued) { try { await b.within(() => dropReceipt(key)); } catch (_) { /* re-sent later: the server absorbs it */ } }
      if (status === 200) trace('receipt', { kind, ping, result: 'ok' }); else traceRefused(kind, ping, status, 'refused');
      return true;
    }
    trace('receipt', { kind, ping, result: queued ? 'queued' : 'lost', status });
    return status !== null;
  } catch (_) { return false; /* a receipt never reaches the notification */ }
}
// Re-send queued receipts, oldest first, at most RECEIPT_DRAIN, inside budget b. Each entry is deleted by the exact key
// read, and only after the server answered 200 or refused it, or when it is older than RECEIPT_MAX_AGE_MS (a day: the
// ping it describes is long past). No answer at all (the network) stops the drain and the entry stays; an answer that
// is not a settle (408, 429, a 5xx) leaves that entry and goes on to the next, so one entry the server keeps refusing
// for a while cannot hold the ones behind it.
async function drainReceipts(b) {
  try {
    if (typeof caches === 'undefined' || b.over) return;
    const c = await b.within(() => caches.open(RECEIPT_CACHE));
    if (c === LATE) return;
    const keys = await b.within(() => receiptKeys(c));
    if (keys === LATE) return;
    for (const k of keys.slice(0, RECEIPT_DRAIN)) {
      if (b.over) return;
      if (Date.now() - receiptAge(k)[0] > RECEIPT_MAX_AGE_MS && receiptAge(k)[0] > 0) {
        try { await b.within(() => c.delete(k)); } catch (_) { /* best effort */ }
        trace('receipt', { kind: null, ping: null, result: 'expired' });
        continue;
      }
      let body;
      try {
        body = await b.within(async () => { const m = await c.match(k); return m ? JSON.parse(await m.text()) : null; });
      } catch (_) { try { await b.within(() => c.delete(k)); } catch (_e) { /* best effort */ } continue; }   // unreadable
      if (body === LATE) return;
      if (!body) continue;
      let status = null;
      try { status = await postReceipt(body, b); } catch (_) { status = null; }
      if (status !== null && receiptSettled(status)) {
        try { await b.within(() => c.delete(k)); } catch (_) { /* re-sent later: the server absorbs it */ }
        if (status === 200) trace('receipt', { kind: body.kind, ping: body.ping, result: 'drained', status });
        else traceRefused(body.kind, body.ping, status, 'refused');
      } else {
        trace('receipt', { kind: body.kind, ping: body.ping, result: 'still-queued', status });
        if (status === null) break;       // no answer: the network is the likely cause
      }
    }
  } catch (_) { /* best effort */ }
}
// all receipt work of one event, bounded: resolves by the budget's deadline whatever hangs inside
function boundedReceipts(b, work) {
  return b.within(work).catch(() => null).finally(() => b.end());
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
  // a fixed tag with renotify: a re-ping for the same item replaces the earlier notification and alerts again. The
  // notification is asked for FIRST, before any trace or receipt, and never waits on either; it does not race the
  // receipt budget (only receipts do)
  let shown;
  try {
    shown = Promise.resolve(self.registration.showNotification(title, {
      body: d.body || '',
      tag: d.tag || 'levain',
      renotify: true,
      icon: '/icon-192.png',
      badge: '/badge-96.png',   // monochrome alpha: Android draws only the alpha channel in the status bar
      data: { url: d.url || '/', ping: d.ping },
    }));
  } catch (err) { shown = Promise.reject(err); }
  trace('sw-version', { version: SW_VERSION, on: 'push' });
  trace('push', { tag: traceScalar(d.tag || 'levain'), ping: traceScalar(d.ping) });
  const ping = typeof d.ping === 'number' ? d.ping : Number(d.ping);
  // then the receipts, all under one budget from the event's start: received now, shown when the notification resolved,
  // and the queue re-sent after both, only when the server answered one of them
  const b = receiptBudget(t0);
  const receipts = boundedReceipts(b, async () => {
    const hashP = endpointHash(b);   // one hash for this event's reports, asked now
    const r = await Promise.all([sendReceipt(ping, 'received', t0, b, hashP),
                                 shown.then(() => sendReceipt(ping, 'shown', Date.now(), b, hashP), () => false)]);
    if (r.some((x) => x === true)) await drainReceipts(b);
  });
  e.waitUntil(Promise.allSettled([shown, receipts]).finally(traceSettled));
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
  const clickT0 = Date.now();
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
  // the receipt queue is re-sent beside the tap, under the click's own receipt budget (never holding the tap)
  const rb = receiptBudget(clickT0);
  e.waitUntil(Promise.allSettled([opened, mark, boundedReceipts(rb, () => drainReceipts(rb))]).then(traceSettled));
});
