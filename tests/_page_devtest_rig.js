// Runs templates/web/devtest.js (band 299) in a vm with a fake DOM, Cache Storage, fetch and service worker, and prints what it did.
// usage: node _page_devtest_rig.js <path to devtest.js> '<scenario json>'
// scenario: {entries: {key: obj}, sent: "", plan: [status...] (reports; last repeats), sendStatus: 200, active: "new"|"old"|"none",
//            matchThrow: {key: n} (cache.match of that key throws the first n times),
//            steps: ["load"|"visible"|"visible2"|"run10"|"swmsg"]}
const fs = require('fs');
const vm = require('vm');
const [jsPath, raw] = process.argv.slice(2);
const sc = Object.assign({ entries: {}, sent: '', plan: [200], sendStatus: 200, active: 'new', steps: ['load'] }, JSON.parse(raw));
const out = { posts: [], sends: [], lines: [], sent: null, skipWaiting: 0, errors: [] };
const store = new Map(Object.entries(sc.entries).map(([k, v]) => [k, JSON.stringify(v)]));
const ls = new Map(); if (sc.sent) ls.set('levain_devtest_sent_key', sc.sent); ls.set('levain_write_token', 'tok');
let reportCalls = 0;
const listeners = {}, docListeners = {}, swListeners = {};
const el = (id) => ({ id, addEventListener: (t, fn) => { (listeners[id] = listeners[id] || {})[t] = fn; } });
const logEl = { prepend: (p) => out.lines.push(p.textContent) };
const doc = { getElementById: (id) => (id === 'dt-log' ? logEl : el(id)), createElement: () => ({ className: '', textContent: '' }),
  visibilityState: 'visible', addEventListener: (t, fn) => { docListeners[t] = fn; } };
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const active = sc.active === 'none' ? null : { postMessage: (m, ports) => {
  if (sc.active === 'new' && m.type === 'devtest-version') setTimeout(() => ports[0].postMessage({ devtest: true, version: 'V-NEW' }), 5);
  if (m.type === 'levain-skip-waiting') out.skipWaiting += 1;
} };
const reg = { active, waiting: sc.waiting ? { postMessage: () => { out.skipWaiting += 1; } } : null, update: async () => {} };
const sandbox = {
  document: doc, window: { localStorage: { getItem: (k) => (ls.has(k) ? ls.get(k) : null), setItem: (k, v) => ls.set(k, String(v)) }, matchMedia: () => ({ matches: false }) },
  navigator: { userAgent: 'iPhone', standalone: true, serviceWorker: { getRegistration: async () => reg, register: async () => reg, ready: Promise.resolve(reg),
    addEventListener: (t, fn) => { swListeners[t] = fn; } } },
  Notification: Object.assign(function () {}, { permission: 'granted', maxActions: 2, requestPermission: async () => 'granted' }),
  MessageChannel: class { constructor() { const a = {}, b = {}; this.port1 = a; this.port2 = { postMessage: (d) => a.onmessage && a.onmessage({ data: d }) }; void b; } },
  URL, JSON, String, Number, Promise, Object, Array, Math, Date, setTimeout, clearTimeout, console,
  caches: { open: async () => ({ keys: async () => [...store.keys()].map((k) => ({ url: 'https://box.example' + k })), match: async (k) => {
    const key = typeof k === 'string' ? k : new URL(k.url).pathname; await delay(sc.slow || 0);
    if ((sc.matchThrow || {})[key] > 0) { sc.matchThrow[key] -= 1; throw new Error('transient cache read'); } return store.has(key) ? { json: async () => JSON.parse(store.get(key)) } : undefined; } }) },
  fetch: async (path, opts) => {
    const body = JSON.parse(opts.body);
    if (path === '/webpush/devtest/send') { out.sends.push(body); return { status: sc.sendStatus }; }
    const status = sc.plan[Math.min(reportCalls, sc.plan.length - 1)]; reportCalls += 1;
    out.posts.push(body);
    if (status === 'throw') throw new Error('net');
    return { status };
  },
};
sandbox.window.document = doc;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox);
(async () => {
  for (const s of sc.steps) {
    if (s === 'load') await delay(30);
    else if (s === 'visible') docListeners.visibilitychange();
    else if (s === 'visible2') { docListeners.visibilitychange(); docListeners.visibilitychange(); }
    else if (s === 'swmsg') swListeners.message({ data: { type: 'devtest-drain' } });
    else if (s === 'run10') { listeners['dt-run10'].click(); await delay(sc.active === 'old' ? 2900 : 150); }
    else if (s === 'run10x2') { listeners['dt-run10'].click(); listeners['dt-run10'].click(); await delay(sc.active === 'old' ? 2900 : 150); }
    else if (s === 'wait') await delay(150);
    else if (s === 'late') { for (const [k, v] of Object.entries(sc.late || {})) store.set(k, JSON.stringify(v)); }
  }
  await delay(150);
  out.sent = ls.get('levain_devtest_sent_key') || '';
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
