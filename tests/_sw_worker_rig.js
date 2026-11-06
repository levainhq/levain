// Runs levain/webpush/static/sw.js in a vm with a stub IndexedDB, Cache Storage and clients, then prints what it did.
// usage: node _sw_worker_rig.js <path to sw.js> activate|click|navigate|push
// The stubs keep only what the worker uses; there is no fetch, so a worker that calls fetch() throws and the rig records it.
const fs = require('fs');
const vm = require('vm');

const [swPath, scenario, mode] = process.argv.slice(2);
const listeners = {};
const log = { console: [], asked: [], posts: [], order: [], shownCalls: [], held: [], getNotificationsCalls: [], fetches: 0, opened: [], posted: [], longWaits: [], claimed: false, skipWaiting: false, windows: [], navigated: [], focused: 0 };
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
    // what the user agent holds: a resolved show adds one (a same-tag show replaces it), unless the platform swallowed it
    // (log.swallowed: the promise resolves and nothing is held, the way a notification no device drew looks from inside)
    showNotification: async (title, o) => {
      log.order.push('show'); log.shownCalls.push({ title, tag: o.tag });
      if (log.showThrows) throw new Error('showNotification refused');
      if (!log.swallowed) { log.held = log.held.filter((n) => n.tag !== o.tag).concat([{ title, tag: o.tag, data: o.data }]); }
    },
    getNotifications: async (filter) => {
      log.getNotificationsCalls.push(filter === undefined ? null : filter);
      if (log.getnThrows) throw new TypeError('getNotifications refused');
      const tag = filter && filter.tag;
      // 'getnall': a filter the UA ignores (an old WebKit returned every notification of the app whatever the tag)
      return log.held.filter((n) => log.getnAll || !tag || n.tag === tag).map((n) => ({ title: n.title, tag: n.tag, data: n.data }));
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
    log.swallowed = parts.includes('swallowed');
    log.getnThrows = parts.includes('getnthrows');
    log.getnAll = parts.includes('getnall');
    if (parts.includes('nogetn')) delete self_.registration.getNotifications;
    if (parts.includes('othertag')) log.held = [{ title: 'other', tag: 'np-other' }];   // the UA holds another ping's notification
    // 'staleheld': the UA still holds an EARLIER ping's notification under this push's tag (a re-ping reuses the tag with
    // renotify); with 'swallowed' the new show leaves it as it was. 'foreigndata': held under the tag with no ping in its data
    if (parts.includes('staleheld')) log.held = [{ title: 'old', tag: 'x', data: { url: '/', ping: 41 } }];
    if (parts.includes('foreigndata')) log.held = [{ title: 'old', tag: 'x', data: { url: '/' } }];
    if (parts.includes('nodata')) log.held = [{ title: 'old', tag: 'x' }];
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
    // 'p409': the shape of ping 409 (2026-11-05, kind consent), rebuilt from the push record's field lengths: the title
    // 'levain', a 57-character `request:pend-<stamp>-<hex>` tag, the center link, an int ping and a badge; the body is a
    // stand-in of the recorded length class, not the recorded text
    const p409 = { title: 'levain', body: 'secret body ' + 'x'.repeat(180), tag: 'request:pend-2026-11-05T19:54:58.556594Z-2690252d9c8e0d77',
                   url: '/?view=notifications&n=p409', ping: 409, badge: 1 };
    const data = parts.includes('noping') ? { title: 'T', body: 'secret body' }
      : parts.includes('p409') ? p409 : { title: 'T', body: 'secret body', ping: 42, url: '/', tag: 'x' };
    // 'taghostile': a tag whose length throws, so heldByAgent's own failure path throws too (nothing real sends one: the
    // flag exists to reach the chain's trailing catch)
    if (parts.includes('taghostile')) data.tag = { get length() { if (!log.hostileTagDone) throw new TypeError('hostile tag'); return 7; }, toString: () => 'hostile' };
    // 'nourl': no url in the payload; 'summary': the held-summary tag; 'crossurl': an off-origin url; 'second': a second push (ping 43) after the first;
    // 'clickping': the `then:...+click` carries ping 42 (as a real notification's data does), 'clickother': ping 99
    if (parts.includes('nourl')) delete data.url;
    if (parts.includes('summary')) data.tag = 'summary';
    if (parts.includes('crossurl')) data.url = 'https://evil.example/x';
    const started = Date.now();
    const evPush = () => ({ data: { json: () => data } });
    if (!parts.includes('clickonly')) {
      await fire('push', evPush());
      if (parts.includes('twice')) await fire('push', evPush());
      if (parts.includes('second')) { data.ping = 43; data.tag = 'y'; data.url = '/?view=notifications&n=p43'; await fire('push', evPush()); }
    }
    log.shownAfterPush = pendings().filter((p) => p.for === 'shown');
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
      if (parts.includes('click')) await fire('notificationclick', { notification: { close: () => {}, data: { url: '/', ...(parts.includes('clickping') ? { ping: 42 } : parts.includes('clickother') ? { ping: 99 } : {}) } } });
      if (parts.includes('push')) await fire('push', evPush());
      log.thenMs = Date.now() - t1;
    }
    if (parts.includes('clickonly')) {
      log.windows = [windowClient];
      const t1 = Date.now();
      await fire('notificationclick', { notification: { close: () => {}, data: { url: '/' } } });
      log.clickMs = Date.now() - t1;
    }
    log.shownAfterClick = pendings().filter((p) => p.for === 'shown');
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
    // verok | verbad: the shell names ONE versioned script; the digest in its url is the real one (verok) or another (verbad)
    const VER_BODY = 'VERSIONED BYTES';
    const verKey = '/v.js?v=' + (md === 'verok' ? require('crypto').createHash('sha256').update(VER_BODY).digest('hex').slice(0, 12) : '0123456789ab');
    sandbox.fetch = async (url) => {
      log.fetched = (log.fetched || []).concat([String(url)]);
      if (md === 'down') throw new TypeError('network down');
      if (md === 'verok' || md === 'verbad') {
        return String(url) === '/' ? new Response('<script src="' + verKey + '"></script>', { status: 200, headers: { 'content-type': 'text/html' } })
          : new Response(VER_BODY, { status: 200, headers: { 'content-type': 'application/javascript' } });
      }
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
    // mode: JSON list of extra older generations' version labels; each holds a page that names itself
    for (const v of mode ? JSON.parse(mode) : []) { cacheStores.set('levain-shell-v' + v, new Map([['/', '<html>shell ' + v + '</html>']])); cacheNames.add('levain-shell-v' + v); }
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
        return new Response(m.assetBody !== undefined ? m.assetBody : 'LIVE ' + key, { status: 200, headers: { 'content-type': m.ctype || (/\.css$/.test(path.pathname) ? 'text/css' : 'application/javascript') } });
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
  } else if (scenario === 'deadline') {
    // the network ANSWERS NOTHING (Tailscale off): a navigation to '/' waits out the worker's deadline and gets the cached shell;
    // then the shell's cached asset must answer from the cache with no network wait, an uncached asset waits its own deadline
    // and its fetch is ABORTED; the navigation's late answer is drained; then the network returns and both go network-first again.
    // mode: JSON {shellHtml, cached}. Steps record {ms, status, body}; net calls record {key, signal}; aborts record the key.
    const m = JSON.parse(mode);
    const shellName = 'levain-shell-v' + /SW_VERSION = '([^']+)'/.exec(fs.readFileSync(swPath, 'utf8'))[1];
    cacheStores.set(shellName, new Map([['/', m.shellHtml], [m.cached, 'CACHED ' + m.cached]])); cacheNames.add(shellName);
    let net = 'hang';
    const late = [];
    log.netCalls = []; log.aborted = []; log.steps = []; log.lateDrained = 0;
    sandbox.fetch = (req, init) => {
      const p = new URL(req.url, 'https://box.example'), key = p.pathname + p.search;
      log.netCalls.push({ key, signal: !!(init && init.signal) });
      if (net === 'ok') {
        return Promise.resolve(key === '/' ? new Response('<html>live</html>', { status: 200, headers: { 'content-type': 'text/html' } })
          : new Response('LIVE ' + key, { status: 200, headers: { 'content-type': /\.css/.test(key) ? 'text/css' : 'application/javascript' } }));
      }
      return new Promise((res, rej) => {
        late.push({ key, res });
        if (init && init.signal) init.signal.addEventListener('abort', () => { log.aborted.push(key); rej(new Error('aborted')); });
      });
    };
    const send = async (url, mode_, destination) => {
      const ev = { request: { url: 'https://box.example' + url, method: 'GET', mode: mode_, destination } };
      let handed = null;
      ev.respondWith = (p) => { handed = Promise.resolve(p); };
      ev.waitUntil = () => {};
      const t0 = Date.now();
      for (const fn of listeners.fetch || []) fn(ev);
      const r = await handed;
      log.steps.push({ url, ms: Date.now() - t0, status: r.status, body: r.status ? await r.text() : null });
    };
    const kindOf = (u) => (/\.css/.test(u) ? 'style' : 'script');
    await send('/', 'navigate', 'document');
    await send(m.cached, 'no-cors', kindOf(m.cached));
    await send(m.uncached, 'no-cors', kindOf(m.uncached));
    for (const l of late.filter((x) => x.key === '/')) {
      const r = new Response('<html>late</html>', { status: 200, headers: { 'content-type': 'text/html' } });
      Object.defineProperty(r, 'body', { value: { cancel: () => { log.lateDrained += 1; return Promise.resolve(); } } });
      l.res(r);
    }
    await new Promise((r) => setTimeout(r, 20));
    net = 'ok';
    await send('/', 'navigate', 'document');
    await send(m.cached, 'no-cors', kindOf(m.cached));
  } else if (scenario === 'hungasset') {
    // the URL is the version (1105-17): the network never answers and the timers are FAKE (none fires unless the scenario fires
    // it), so an asset answered from the cache can be told from one that waited on the deadline by whether a deadline timer
    // was ever scheduled. mode: JSON {shellHtml, cached, uncached, cachedCtype?}. Records, per step, the status/body, the
    // network calls it made and the deadline timers (ms >= 4000) it scheduled; the uncached step then fires its timer, and a
    // closing sweep fires what is left and records `leaked` ({liveTimers, unabortedFetches}).
    const m = JSON.parse(mode);
    const shellName = 'levain-shell-v' + /SW_VERSION = '([^']+)'/.exec(fs.readFileSync(swPath, 'utf8'))[1];
    cacheStores.set(shellName, new Map([['/', m.shellHtml], [m.cached, 'CACHED ' + m.cached]])); cacheNames.add(shellName);
    if (m.cachedCtype) cacheCtypes.set(shellName + '|' + m.cached, m.cachedCtype);
    const timers = []; let calls = 0; log.aborted = []; log.steps = [];
    sandbox.setTimeout = (fn, ms) => { const t = { fn, ms, live: true }; timers.push(t); return t; };
    sandbox.clearTimeout = (t) => { if (t) t.live = false; };
    sandbox.fetch = (req, init) => {
      calls += 1;
      return new Promise((res, rej) => { if (init && init.signal) init.signal.addEventListener('abort', () => { log.aborted.push(req.url); rej(new Error('aborted')); }); });
    };
    const kindOf = (u) => (/\.css/.test(u) ? 'style' : 'script');
    const ask = async (url, fire) => {
      const before = { calls, timers: timers.length };
      const ev = { request: { url: 'https://box.example' + url, method: 'GET', mode: 'no-cors', destination: kindOf(url) } };
      let handed = null;
      ev.respondWith = (p) => { handed = Promise.resolve(p); };
      ev.waitUntil = () => {};
      for (const fn of listeners.fetch || []) fn(ev);
      if (fire) { await new Promise((r) => setImmediate(r)); for (const t of timers.slice(before.timers)) if (t.live && t.ms >= 4000) { t.live = false; t.fn(); } }
      const r = await Promise.race([handed, new Promise((res) => setImmediate(() => setImmediate(() => setImmediate(() => res(null)))))]);
      const resolved = r !== null;
      log.steps.push({ url, resolved, status: resolved ? r.status : null, body: resolved && r.status ? await r.text() : null, net: calls - before.calls, deadlines: timers.slice(before.timers).filter((t) => t.ms >= 4000).length });
    };
    await ask(m.cached, false);
    await ask(m.uncached, false);   // pends on the network: nothing resolves it until the deadline fires
    await ask(m.uncached, true);    // the same request again, this time firing its deadline timer
    // clean up after itself: the second step's request is still pending with its deadline timer live; fire what is left so its
    // fetch is aborted, then record what could still be held (the test asserts nothing is)
    for (const t of timers) if (t.live) { t.live = false; t.fn(); }
    await new Promise((r) => setImmediate(r));
    log.leaked = { liveTimers: timers.filter((t) => t.live).length, unabortedFetches: calls - log.aborted.length };
  } else if (scenario === 'navoff') {
    // a scenario of ordered requests against a fake clock (the flag it was written for is deleted; it now drives the cached-asset type check). mode: JSON {shellHtml, cached, cachedCtype?, ops}; each op is
    // {t, url, nav?, net: 'down'|'ok'}: at fake time t (ms), request url (a navigation when nav) with the network down or up.
    // Steps record {url, status, body, net} where net is the number of network calls that step made.
    const m = JSON.parse(mode);
    const shellName = 'levain-shell-v' + /SW_VERSION = '([^']+)'/.exec(fs.readFileSync(swPath, 'utf8'))[1];
    cacheStores.set(shellName, new Map([['/', m.shellHtml], [m.cached, 'CACHED ' + m.cached]])); cacheNames.add(shellName);
    if (m.cachedCtype) cacheCtypes.set(shellName + '|' + m.cached, m.cachedCtype);
    let clock = 1_000_000, net = 'ok', calls = 0;
    sandbox.Date = { now: () => clock };
    sandbox.fetch = async (req) => {
      calls += 1;
      if (net === 'down') throw new TypeError('network down');
      const p = new URL(req.url, 'https://box.example'), key = p.pathname + p.search;
      if (key === '/') return new Response('<html>live</html>', { status: 200, headers: { 'content-type': 'text/html' } });
      return new Response('LIVE ' + key, { status: 200, headers: { 'content-type': /\.css/.test(key) ? 'text/css' : 'application/javascript' } });
    };
    log.steps = [];
    for (const op of m.ops) {
      clock = 1_000_000 + op.t; net = op.net;
      const before = calls;
      const ev = { request: { url: 'https://box.example' + op.url, method: 'GET', mode: op.nav ? 'navigate' : 'no-cors', destination: op.nav ? 'document' : (/\.css/.test(op.url) ? 'style' : 'script') } };
      let handed = null; const waits = [];
      ev.respondWith = (p) => { handed = Promise.resolve(p); };
      ev.waitUntil = (p) => { waits.push(Promise.resolve(p)); };
      for (const fn of listeners.fetch || []) fn(ev);
      const r = await handed;
      log.steps.push({ url: op.url, status: r.status, body: r.status ? await r.text() : null, net: calls - before });
      await Promise.all(waits);
    }
  }
  log.hostileTagDone = true;   // the hostile tag stops throwing once the worker is done, so the log can be written
  console.log(JSON.stringify(log));
  if (scenario === 'hold') process.exit(0);
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
