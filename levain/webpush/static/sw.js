// Levain's push service worker (the cockpit's, since W1b): show a pushed nudge, and on a tap open the page and mark the
// tap. The worker holds NO credential and makes no network call in a tap: it writes the tapped ping's id into its own
// IndexedDB and nudges the page, which holds the token and records the open (pair_card.js). The page/worker contract:
// IndexedDB `levain-sw-2` version 1, object store `opened` (key = the ping id as a string, value = the tap time in ms),
// window message {type: 'levain-opened'}. The page opens the same database with the same version and upgrade, so
// whichever opens first creates the store; correctness depends on no timing (an unread key waits for the next trigger).
const DB = 'levain-sw-2', STORE = 'opened';
// A short id of this worker's code, recorded in the trace so a device's trace says which worker ran.
const SW_VERSION = '1106-53-tap-1';
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
// The offline shell: the cockpit page '/' cached under a name that carries this worker's own version, so a new worker
// precaches its own copy and activation deletes every older `levain-shell-v*` (dropOldCaches: not in CACHES_KEEP).
const SHELL_CACHE = 'levain-shell-v' + SW_VERSION, SHELL_KEY = '/';
const CACHES_KEEP = [OPEN_CACHE, TRACE_CACHE, RECEIPT_CACHE, SHELL_CACHE];
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
// resolved and the user agent lists the notification (heldByAgent: held, which is not the same as drawn). The worker holds no credential, so a report names the SHA-256 of its own push-subscription endpoint (the
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
async function sendReceipt(ping, kind, at, b, hashP, extra) {
  try {
    if (!Number.isInteger(ping) || ping <= 0) { trace('receipt', { kind, ping: null, result: 'no-ping' }); return false; }
    const hash = await hashP;
    if (hash === LATE || b.over) { trace('receipt', { kind, ping, result: 'late' }); return false; }
    if (!hash) { trace('receipt-no-sub', { kind, ping }); return false; }
    const body = { ping, kind, at, endpoint_sha256: hash, ...(extra || {}) };   // extra: a tap report's target and how only
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
// --- tap reports (1106-53) -----------------------------------------------------------------------------------------
// What the worker says about a TAP goes to the server by the same queued receipt path, independent of the Cache Storage
// trace (11-06 21:00: ping 640 was received and shown on the phone, and no click or open step reached the trace). `clicked`
// is STARTED before the window step (its post waits only for the subscription, never for a window, so it goes out before any
// openWindow, navigate or ack outcome; matchAll is started in the same turn); `opened` after the window step, with HOW the worker got a window. Each report has
// its own budget, so a slow window step cannot starve the click's report. A target is a path and, of the query, only `view`
// and `item` (nothing else, no fragment, no token, no body).
const TAP_ITEM = /^[A-Za-z0-9_.:-]{1,64}$/;
function tapTarget(u) {
  try {
    const x = new URL(u, self.location.origin);
    const p = x.pathname.replace(/[^A-Za-z0-9/_.-]/g, '').slice(0, 60) || '/';
    const v = x.searchParams.get('view'), it = x.searchParams.get('item');
    const q = [];
    if (v !== null && /^[a-z]{1,20}$/.test(v)) q.push('view=' + v);
    if (it !== null && TAP_ITEM.test(it)) q.push('item=' + it);
    // the server's grammar has `item` only after `view` or alone; both orders produced here fit it
    return p + (q.length ? '?' + q.join('&') : '');
  } catch (_) { return null; }
}
// Starts one tap report and records its promise in `work` (the click's waitUntil holds them); total, never throws.
function reportTap(work, ping, kind, extra) {
  try {
    const t = Date.now(), b = receiptBudget(t);
    const clean = {};
    for (const k of Object.keys(extra || {})) if (extra[k] !== null && extra[k] !== undefined) clean[k] = extra[k];
    work.push(boundedReceipts(b, () => sendReceipt(ping, kind, t, b, endpointHash(b), clean)));
  } catch (_) { /* a report never reaches the tap */ }
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

// A new worker whose install could not fetch '/' would otherwise delete the last good shell and leave nothing for an
// outage: before the old caches go, the newest older shell's copy is carried into the current cache when that is empty.
// Every mutation of the shell cache (commit with its snapshot/write/rollback/prune, the activation carry, the runtime
// write-through) runs through this ONE chain, so no two ever interleave. Network fetches and validation stay outside it;
// only cache reads of existing state and the writes run inside. A rejected task is observed by its caller and never poisons
// the chain.
let shellChain = Promise.resolve();
function serial(fn) {
  const run = shellChain.then(fn);
  shellChain = run.then(() => {}, () => {});
  return run;
}
// Cache names carry a version label ('0.11.10', '1105-pwaab-1'): compared run by run, digit runs as NUMBERS (so 0.11.10 is
// newer than 0.11.9), other runs as text. A digit run outranks a text run at the same place, so a label with no digits at all
// (malformed) sorts below every numbered one and is then ordered as plain text. Never throws.
function versionRuns(v) { return String(v).match(/\d+|\D+/g) || []; }
function compareVersions(a, b) {
  const x = versionRuns(a), y = versionRuns(b);
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    if (x[i] === y[i]) continue;
    const nx = /^\d/.test(x[i]), ny = /^\d/.test(y[i]);
    if (nx && ny) { const d = Number(x[i]) - Number(y[i]); if (d !== 0 && !Number.isNaN(d)) return d < 0 ? -1 : 1; if (x[i].length !== y[i].length) return x[i].length < y[i].length ? -1 : 1; continue; }
    if (nx !== ny) return nx ? 1 : -1;
    return x[i] < y[i] ? -1 : 1;
  }
  return x.length === y.length ? 0 : (x.length < y.length ? -1 : 1);
}
function carryShellForward() { return serial(carryShellForwardNow); }
async function carryShellForwardNow() {
  if (typeof caches === 'undefined') return;
  const names = await caches.keys();
  if (names.includes(SHELL_CACHE) && await (await caches.open(SHELL_CACHE)).match(SHELL_KEY)) return;
  for (const n of names.filter((x) => x.startsWith('levain-shell-v') && x !== SHELL_CACHE).sort((p, q) => compareVersions(q.slice('levain-shell-v'.length), p.slice('levain-shell-v'.length)))) {
    const from = await caches.open(n);
    const page = await from.match(SHELL_KEY);
    if (!page) continue;
    // Only the old '/' and exactly the assets it names, each held to the same gate as a fresh write. An old generation
    // that fails it (a missing or query-mismatched asset, an HTML body under a script key) carries NOTHING: the app falls
    // to the network, honest, rather than a half shell. Everything else in the old cache is dropped with it.
    if (!shellCacheable(page)) { drain(page); return; }   // headers only; the body stays unread for the clone below
    const html = await page.clone().text();
    const staged = [];
    for (const a of shellAssets(html)) {
      const hit = await from.match(a.key);
      if (!assetOk(hit, a.kind)) { drain(hit); drain(page); return; }
      staged.push([a.key, new Response(await hit.arrayBuffer(), { status: 200, headers: { 'content-type': hit.headers.get('content-type') } })]);
    }
    const to = await caches.open(SHELL_CACHE);
    for (const [k, r] of staged) await to.put(k, r);
    await to.put(SHELL_KEY, page);
    return;
  }
}

async function dropOldCaches() {
  if (typeof caches === 'undefined') return;
  try { await carryShellForward(); } catch (_) { /* the shell is a convenience; activation never fails on it */ }
  const names = await caches.keys();
  await Promise.all(names.filter((n) => n.startsWith('levain-') && !CACHES_KEEP.includes(n)).map((n) => caches.delete(n)));
}

// No skipWaiting here: a new worker waits until the page, which shows "New version available. Tap to reload.", posts
// {type: 'levain-skip-waiting'} (the first install has no controller and activates at once anyway). The shell precache
// is best effort: a failed or redirected fetch leaves the cache empty and never fails the install.
async function precacheShell() {
  try {
    const r = await fetch(SHELL_KEY, { credentials: 'same-origin', cache: 'reload' });
    if (!shellCacheable(r)) { drain(r); return; }
    await commitShell(r);
  } catch (_) { /* offline at install: the next successful navigation fills it */ }
}
// One coherent generation: '/' is committed only AFTER every CSS/JS its markup names has been fetched and validated, so a
// cached '/' never points at an asset the cache lacks. Any failure commits nothing (the previous generation keeps serving;
// a first install with a failing asset caches nothing and the app behaves as it did before the shell cache).
function drain(r) { try { if (r && r.body) r.body.cancel().catch(() => {}); } catch (_) { /* nothing held */ } }
async function commitShell(resp) {
  let html = '';
  try { html = await resp.clone().text(); } catch (_) { /* no markup: the shell alone */ }
  const assets = shellAssets(html);
  const staged = await Promise.all(assets.map(async (a) => {
    try {
      const r = await fetch(a.key, { credentials: 'same-origin', cache: 'reload' });
      if (!assetOk(r, a.kind)) { drain(r); return null; }   // an unread body holds its connection
      // a versioned url is staged only when its bytes hash to its version, exactly as the write-through path holds it (an old
      // shell naming ?v=OLD answered by a newer or other backend would otherwise sit cache-first under the old name); a
      // mismatch fails this asset like a failed fetch, so nothing is committed. The body is read inside bytesOfVersion.
      if (versionedKey(a.key)) { const ok = await bytesOfVersion(r, a.key, a.kind); return ok ? [a.key, ok] : null; }
      // read the body now: an unread response holds its connection, and a shell of more assets than the browser's per-host
      // limit would wait on itself
      return [a.key, new Response(await r.arrayBuffer(), { status: 200, headers: { 'content-type': r.headers.get('content-type') } })];
    } catch (_) { return null; }
  }));
  if (staged.some((x) => !x)) { drain(resp); return false; }
  return serial(() => writeShell(resp, staged));
}
async function writeShell(resp, staged) {
  const cache = await caches.open(SHELL_CACHE);
  // the live cache is written assets first, '/' last; a put that throws midway puts back what it overwrote (and removes
  // what it added) so an old '/' is never left naming assets of another generation
  const keys = [...staged.map(([k]) => k), SHELL_KEY];
  const before = new Map();
  // status, statusText and EVERY header are kept (a CSP on '/' must survive a restore)
  for (const k of keys) { const h = await cache.match(k); before.set(k, h ? new Response(await h.arrayBuffer(), { status: h.status, statusText: h.statusText, headers: new Headers(h.headers) }) : null); }
  try {
    for (const [k, r] of staged) await cache.put(k, r);
    await cache.put(SHELL_KEY, resp);
  } catch (err) {
    for (const [k, old] of before) { try { if (old) await cache.put(k, old); else await cache.delete(k); } catch (_) { /* best effort */ } }
    throw err;
  }
  // bound the cache: only '/' and the assets it names stay
  try {
    const keep = new Set(keys);
    for (const k of await cache.keys()) { const u = new URL(k.url), key = u.pathname + u.search; if (!keep.has(key)) await cache.delete(key); }
  } catch (_) { /* best effort */ }
  return true;
}
// The attribute-name boundary: `data-src` is not `src` (a plain \b matched it, the hyphen being a word boundary). It consumes the
// character before the name (capture group 1) instead of looking behind: a regex lookbehind throws in Safari before 16.4, and a
// throw here caches no shell. The server's _ATTR_BOUNDARY (levain/web_server.py) is this exact source text; tests/test_shell_asset_versions.py
// asserts they are equal and that no lookbehind appears in the worker's regexes.
const ATTR_BOUNDARY = String.raw`(^|[^\w-])`;
// Same-origin absolute URLs named by <script src> and <link rel=stylesheet href> in the shell html, as {key: path+query, kind}.
// The exact URL (query included) is what the worker caches and what it later intercepts.
function shellAssets(html) {
  const out = new Map();
  const add = (v, kind) => {
    if (!/^\/(?!\/)/.test(v)) return;
    let u; try { u = new URL(v.split('#')[0], 'https://shell.invalid'); } catch (_) { return; }
    if (NO_INTERCEPT.some((p) => u.pathname === p || u.pathname.startsWith(p + '/'))) return;
    const key = u.pathname + u.search;
    if (!out.has(key)) out.set(key, { key, kind });
  };
  const attr = (tag, name) => { const m = new RegExp(ATTR_BOUNDARY + name + '\\s*=\\s*(?:"([^"]+)"|\'([^\']+)\')', 'i').exec(tag); return m ? (m[2] || m[3]) : null; };
  for (const m of html.matchAll(/<script\b[^>]*>/gi)) { const v = attr(m[0], 'src'); if (v) add(v, 'script'); }
  for (const m of html.matchAll(/<link\b[^>]*>/gi)) {
    const rel = attr(m[0], 'rel');
    if (rel && /^stylesheet$/i.test(rel.trim())) { const h = attr(m[0], 'href'); if (h) add(h, 'style'); }
  }
  return [...out.values()];
}
// A 200, not redirected, with a content type that fits the kind: a soft-200 HTML error page or captive portal is not an asset.
function assetOk(r, kind) {
  if (!r || r.status !== 200 || r.redirected) return false;
  const ct = r.headers.get('content-type') || '';
  return kind === 'script' ? /javascript|ecmascript/i.test(ct) : kind === 'style' ? /text\/css/i.test(ct) : false;
}
function shellCacheable(r) {
  return !!r && r.status === 200 && !r.redirected && /text\/html/i.test(r.headers.get('content-type') || '');
}
self.addEventListener('install', (e) => e.waitUntil(precacheShell()));
self.addEventListener('message', (e) => {
  const d = e.data;
  if (d && typeof d === 'object' && d.type === 'levain-skip-waiting') e.waitUntil(self.skipWaiting());
});

// Navigations only: network first, the cached shell (or the offline page) when the network fails. Never intercepted:
// anything but a same-origin GET navigation, and /webpush/ and /cockpit/ (data). Never cached: any response but the bare
// '/' with no query (a credential would ride in a query; the offline page never echoes the URL); a fragment never reaches
// a worker. A navigation to '/' with a query is still answered offline from the cached shell (reading it, not writing it).
const NO_INTERCEPT = ['/webpush', '/cockpit'];
const OFFLINE_HTML = '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Levain</title>' +
  '<style>html,body{margin:0;height:100%;background:#14110f;color:#f1ebe3;font:17px/1.4 -apple-system,system-ui,sans-serif}' +
  'body{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:20px;' +
  'padding:calc(24px + env(safe-area-inset-top)) calc(24px + env(safe-area-inset-right)) calc(24px + env(safe-area-inset-bottom)) calc(24px + env(safe-area-inset-left))}' +
  'a{color:#14110f;background:#f1ebe3;padding:12px 28px;border-radius:12px;text-decoration:none;font-weight:600}</style></head>' +
  '<body><p>Can\'t reach your cockpit. Is Tailscale on?</p><a href="">Retry</a></body></html>';
// How long the worker waits on the network for a navigation or a shell script/style before it answers from the cache (or the
// offline page). A host that is unreachable by SILENCE (the cockpit with Tailscale off: its packets go nowhere and nothing
// refuses them) never makes fetch() reject for the browser's own minute-long timeout; without a deadline the navigation, and
// then every render-blocking script of the cached shell, hung there as a blank white page (1105-7).
// A script/style fetch carries an AbortController signal and is aborted at its deadline, so a timed-out fetch does not stay
// queued on the dead connection pool ahead of a later Retry. A navigation cannot: a navigate-mode request cannot be re-made
// with a signal (Fetch spec: a new Request with a non-empty init turns mode 'navigate' into 'same-origin', a different request
// with its own redirect handling), so its late response is left to
// settle and, if it ever arrives, its body is cancelled unread (drain).
// KNOWN EDGE (kept on purpose, L3 pwa-ab r1): a working network slower than the deadline is treated as down (the cached shell,
// or the offline page whose Retry starts a fresh navigation under a fresh deadline); a longer wait is the hang defect B was.
const NET_TIMEOUT_MS = 4000;
function fetchWithin(req) {
  let timer, timedOut = false;
  const ac = req.mode === 'navigate' ? null : new AbortController();
  const late = new Promise((_, reject) => {
    timer = setTimeout(() => { timedOut = true; if (ac) ac.abort(); reject(new Error('network deadline')); }, NET_TIMEOUT_MS);
  });
  const pending = ac ? fetch(req, { signal: ac.signal }) : fetch(req);
  // the loser of the race may settle later: a rejection is observed (never unhandled), a late response is released unread
  pending.then((r) => { if (timedOut) drain(r); }, () => {});
  return Promise.race([pending, late]).finally(() => clearTimeout(timer));
}
function offlineResponse() {
  return new Response(OFFLINE_HTML, { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } });
}
async function navigateNetworkFirst(e, req, bare) {
  try {
    const r = await fetchWithin(req);
    if (bare && shellCacheable(r)) {
      // off the critical path: the page never waits on Cache Storage (a slow or full cache costs the copy, not the page)
      const copy = r.clone();
      e.waitUntil((async () => { try { await commitShell(copy); } catch (_) { /* best effort; '/' is replaced only with its assets */ } })());
    }
    return r;
  } catch (err) {
    if (new URL(req.url).pathname === SHELL_KEY) {
      try {
        const hit = await (await caches.open(SHELL_CACHE)).match(SHELL_KEY);
        if (hit) return hit;
      } catch (_) { /* fall through to the offline page */ }
    }
    return offlineResponse();
  }
}
// The shell's own scripts and styles. The cockpit page names each as <path>?v=<digest of the bytes served>, so the exact URL
// IS the version (1105-17): a cached copy of a VERSIONED url the cached '/' names, that passes assetOk, is answered from the
// cache with no network wait, online or off; there is nothing to revalidate and no state beyond the cache itself. A miss, an
// unversioned url or a copy that fails assetOk goes to the network under its own deadline, is written through to the shell
// cache OFF the response path (the worker's cache.put ignores the server's no-store) and only for an exact URL the cached
// shell names, with a content type that fits. A cold open with a dead network costs ONE deadline: the navigation's.
async function cachedAsset(key, kind) {
  try {
    // NOT serialized: a held or never-settling mutation must not hang an offline script/style request. KNOWN EDGE: this
    // check-then-read can race a commit, so an offline request racing one may serve one old asset once, or answer a network error once when the commit's prune lands between the check and the read.
    const c = await caches.open(SHELL_CACHE);
    const page = await c.match(SHELL_KEY);
    if (!page || !shellAssets(await page.text()).some((a) => a.key === key)) return null;
    const hit = (await c.match(key)) || null;
    if (hit && !assetOk(hit, kind)) { drain(hit); return null; }
    return hit;
  } catch (_) { return null; }
}
// An unversioned url names no fixed bytes (a deploy changes them under the same name), so it is never served without the
// network's say; only the digest in the url makes cache-first correct.
// The server's digest is always the first 12 hex of the SHA-256 of the bytes it serves (levain/web_server.py _asset_digest).
function versionOf(key) { try { const v = new URL(key, 'https://shell.invalid').searchParams.get('v') || ''; return /^[0-9a-f]{12}$/i.test(v) ? v.toLowerCase() : ''; } catch (_) { return ''; } }
function versionedKey(key) { return !!versionOf(key); }
// A network answer for a versioned url is cached only when its bytes ARE that version (an old page's ?v=OLD answered by a
// newer deploy, or by another backend, would otherwise sit cache-first under the old name). Returns the response to store
// (the body is read once here), or null on a mismatch / any failure; the caller's page response is unaffected.
async function bytesOfVersion(copy, key, kind) {
  try {
    const want = versionOf(key);
    const buf = await copy.arrayBuffer();
    const hex = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', buf)), (b) => b.toString(16).padStart(2, '0')).join('');
    if (!want || hex.slice(0, 12) !== want) return null;
    return new Response(buf, { status: 200, headers: { 'content-type': copy.headers.get('content-type') || (kind === 'style' ? 'text/css' : 'application/javascript') } });
  } catch (_) { return null; }
}
async function assetCacheFirst(e, req, key, kind) {
  if (versionedKey(key)) {
    const hit = await cachedAsset(key, kind);
    if (hit) return hit;
  }
  try {
    const r = await fetchWithin(req);
    if (assetOk(r, kind)) {
      const copy = r.clone();
      e.waitUntil(serial(async () => {
        try {
          const c = await caches.open(SHELL_CACHE);
          const hit = await c.match(SHELL_KEY);
          if (hit && shellAssets(await hit.text()).some((a) => a.key === key)) {
            // a versioned url is stored only when its bytes hash to the version; an unversioned one is stored as before
            const toStore = versionedKey(key) ? await bytesOfVersion(copy, key, kind) : copy;
            if (toStore) await c.put(key, toStore);
            else drain(copy);
          } else drain(copy);
        } catch (_) { drain(copy); /* best effort; the unread tee branch is released */ }
      }));
    }
    return r;
  } catch (err) {
    return (await cachedAsset(key, kind)) || Response.error();
  }
}
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (!req || req.method !== 'GET') return;
  let u;
  try { u = new URL(req.url); } catch (_) { return; }
  if (u.origin !== self.location.origin) return;
  if (NO_INTERCEPT.some((p) => u.pathname === p || u.pathname.startsWith(p + '/'))) return;
  if (req.mode !== 'navigate') {
    if (req.destination === 'script' || req.destination === 'style') e.respondWith(assetCacheFirst(e, req, u.pathname + u.search, req.destination));
    return;
  }
  e.respondWith(navigateNetworkFirst(e, req, u.pathname === SHELL_KEY && !u.search));
});
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

// The app-icon badge: the push payload's `badge` is the open Needs-you count at send time (never a notice count), the only
// source the worker has (it holds no credential). A plain int > 0 sets it, 0 clears it, anything else (absent, not an int)
// leaves the icon as it is. Feature-detected and caught: a failing badge never touches the notification.
function applyBadge(n) {
  try {
    if (typeof n !== 'number' || !Number.isInteger(n) || n < 0) return Promise.resolve();
    const nav = self.navigator;
    if (!nav) return Promise.resolve();
    if (n > 0) return typeof nav.setAppBadge === 'function' ? Promise.resolve(nav.setAppBadge(n)).catch(() => {}) : Promise.resolve();
    return typeof nav.clearAppBadge === 'function' ? Promise.resolve(nav.clearAppBadge()).catch(() => {}) : Promise.resolve();
  } catch (_) { return Promise.resolve(); }
}

// Whether the user agent holds the notification just asked for: getNotifications({tag}) lists one with this exact tag
// AND this push's ping in its data (the tag is compared here too: an older WebKit ignored the filter and listed every
// notification of the app). 2026-11-05, ping 409:
// all four devices reported `shown` for a notification none of them displayed, because `shown` then meant only that
// showNotification resolved, and a resolved promise is the UA accepting the request (WHATWG Notifications: the promise
// resolves after the show steps hand it to the platform, with no check that it appeared). The LIMIT that remains: a page
// can learn only that the UA holds the notification, never that the OS drew it (a Focus that silences the app, a
// scheduled summary, a banner the OS chose not to show), so `shown` means "the device holds it", and nothing that must
// prove Phill SAW a ping may rest on `shown` alone: the evidence for that is a tap (notificationclick -> an `opens` row)
// or his answer on the sheet. Absent, unlistable (no getNotifications, a throw, past the budget): no `shown`, and the
// trace says `show-missing` with the tag's length and its leading word only (no content).
async function heldByAgent(tag, ping, b) {
  const miss = (why, count) => {
    trace('show-missing', { why, tagLen: tag.length, tagKind: (/^[A-Za-z]{1,16}/.exec(tag) || [''])[0], count: count == null ? null : count });
    return false;
  };
  try {
    const reg = self.registration;
    if (!reg || typeof reg.getNotifications !== 'function') return miss('no-api');
    const list = await b.within(() => reg.getNotifications({ tag }));
    if (list === LATE) return miss('late');
    const all = Array.from(list || []);
    // the tag alone is not enough: a re-ping reuses the ref as its tag (renotify), so an EARLIER ping's notification can sit
    // under the same tag while the platform swallowed this one. The options carry data.ping; it must be this push's
    return all.some((n) => n && n.tag === tag && n.data && Number(n.data.ping) === ping) || miss('absent', all.length);
  } catch (_) { return miss('error'); }
}

self.addEventListener('push', (e) => {
  const t0 = Date.now();
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: e.data ? e.data.text() : '' }; }
  const title = d.title || 'Levain';
  // a fixed tag with renotify: a re-ping for the same item replaces the earlier notification and alerts again. The
  // notification is asked for FIRST, before any trace or receipt, and never waits on either; it does not race the
  // receipt budget (only receipts do)
  // The launch fallback's entry (sw_open.js) is made DURABLE BEFORE the notification can be tapped: its write starts first and the
  // show waits for it, at most SHOWN_WRITE_WAIT_MS (a slow cache never delays the notification beyond that). A show that
  // rejects takes its entry away again (below), so an entry never outlives a notification that was not drawn.
  const pend = startShownPending(d);
  const ask = () => self.registration.showNotification(title, {
    body: d.body || '',
    tag: d.tag || 'levain',
    renotify: true,
    icon: '/icon-192.png',
    badge: '/badge-96.png',   // monochrome alpha: Android draws only the alpha channel in the status bar
    data: { url: d.url || '/', ping: d.ping },
  });
  let shown;
  if (!pend) {
    try { shown = Promise.resolve(ask()); } catch (err) { shown = Promise.reject(err); }
  } else {
    shown = (async () => { await waitBounded(pend.written, SHOWN_WRITE_WAIT_MS); return ask(); })();
  }
  // A rejected show takes its entry away, and the delete is CHAINED AFTER the write settles (however late it lands), so a slow put
  // can never recreate the entry the cleanup deleted; the event only waits a bound for it. A resolved show replaces any OLDER
  // entry under the same tag (a re-ping reuses the tag: the platform swapped the notification, so the older entry is stale).
  const shownCleanup = pend ? shown.then(
    () => waitBounded(pend.written.then((ok) => (ok ? pruneSuperseded(pend.tag, pend.ping) : false)), 2 * SHOWN_WRITE_WAIT_MS),
    () => waitBounded(pend.written.then(() => dropKey(pend.key)), 2 * SHOWN_WRITE_WAIT_MS)) : undefined;
  const badged = applyBadge(d.badge);   // after the notification is asked for; its failure is its own
  trace('sw-version', { version: SW_VERSION, on: 'push' });
  trace('push', { tag: traceScalar(d.tag || 'levain'), ping: traceScalar(d.ping) });
  const ping = typeof d.ping === 'number' ? d.ping : Number(d.ping);
  // then the receipts, all under one budget from the event's start: received now, shown when the notification resolved
  // AND the user agent still holds it (heldByAgent), and the queue re-sent after both, only when the server answered one
  const b = receiptBudget(t0);
  const receipts = boundedReceipts(b, async () => {
    const hashP = endpointHash(b);   // one hash for this event's reports, asked now
    const tag = d.tag || 'levain';
    const r = await Promise.all([sendReceipt(ping, 'received', t0, b, hashP),
                                 shown.then(() => heldByAgent(tag, ping, b), () => false)
                                   .then((held) => (held ? sendReceipt(ping, 'shown', Date.now(), b, hashP) : false))
                                   .catch(() => false)]);   // total: a throw in this arm would reject Promise.all, and the budget's end would abort `received` too
    if (r.some((x) => x === true)) await drainReceipts(b);
  });
  e.waitUntil(Promise.allSettled([shown, badged, receipts, shownCleanup, pend && waitBounded(pend.pruned, 2 * SHOWN_WRITE_WAIT_MS)]).finally(traceSettled));
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
// for: 'shown' = the launch fallback for a tap whose notificationclick never runs (iOS Home Screen app, 2026-11-06: pings 607-609
//                were shown, no click step, the app opened at '/'). ONE IMMUTABLE ENTRY PER PING, key OPEN_PREFIX + 'shown-' +
//                ping + '-' + nonce, value {url, nonce, at, for, ping, tag}: a later push writes another key and never replaces
//                or races an earlier one, and every delete names exactly the key it read (the page) or its own ping's keys (a
//                click). Written BEFORE the notification can be tapped (see the push handler) and removed if the show rejects.
//                At most SHOWN_MAX are kept (the oldest go). Only a page's first load may take one (sw_open.js), and only at a
//                bare standalone '/' within SHOWN_TTL_MS with the ping's notification gone from the tray. A click that does run
//                deletes its ping's entries (dropShownPending).
const SHOWN_PREFIX = OPEN_PREFIX + 'shown-', SHOWN_MAX = 10, SHOWN_TTL_MS = 180000, SHOWN_WRITE_WAIT_MS = 1500;
const waitBounded = (p, ms) => new Promise((res) => {
  const t = setTimeout(res, ms);
  p.then(() => { clearTimeout(t); res(); }, () => { clearTimeout(t); res(); });
});
// total: returns {key, written, pruned} (written resolves true once the entry is stored, never rejects), or null when this push gets no entry
function startShownPending(d) {
  try {
    if (!d || !d.url || typeof d.url !== 'string') return null;
    const tag = d.tag || 'levain';
    if (tag === 'summary') return null;   // a held summary links a view and is no item (sender.py: tag = ref or kind)
    const ping = typeof d.ping === 'number' ? d.ping : Number(d.ping);
    if (!Number.isInteger(ping) || ping <= 0) return null;   // the page cannot tell whether it was tapped without the ping
    let u;
    try { u = new URL(d.url, self.location.origin); } catch (_) { u = null; }
    const url = u && u.origin === self.location.origin ? u.pathname + u.search + u.hash : '/';
    const nonce = newNonce();
    const key = SHOWN_PREFIX + ping + '-' + nonce;
    const entry = JSON.stringify({ url, nonce, at: Date.now(), for: 'shown', ping, tag: String(tag) });
    const written = (async () => {
      try {
        const c = await caches.open(OPEN_CACHE);
        await c.put(key, new Response(entry));
        trace('shown-pending', { ping, url: traceUrl(url) });
        return true;
      } catch (err) {
        trace('shown-pending-failed', { error: err && err.name || 'error' });
        return false;
      }
    })();
    const pruned = written.then((ok) => (ok ? pruneShown() : false)).catch(() => false);
    return { key, written, pruned, tag: String(tag), ping };
  } catch (err) {
    trace('shown-pending-failed', { error: err && err.name || 'error' });
    return null;
  }
}
// the fixed singleton key of the first version of the fallback: no ping suffix, never written now, always removed
const LEGACY_SHOWN_KEY = OPEN_PREFIX + 'shown';
async function shownKeys(c) {
  return (await c.keys()).filter((r) => new URL(r.url).pathname.indexOf(SHOWN_PREFIX) === 0);
}
// keep the newest SHOWN_MAX live entries: older, expired and unreadable ones go (each delete names the request it read)
async function pruneShown() {
  try {
    const c = await caches.open(OPEN_CACHE);
    try { await c.delete(LEGACY_SHOWN_KEY); } catch (_) { /* swept by its TTL on the page */ }
    const rows = [];
    for (const req of await shownKeys(c)) {
      let at = null;
      try { const r = await c.match(req); const e = r && await r.json(); at = e && typeof e.at === 'number' ? e.at : null; } catch (_) { at = null; }
      if (at === null || Date.now() - at > SHOWN_TTL_MS) { await c.delete(req); continue; }
      rows.push({ req, at });
    }
    rows.sort((x, y) => y.at - x.at);
    for (const x of rows.slice(SHOWN_MAX)) await c.delete(x.req);
    return true;
  } catch (_) { return false; }
}
// this push's notification replaced the earlier one(s) under its tag: their entries (an OLDER ping, same tag) go
async function pruneSuperseded(tag, ping) {
  try {
    const c = await caches.open(OPEN_CACHE);
    let any = false;
    for (const req of await shownKeys(c)) {
      let e = null;
      try { const r = await c.match(req); e = r && await r.json(); } catch (_) { e = null; }
      if (e && e.tag === tag && Number.isInteger(e.ping) && e.ping < ping && await c.delete(req)) any = true;
    }
    return any;
  } catch (_) { return false; }
}
async function dropKey(key) {
  try { const c = await caches.open(OPEN_CACHE); return await c.delete(key); } catch (_) { return false; }
}
// a tap that DID fire its click owns the open: that ping's entries go (a later bare launch is not redirected twice); other pings' stay
async function dropShownPending(ping) {
  try {
    const n = typeof ping === 'number' ? ping : Number(ping);
    if (!Number.isInteger(n)) return false;
    const c = await caches.open(OPEN_CACHE);
    const mine = SHOWN_PREFIX + n + '-';
    let any = false;
    for (const req of await shownKeys(c)) {
      if (new URL(req.url).pathname.indexOf(mine) === 0 && await c.delete(req)) any = true;
    }
    return any;
  } catch (_) { return false; }
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
  // the server-side record of this tap (see reportTap): `clicked` is started before the window step and never waits on it, `opened` once with how
  const tapWork = [], tapPing = typeof data.ping === 'number' ? data.ping : Number(data.ping), tapTo = tapTarget(url);
  reportTap(tapWork, tapPing, 'clicked', { target: tapTo });
  let openedReported = false;
  const reportOpened = (how) => {
    if (openedReported) return;
    openedReported = true;
    reportTap(tapWork, tapPing, 'opened', { target: tapTo, how });
  };
  const openedInner = async () => {
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
        reportOpened(w ? 'openWindow' : 'refused');   // before the hold below: the report must not wait NEW_TTL_MS
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
        reportOpened('refused');
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
      reportOpened('ack');
      console.info('levain sw: tap -> ack');
      return;
    }
    // no ack: claim the asked entry (the exact key this tap wrote) or find the page already took it (a late ack)
    if (askedKey) {
      let mine;
      // a cache that throws cannot have been claimed by a page that needs the same cache: the worker owns the open
      try { mine = await dropPending(askedKey); } catch (_) { mine = true; }
      trace('no-ack-claim', { mine });
      if (!mine) { reportOpened('late-ack'); console.info('levain sw: tap -> ack (late)'); return; }
    }
    if (ctl.includes(c)) {
      try { await c.focus(); } catch (_) { /* navigate anyway */ }
      try {
        // accepted when the resolved client is at the target (origin + path + query); null or another page is a navigation that did not happen
        const r = await c.navigate(url);
        trace('navigate', { result: r ? traceUrl(r.url) : null, ok: !!(r && sameTarget(r.url, url)) });
        if (r && sameTarget(r.url, url)) { reportOpened('navigate'); console.info('levain sw: tap -> navigate'); return; }
      } catch (err) { trace('navigate', { error: err && err.name || 'error' }); /* fall through to a new window */ }
    }
    await openNew('no ack');
  };
  // whatever path ended without a report is `none` (the window step ran out, or threw)
  const opened = openedInner().finally(() => reportOpened('none'));
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
  const dropShown = dropShownPending(data.ping);
  // the tap reports are pushed as the window step runs: held after it settles, each under its own budget
  const tapDone = opened.catch(() => {}).then(() => Promise.allSettled(tapWork));
  e.waitUntil(Promise.allSettled([opened, mark, dropShown, tapDone, boundedReceipts(rb, () => drainReceipts(rb))]).then(traceSettled));
});
