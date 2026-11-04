// Runs levain/webpush/static/sw.js in a vm with a stub IndexedDB, Cache Storage and clients, then prints what it did.
// usage: node _sw_worker_rig.js <path to sw.js> activate|click|navigate|push
// The stubs keep only what the worker uses; there is no fetch, so a worker that calls fetch() throws and the rig records it.
const fs = require('fs');
const vm = require('vm');

const [swPath, scenario, mode] = process.argv.slice(2);
const listeners = {};
const log = { console: [], asked: [], posts: [], order: [], shownCalls: [], fetches: 0, opened: [], posted: [], longWaits: [], claimed: false, skipWaiting: false, windows: [], navigated: [], focused: 0 };
let traceFail = false;   // 'tracefail' in the mode: every open of the trace cache throws
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
const cacheCtypes = new Map();
const cacheHeaders = new Map(); // 'cache|key' -> every header of the stored response
// (cacheCtypes above: 'cache|key' -> content-type, when a test pins one)
let putFailKey = null;           // the next put to this key in a shell cache throws once
const cacheStores = new Map();   // cache name -> Map(request key -> body text)
const caches = {
  keys: async () => [...cacheNames],
  delete: async (n) => cacheNames.delete(n),
  open: async (n) => {
    if (n === 'levain-trace' && traceFail) throw new TypeError('the trace cache refused');
    cacheNames.add(n);
    if (!cacheStores.has(n)) cacheStores.set(n, new Map());
    const m = cacheStores.get(n);
    const guess = (k) => (k === '/' ? 'text/html' : /\.css(\?|$)/.test(k) ? 'text/css' : /\.js(\?|$)/.test(k) ? 'application/javascript' : '');
    return {
      put: async (k, r) => {
        if (n.startsWith('levain-shell-v') && putFailKey === k) { putFailKey = null; throw new TypeError('quota'); }
        cacheCtypes.set(n + '|' + k, r.headers.get('content-type') || guess(k)); cacheHeaders.set(n + '|' + k, [...r.headers.entries()]); m.set(k, await r.text());
      },
      match: async (k) => (m.has(k) ? new Response(m.get(k), { headers: [...(cacheHeaders.get(n + '|' + k) || []), ['content-type', cacheCtypes.get(n + '|' + k) || guess(k)]].reduce((h, [a, b]) => { h.set(a, b); return h; }, new Headers()) }) : undefined),
      delete: async (k) => m.delete(k),
      keys: async () => [...m.keys()].map((k) => ({ url: 'https://box.example' + k })),
    };
  },
};

const traceSteps = () => [...(cacheStores.get('levain-trace') || new Map()).entries()].sort(([a], [b]) => (a < b ? -1 : 1)).map(([k, v]) => ({ key: k, ...JSON.parse(v) }));
const pendings = () => [...(cacheStores.get('levain-open') || new Map()).entries()].map(([k, v]) => ({ key: k, ...JSON.parse(v) }));

// --- clients ---------------------------------------------------------------------------------------------------------
const windowClient = {
  url: 'https://box.example/', focus: async () => {}, navigate: async () => {},
  postMessage: (m, tr) => { if (m && m.type === 'levain-open') { log.asked.push(m.url); tr[0].postMessage({ ok: true }); } else log.posted.push(m); },
};
const self_ = {
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  skipWaiting: () => { log.skipWaiting = true; },
  location: { origin: 'https://box.example' },
  registration: {
    showNotification: async (title, o) => {
      log.order.push('show'); log.shownCalls.push({ title, tag: o.tag });
      if (log.showThrows) throw new Error('showNotification refused');
    },
    pushManager: { getSubscription: async () => log.subscription },
  },
  clients: {
    claim: async () => { log.claimed = true; },
    matchAll: async (o) => log.windows.concat((o && o.includeUncontrolled) ? uncontrolled : []),
    openWindow: async (u) => { log.openWindow = true; log.openedUrl = u; log.openedAll = (log.openedAll || []).concat([u]); log.entriesAtOpen = pendings(); return { url: u }; },
  },
};
const sandbox = {
  self: self_, indexedDB, caches, Response, Headers, Math,
  console: { info: (m) => log.console.push(['info', String(m)]), warn: (m) => log.console.push(['warn', String(m)]) },
  URL, clearTimeout, MessageChannel, TextEncoder, AbortController, crypto: require('crypto').webcrypto,
  // a wait of 5 s or more is recorded and shortened, so the worker's NEW_TTL_MS hold after a successful openWindow does not slow the rig
  setTimeout: (fn, ms, ...a) => setTimeout(fn, ms >= 5000 ? (log.longWaits.push(ms), 50) : ms, ...a), Date, Promise, JSON, String, Array,
  fetch: () => { log.fetches += 1; throw new Error('the worker must not call fetch'); },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(swPath, 'utf8'), sandbox);

// the receipt scenarios' fetch: records the body, then behaves as `behaviour` says (ok | throw | hang | <status number>)
let behaviour = 'ok';
sandbox.fetch = (url, init) => {
  log.fetches += 1; log.order.push('post');
  log.posts.push({ url, method: init.method, body: JSON.parse(init.body), contentType: init.headers['Content-Type'], hasSignal: !!init.signal });
  if (behaviour === 'throw') return Promise.reject(new TypeError('network down'));
  if (behaviour === 'hang') return new Promise((_, rej) => init.signal.addEventListener('abort', () => rej(new Error('aborted'))));
  if (behaviour === 'slow') return new Promise((res, rej) => {   // 200 after 1.5 s, unless the worker aborts it first
    let done = false;   // the signal is shared by the event's every fetch: count an abort only of one still in flight
    const t = setTimeout(() => { done = true; res({ status: 200 }); }, 1500);
    init.signal.addEventListener('abort', () => { if (done) return; done = true; clearTimeout(t); log.aborted = (log.aborted || 0) + 1; rej(new Error('aborted')); });
  });
  if (behaviour === 'deaf') return new Promise(() => {});   // never settles, never honours the abort
  const status = behaviour === 'ok' ? 200 : Number(behaviour);
  return Promise.resolve({ status });
};

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
    for (const n of ['levain-old-shell', 'levain-sw-cache', 'levain-open', 'other-app-cache']) cacheNames.add(n);
    // the pre-fix4 singleton key, and a current per-entry key in the same cache
    cacheStores.set('levain-open', new Map([['/__levain_open', '{"url":"/sign/1"}'], ['/__levain_open/abc', '{"url":"/sign/2","for":"asked"}']]));
    await fire('activate', {});
    log.openKeys = [...cacheStores.get('levain-open').keys()].sort();
    log.dbs = [...dbs.keys()].sort();
    log.caches = [...cacheNames].sort();
    log.keptOpened = [...(dbs.get('levain-sw-2').stores.get('opened').entries())];
  } else if (scenario === 'navigate') {
    // mode: <window>[+ack][+two] ; "+two" fires a second tap (url /sign/10) 100 ms after the first ; window = controlled | uncontrolled | reject | null | mismatch | crossorigin. "+ack" means the
    // page runs sw_open.js (acks the worker's levain-open message); without it the page is silent, as an old cached page is.
    const parts = mode.split('+');
    traceFail = parts.includes('tracefail');
    const [kind, ack, extra] = parts.filter((p) => p !== 'tracefail');
    // ack: absent = silent page; 'ack' = acks at once; 'late' = acks after OPEN_ACK_MS (the page claims the pending target first)
    const post = function (m, tr) {
      if (m && m.type === 'levain-open') {
        log.askedWho = (log.askedWho || []).concat([this.url]);
        log.asked.push(m.url); log.nonces = (log.nonces || []).concat([m.nonce]);
        if (ack === 'ack') tr[0].postMessage({ ok: true });
        if (ack === 'late') setTimeout(async () => {
          // what sw_open.js does: delete the key the message names, ack only if that delete took it
          const c = await caches.open('levain-open');
          if (await c.delete('/__levain_open/' + m.nonce)) { log.lateClaimed = true; tr[0].postMessage({ ok: true }); } else log.lateClaimed = false;
        }, 700);
      } else log.posted.push(m);
    };
    const win = (nav, u) => ({ url: u || 'https://box.example/cockpit', visibilityState: 'visible', focused: true, focus: async () => { log.focused += 1; }, navigate: nav, postMessage: post });
    const ok = async (u) => { log.navigated.push(u); return { url: u }; };
    if (kind === 'controlled' || kind === 'crossorigin') log.windows = [win(ok)];
    else if (kind === 'uncontrolled') uncontrolled = [win(async () => { throw new TypeError("This service worker is not the client's active service worker."); })];
    else if (kind === 'reject') log.windows = [win(async () => { throw new TypeError('navigate refused'); })];
    else if (kind === 'null') log.windows = [win(async (u) => { log.navigated.push(u); return null; })];
    else if (kind === 'mismatch') log.windows = [win(async (u) => { log.navigated.push(u); return { url: 'https://box.example/elsewhere' }; })];
    else if (kind === 'fragment') log.windows = [win(async (u) => { log.navigated.push(u); return { url: 'https://box.example/sign/9' }; })];
    else if (kind === 'search') log.windows = [win(async (u) => { log.navigated.push(u); return { url: 'https://box.example/' }; }, 'https://box.example/')];
    else if (kind === 'matchfail') { self_.clients.matchAll = async () => { throw new TypeError('matchAll refused'); }; }
    else if (kind === 'opennull') { log.windows = []; self_.clients.openWindow = async (u) => { log.openAttempt = u; log.entriesAtOpen = pendings(); return null; }; }
    else if (kind === 'openfail') { log.windows = []; self_.clients.openWindow = async (u) => { log.openAttempt = u; throw new TypeError('openWindow refused'); }; }
    else if (kind === 'secreturl') log.windows = [win(ok, 'https://box.example/?write_token=SECRET&view=needs#frag')];
    else if (kind === 'tapwin') log.windows = [win(ok, 'https://box.example/tap_trace')];
    else if (kind === 'tapwin2') log.windows = [win(ok, 'https://box.example/tap_trace'), win(ok, 'https://box.example/docs/guide')];
    else if (kind === 'prefer') log.windows = [win(ok, 'https://box.example/docs/guide'), win(async (u) => { log.navigated.push('cockpit:' + u); return { url: u }; }, 'https://box.example/sign/3')];
    const url = kind === 'crossorigin' ? 'https://evil.example/x' : kind === 'search' ? '/?view=needs' : kind === 'secreturl' ? '/?write_token=SECRET&view=needs#frag' : '/sign/9#sign=a&v=1';
    const first = fire('notificationclick', { notification: { close: () => {}, data: { url, ping: kind === 'secreturl' ? { write_token: 'SECRET' } : undefined, tag: kind === 'secreturl' ? 'x'.repeat(100) : undefined } } });
    if (extra === 'two') { await new Promise((r) => setTimeout(r, 100)); await fire('notificationclick', { notification: { close: () => {}, data: { url: '/sign/10' } } }); }
    await first;
    if (ack === 'late') await new Promise((r) => setTimeout(r, 900));   // let the late page act
    log.trace = traceSteps();
    log.pendings = pendings();
    log.pendingLeft = log.pendings.length;
    if (log.pendings.length) log.pending = log.pendings[0];
  } else if (scenario === 'push') {
    // mode: <behaviour>[+<flag>...]; flags: nosub noping showthrows seed<N> (N queued entries, new key form, a scrambled
    // age order) seedold (one entry under the pre-fix key form) twice (the first push fires twice, the same ping) subhang
    // (getSubscription never answers) cachehang (the receipts cache never opens) then:<behaviour> (after the first push, the fetch behaves so and a `click` or a second `push` event runs:
    // `then:ok+click` / `then:ok+push`) rotate (the subscription's endpoint changes before that second event)
    const parts = mode.split('+');
    behaviour = parts[0];
    log.subscription = parts.includes('nosub') ? null : { endpoint: 'https://fcm.googleapis.com/fcm/send/dev1' };
    log.showThrows = parts.includes('showthrows');
    if (parts.includes('subhang')) self_.registration.pushManager.getSubscription = () => new Promise(() => {});
    if (parts.includes('cachehang')) {
      const realOpen = caches.open;
      caches.open = (n) => (n === 'levain-receipts' ? new Promise(() => {}) : realOpen(n));
    }
    const seed = parts.find((p) => /^seed\d+$/.test(p));
    if (seed || parts.includes('seedold')) {
      const m = new Map();
      const n = seed ? Number(seed.slice(4)) : 0;
      log.seededAts = [];
      for (let i = 0; i < n; i++) {
        const at = Date.now() - 3_600_000 + ((i * 7) % n) * 1000;
        log.seededAts.push(at);
        m.set('/__levain_receipt/' + (100 + i) + '-received-' + String(at).padStart(13, '0') + '-0000',
              JSON.stringify({ ping: 100 + i, kind: 'received', at, endpoint_sha256: 'a'.repeat(64) }));
      }
      if (parts.includes('seedold')) m.set('/__levain_receipt/99-shown', JSON.stringify({ ping: 99, kind: 'shown', at: 1_750_000_000_000, endpoint_sha256: 'b'.repeat(64) }));
      if (parts.includes('seedstale')) { const at = Date.now() - 3 * 86400000; m.set('/__levain_receipt/98-received-' + String(at).padStart(13, '0') + '-0000', JSON.stringify({ ping: 98, kind: 'received', at, endpoint_sha256: 'c'.repeat(64) })); }
      cacheNames.add('levain-receipts'); cacheStores.set('levain-receipts', m);
    }
    const data = parts.includes('noping') ? { title: 'T', body: 'secret body' } : { title: 'T', body: 'secret body', ping: 42, url: '/', tag: 'x' };
    const started = Date.now();
    const evPush = () => ({ data: { json: () => data } });
    if (!parts.includes('clickonly')) {
      await fire('push', evPush());
      if (parts.includes('twice')) await fire('push', evPush());
    }
    log.elapsedMs = Date.now() - started;
    log.afterFirst = { posts: log.posts.length, queued: [...(cacheStores.get('levain-receipts') || new Map()).keys()].sort() };
    log.firstPosts = log.posts;
    const then = parts.find((p) => p.startsWith('then:'));
    if (then) {
      behaviour = then.slice(5);
      log.posts = [];
      log.windows = [windowClient];
      if (parts.includes('rotate')) log.subscription = { endpoint: 'https://fcm.googleapis.com/fcm/send/dev2' };
      const t1 = Date.now();
      if (parts.includes('click')) await fire('notificationclick', { notification: { close: () => {}, data: { url: '/' } } });
      if (parts.includes('push')) await fire('push', evPush());
      log.thenMs = Date.now() - t1;
    }
    if (parts.includes('clickonly')) {
      log.windows = [windowClient];
      const t1 = Date.now();
      await fire('notificationclick', { notification: { close: () => {}, data: { url: '/' } } });
      log.clickMs = Date.now() - t1;
    }
    log.queued = [...(cacheStores.get('levain-receipts') || new Map()).keys()].sort();
    log.trace = traceSteps();
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
  } else if (scenario === 'install') {
    // mode: ok | down | redirected | assets | badasset | softhtml, optionally ':prior' (an older generation exists; activation runs after)
    const [md, prior] = mode.split(':');
    const ctypeOf = (path) => (/\.css(\?|$)/.test(path) ? 'text/css' : 'application/javascript');
    if (prior === 'stale') {
      // an older generation produced by the vulnerable worker: '/' names assets, one missing, one query-mismatched, one HTML under a script key, plus a stray
      cacheStores.set('levain-shell-vOLD', new Map([['/', '<link rel="stylesheet" href="/dashboard.css"><script src="/b.js?v=2"></script><script src="/c.js"></script>'],
        ['/dashboard.css', 'OLD CSS'], ['/b.js', 'OLD B NOQUERY'], ['/c.js', '<html>portal</html>'], ['/stray.js', 'STRAY']]));
      cacheCtypes.set('levain-shell-vOLD|/c.js', 'text/html'); cacheNames.add('levain-shell-vOLD');
    } else if (prior === 'good') {
      cacheStores.set('levain-shell-vOLD', new Map([['/', '<link rel="stylesheet" href="/dashboard.css"><script src="/b.js?v=2"></script>'],
        ['/dashboard.css', 'OLD CSS'], ['/b.js?v=2', 'OLD B'], ['/stray.js', 'STRAY']]));
      cacheNames.add('levain-shell-vOLD');
    } else if (prior) {
      cacheStores.set('levain-shell-vOLD', new Map([['/', '<html>old shell</html>'], ['/dashboard.css', 'OLD CSS'], ['/a.js', 'OLD A']]));
      cacheNames.add('levain-shell-vOLD');
    }
    sandbox.fetch = async (url) => {
      log.fetched = (log.fetched || []).concat([String(url)]);
      if (md === 'down') throw new TypeError('network down');
      const withAssets = ['assets', 'badasset', 'softhtml'].includes(md);
      if (withAssets && String(url) !== '/') {
        const spy = (r) => { Object.defineProperty(r, 'body', { value: { cancel: () => { log.cancelled = (log.cancelled || 0) + 1; return Promise.resolve(); } } }); return r; };
        if (md === 'badasset' && String(url) === '/dashboard.css') return spy(new Response('nope', { status: 404, headers: { 'content-type': 'text/css' } }));
        if (md === 'softhtml' && String(url) === '/a.js') return spy(new Response('<html>portal</html>', { status: 200, headers: { 'content-type': 'text/html' } }));
        return new Response('ASSET ' + url, { status: 200, headers: { 'content-type': ctypeOf(String(url)) } });
      }
      const html = withAssets
        ? '<link rel="stylesheet" href="/dashboard.css"><script src="/a.js"></script><script src="https://cdn.example/x.js"></script>' +
          '<script src="/webpush/z.js"></script><link rel="icon" href="/icon.png"><script src="/b.js?v=2"></script>' +
          "<script src='/q.js'></script><link rel='stylesheet' href='/q.css'>"
        : '<html>shell</html>';
      const r = new Response(html, { status: 200, headers: { 'content-type': 'text/html' } });
      if (md === 'redirected') Object.defineProperty(r, 'redirected', { value: true });
      return r;
    };
    await fire('install', {});
    if (prior) await fire('activate', {});
    log.shells = [...cacheStores.entries()].filter(([n]) => n.startsWith('levain-shell-v') && cacheNames.has(n)).map(([n, m]) => [n, [...m.entries()]]);
  } else if (scenario === 'carry') {
    // an older shell exists, the current one is empty (its install fetch failed): activation carries it forward
    cacheStores.set('levain-shell-vOLD', new Map([['/', '<html>old shell</html>']])); cacheNames.add('levain-shell-vOLD');
    await fire('activate', {});
    log.caches = [...cacheNames].sort();
    log.shells = [...cacheStores.entries()].filter(([n]) => n.startsWith('levain-shell-v') && cacheNames.has(n)).map(([n, m]) => [n, [...m.entries()]]);
  } else if (scenario === 'message') {
    await fire('message', { data: JSON.parse(mode) });
  } else if (scenario === 'fetch') {
    // mode: JSON {url, method, mode, net: 'down'|'ok'|'500', shell: bool}; records respondWith, the response and the cache writes
    const m = JSON.parse(mode);
    const shellName = 'levain-shell-v' + /SW_VERSION = '([^']+)'/.exec(fs.readFileSync(swPath, 'utf8'))[1];
    if (m.shell) { cacheStores.set(shellName, new Map([['/', m.shellHtml || '<html>cached shell</html>']])); cacheNames.add(shellName); }
    if (m.shellHeaders) cacheHeaders.set(shellName + '|/', Object.entries(m.shellHeaders));
    for (const k of m.stray || []) cacheStores.get(shellName).set(k, 'STRAY');
    if (m.putFail) putFailKey = m.putFail;
    if (m.cached) { cacheStores.get(shellName).set(m.cached, 'CACHED ' + m.cached); }
    sandbox.fetch = async (req) => {
      log.fetchedNet = (log.fetchedNet || 0) + 1;
      if (m.net === 'down') throw new TypeError('network down');
      const path = new URL(typeof req === 'string' ? req : req.url, 'https://box.example');
      const key = path.pathname + path.search;
      if (key !== '/' && /\.(css|js)$/.test(path.pathname)) {
        if ((m.assetFail || []).includes(key)) return new Response('nope', { status: 404, headers: { 'content-type': 'text/css' } });
        return new Response('LIVE ' + key, { status: 200, headers: { 'content-type': m.ctype || (/\.css$/.test(path.pathname) ? 'text/css' : 'application/javascript') } });
      }
      return new Response(m.html || '<html>live</html>', { status: m.net === '500' ? 500 : 200, headers: { 'content-type': 'text/html' } });
    };
    const ev = { request: { url: m.url, method: m.method || 'GET', mode: m.mode || 'navigate', destination: m.destination || 'document' } };
    let handed = null;
    ev.respondWith = (p) => { handed = Promise.resolve(p); };
    ev.waitUntil = (p) => { (log.waits = log.waits || []).push(Promise.resolve(p)); };
    for (const fn of listeners.fetch || []) fn(ev);
    log.responded = handed !== null;
    if (handed) {
      const r = await handed;
      log.status = r.status; log.ctype = r.headers.get('content-type'); log.body = await r.text();
    }
    await Promise.all(log.waits || []);
    log.shells = [...cacheStores.entries()].filter(([n]) => n.startsWith('levain-shell-v')).map(([n, mm]) => [n, [...mm.entries()]]);
    log.rootCsp = (cacheHeaders.get(shellName + '|/') || []).filter(([h]) => h === 'content-security-policy').map(([, v]) => v);
  } else if (scenario === 'overlap') {
    // two navigations to '/' in flight together; runs[i] = {html, putFail}: the i-th '/' fetch answers that html
    const m = JSON.parse(mode);
    const shellName = 'levain-shell-v' + /SW_VERSION = '([^']+)'/.exec(fs.readFileSync(swPath, 'utf8'))[1];
    cacheStores.set(shellName, new Map([['/', m.shellHtml]])); cacheNames.add(shellName);
    let nav = 0;
    sandbox.fetch = async (req) => {
      const p = new URL(typeof req === 'string' ? req : req.url, 'https://box.example'), key = p.pathname + p.search;
      if (key !== '/') return new Response('LIVE ' + key, { status: 200, headers: { 'content-type': /\.css$/.test(key) ? 'text/css' : 'application/javascript' } });
      return new Response(m.runs[nav++].html, { status: 200, headers: { 'content-type': 'text/html' } });
    };
    if (m.putFail) putFailKey = m.putFail;
    const waits = [];
    for (let i = 0; i < m.runs.length; i++) {
      const ev = { request: { url: 'https://box.example/', method: 'GET', mode: 'navigate', destination: 'document' } };
      let handed = null;
      ev.respondWith = (p) => { handed = Promise.resolve(p); };
      ev.waitUntil = (p) => { waits.push(Promise.resolve(p)); };
      for (const fn of listeners.fetch || []) fn(ev);
      await handed;
    }
    await Promise.all(waits);
    log.shells = [...cacheStores.entries()].filter(([n]) => n.startsWith('levain-shell-v')).map(([n, mm]) => [n, [...mm.entries()]]);
  } else if (scenario === 'hold') {
    // mode: JSON {shellHtml, key, hang: bool, putFail: bool}. An ONLINE asset request writes through (its put hangs forever when
    // `hang`, throws when `putFail`; the clone's body cancel is recorded), then the network drops and an OFFLINE request for
    // the same asset must still answer while that write-through is pending.
    const m = JSON.parse(mode);
    const shellName = 'levain-shell-v' + /SW_VERSION = '([^']+)'/.exec(fs.readFileSync(swPath, 'utf8'))[1];
    cacheStores.set(shellName, new Map([['/', m.shellHtml], [m.key, 'CACHED ' + m.key]])); cacheNames.add(shellName);
    const realOpen = caches.open;
    caches.open = async (n) => {
      const c = await realOpen(n);
      if (!n.startsWith('levain-shell-v')) return c;
      const put = c.put;
      return { ...c, put: async (k, r) => { if (k !== m.key) return put(k, r); if (m.hang) return new Promise(() => {}); if (m.putFail) throw new TypeError('quota'); return put(k, r); } };
    };
    let up = true;
    sandbox.fetch = async () => {
      if (!up) throw new TypeError('network down');
      const r = new Response('LIVE ' + m.key, { status: 200, headers: { 'content-type': /\.css/.test(m.key) ? 'text/css' : 'application/javascript' } });
      const realClone = r.clone.bind(r);
      r.clone = () => { const c = realClone(); const cancel = c.body.cancel.bind(c.body); c.body.cancel = (...a) => { log.copyDrained = true; return cancel(...a); }; return c; };
      return r;
    };
    const send = async () => {
      const ev = { request: { url: 'https://box.example' + m.key, method: 'GET', mode: 'no-cors', destination: /\.css/.test(m.key) ? 'style' : 'script' } };
      let handed = null; const waits = [];
      ev.respondWith = (p) => { handed = Promise.resolve(p); };
      ev.waitUntil = (p) => { waits.push(Promise.resolve(p)); };
      for (const fn of listeners.fetch || []) fn(ev);
      return { handed, waits };
    };
    const first = await send();
    const live = await first.handed; log.liveBody = await live.text();
    await new Promise((r) => setTimeout(r, 20));        // let the write-through reach its put
    up = false;
    const second = await send();
    const off = await Promise.race([second.handed.then(async (r) => ({ status: r.status, body: await r.text() })), new Promise((r) => setTimeout(() => r({ timeout: true }), 500))]);
    log.offline = off;
    if (!m.hang) await Promise.all(first.waits);
  }
  console.log(JSON.stringify(log));
  if (scenario === 'hold') process.exit(0);
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
