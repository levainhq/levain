// Runs levain/webpush/static/sw.js in a vm with a stub IndexedDB, Cache Storage and clients, then prints what it did.
// usage: node _sw_worker_rig.js <path to sw.js> activate|click
// The stubs keep only what the worker uses; there is no fetch, so a worker that calls fetch() throws and the rig records it.
const fs = require('fs');
const vm = require('vm');

const [swPath, scenario, mode] = process.argv.slice(2);
const listeners = {};
const log = { asked: [], fetches: 0, opened: [], posted: [], claimed: false, skipWaiting: false, windows: [], navigated: [], focused: 0 };
let uncontrolled = [];   // windows only matchAll({includeUncontrolled: true}) returns

// --- IndexedDB: databases by name, each with object stores that are Maps ---------------------------------------------
const dbs = new Map();   // name -> {version, stores: Map(storeName -> Map)}
function makeDb(rec) {
  return {
    objectStoreNames: { contains: (n) => rec.stores.has(n) },
    createObjectStore: (n) => { rec.stores.set(n, new Map()); return {}; },
    transaction(store) {
      const m = rec.stores.get(store);
      const tx = { oncomplete: null, onerror: null, onabort: null };
      tx.objectStore = () => ({
        put: (value, key) => { m.set(key, value); setTimeout(() => tx.oncomplete && tx.oncomplete(), 0); },
        get: (key) => { const r = { result: m.get(key) }; setTimeout(() => { r.onsuccess && r.onsuccess(); tx.oncomplete && tx.oncomplete(); }, 0); return r; },
      });
      return tx;
    },
    close() {},
  };
}
const indexedDB = {
  open(name, version) {
    const req = {};
    setTimeout(() => {
      let rec = dbs.get(name);
      const fresh = !rec;
      if (fresh) { rec = { version, stores: new Map() }; dbs.set(name, rec); }
      req.result = makeDb(rec);
      if (fresh && req.onupgradeneeded) req.onupgradeneeded();
      req.onsuccess && req.onsuccess();
    }, 0);
    return req;
  },
  deleteDatabase(name) {
    const req = {};
    setTimeout(() => { dbs.delete(name); req.onsuccess && req.onsuccess(); }, 0);
    return req;
  },
};

// --- Cache Storage ---------------------------------------------------------------------------------------------------
const cacheNames = new Set();
const caches = {
  keys: async () => [...cacheNames],
  delete: async (n) => cacheNames.delete(n),
};

// --- clients ---------------------------------------------------------------------------------------------------------
const windowClient = {
  url: 'https://box.example/', focus: async () => {}, navigate: async () => {},
  postMessage: (m, tr) => { if (m && m.type === 'levain-open') { log.asked.push(m.url); tr[0].postMessage({ ok: true }); } else log.posted.push(m); },
};
const self_ = {
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  skipWaiting: () => { log.skipWaiting = true; },
  location: { origin: 'https://box.example' },
  registration: { showNotification: async () => {} },
  clients: {
    claim: async () => { log.claimed = true; },
    matchAll: async (o) => log.windows.concat((o && o.includeUncontrolled) ? uncontrolled : []),
    openWindow: async (u) => { log.openWindow = true; log.openedUrl = u; },
  },
};
const sandbox = {
  self: self_, indexedDB, caches, URL, setTimeout, clearTimeout, MessageChannel, Date, Promise, JSON, String, Array,
  fetch: () => { log.fetches += 1; throw new Error('the worker must not call fetch'); },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(swPath, 'utf8'), sandbox);

function fire(type, ev) {
  const waits = [];
  ev.waitUntil = (p) => waits.push(Promise.resolve(p));
  for (const fn of listeners[type] || []) fn(ev);
  return Promise.all(waits);
}

(async () => {
  log.listeners = Object.keys(listeners).sort();
  if (scenario === 'activate') {
    // the retired phone-port database (it held the phone token), the new one, and caches of several names
    dbs.set('levain-sw', { version: 1, stores: new Map([['kv', new Map([['token', 'phone-token']])]]) });
    dbs.set('levain-sw-2', { version: 1, stores: new Map([['opened', new Map([['7', 1]])]]) });
    for (const n of ['levain-old-shell', 'levain-sw-cache', 'other-app-cache']) cacheNames.add(n);
    await fire('activate', {});
    log.dbs = [...dbs.keys()].sort();
    log.caches = [...cacheNames].sort();
    log.keptOpened = [...(dbs.get('levain-sw-2').stores.get('opened').entries())];
  } else if (scenario === 'navigate') {
    // mode: <window>[+ack] ; window = controlled | uncontrolled | reject | null | mismatch | crossorigin. "+ack" means the
    // page runs sw_open.js (acks the worker's levain-open message); without it the page is silent, as an old cached page is.
    const [kind, ack] = mode.split('+');
    const post = (m, tr) => { if (m && m.type === 'levain-open') { log.asked.push(m.url); if (ack) tr[0].postMessage({ ok: true }); } else log.posted.push(m); };
    const win = (nav) => ({ url: 'https://box.example/cockpit', focus: async () => { log.focused += 1; }, navigate: nav, postMessage: post });
    const ok = async (u) => { log.navigated.push(u); return { url: u }; };
    if (kind === 'controlled' || kind === 'crossorigin') log.windows = [win(ok)];
    else if (kind === 'uncontrolled') uncontrolled = [win(async () => { throw new TypeError("This service worker is not the client's active service worker."); })];
    else if (kind === 'reject') log.windows = [win(async () => { throw new TypeError('navigate refused'); })];
    else if (kind === 'null') log.windows = [win(async (u) => { log.navigated.push(u); return null; })];
    else if (kind === 'mismatch') log.windows = [win(async (u) => { log.navigated.push(u); return { url: 'https://box.example/elsewhere' }; })];
    const url = kind === 'crossorigin' ? 'https://evil.example/x' : '/sign/9#sign=a&v=1';
    await fire('notificationclick', { notification: { close: () => {}, data: { url } } });
  } else if (scenario === 'click') {
    log.windows = [windowClient];
    let closed = false;
    await fire('notificationclick', { notification: { close: () => { closed = true; }, data: { url: '/?view=needs', ping: 42 } } });
    log.closed = closed;
    const rec = dbs.get('levain-sw-2');
    log.db = rec ? { version: rec.version, stores: [...rec.stores.keys()] } : null;
    log.opened = rec && rec.stores.has('opened') ? [...rec.stores.get('opened').entries()] : [];
    // a tap with no ping writes nothing
    await fire('notificationclick', { notification: { close: () => {}, data: { url: '/' } } });
    log.openedAfterNoPing = [...dbs.get('levain-sw-2').stores.get('opened').entries()].length;
  }
  console.log(JSON.stringify(log));
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
