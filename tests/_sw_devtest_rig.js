// Runs sw.js's devtest notificationclick branch (band 299) in a vm and prints what it did.
// usage: node _sw_devtest_rig.js <path to sw.js> <windows: none|hidden|visible> <devtest: 1|0> <action>
const fs = require('fs');
const vm = require('vm');
const [swPath, windows, devtest, action] = process.argv.slice(2);
const listeners = {};
const log = { closed: false, focused: 0, navigated: [], opened: [], fetches: 0, trace: [], otherPath: false };
const stores = new Map();
const caches = {
  keys: async () => [...stores.keys()], delete: async (n) => stores.delete(n),
  open: async (n) => {
    if (!stores.has(n)) stores.set(n, new Map());
    const m = stores.get(n);
    return { put: async (k, r) => { m.set(k, await r.text()); }, keys: async () => [...m.keys()].map((url) => ({ url: 'https://box.example' + url })),
      match: async (req) => { const k = new URL(req.url).pathname; return m.has(k) ? new Response(m.get(k)) : undefined; }, delete: async (k) => m.delete(k) };
  },
};
const win = { url: 'https://box.example/', visibilityState: windows === 'visible' ? 'visible' : 'hidden', focused: windows === 'visible',
  focus: async () => { log.focused += 1; }, navigate: async (u) => { log.navigated.push(u); }, postMessage: () => { log.otherPath = true; } };
const self_ = {
  addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); }, skipWaiting() {}, location: { origin: 'https://box.example' },
  registration: { showNotification: async () => {}, getNotifications: async () => [], pushManager: { getSubscription: async () => null } },
  clients: { claim: async () => {}, matchAll: async () => (windows === 'none' ? [] : [win]), openWindow: async (u) => { log.opened.push(u); return { url: u }; } },
};
const sandbox = { self: self_, indexedDB: { open() { log.otherPath = true; return {}; } }, caches, Response, Headers, Math, URL, clearTimeout, MessageChannel, TextEncoder,
  AbortController, crypto: require('crypto').webcrypto, console: { info() {}, warn() {} }, setTimeout, Date, Promise, JSON, String, Array,
  fetch: () => { log.fetches += 1; throw new Error('no fetch'); } };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(swPath, 'utf8'), sandbox);
(async () => {
  const waits = [];
  const ev = { action: action === '-' ? '' : action, notification: { close: () => { log.closed = true; }, data: devtest === '1' ? { devtest: true, url: '/devtest' } : { url: '/', ping: 9 } },
    waitUntil: (p) => waits.push(Promise.resolve(p)) };
  for (const fn of listeners.notificationclick) fn(ev);
  await Promise.race([Promise.all(waits), new Promise((r) => setTimeout(r, 400))]);   // an ordinary ping tap may wait on an ack that never comes
  const tr = stores.get('levain-trace') || new Map();
  log.trace = [...tr.entries()].map(([k, v]) => JSON.parse(v));
  console.log(JSON.stringify(log));
  process.exit(0);
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
