// Runs levain/templates/web/sw_open.js in a vm with a stub page; prints what the page did.
// usage: node _sw_open_rig.js <path to sw_open.js> [messages|claim|load|nocache]
//   messages (default): the message path with no pending target (message-only), cross-origin and malformed messages
//   claim:   messages carrying a nonce claim the pending target; a lost or wrong claim does nothing, a second one too
//   load:    a page loaded / shown with a pending target forwards itself; expired and already-here targets do not move it
//   nocache: no Cache Storage (an insecure context) -> message-only behaviour even with a nonce
const fs = require('fs');
const vm = require('vm');
const [swOpen, scenario = 'messages'] = process.argv.slice(2);
const handlers = [], winHandlers = {}, docHandlers = {};
const log = { assigned: [], acks: [], started: false };
const store = new Map();
const cache = {
  match: async (k) => (store.has(k) ? new Response(store.get(k)) : undefined),
  delete: async (k) => store.delete(k),
};
const KEY = '/__levain_open';
const put = (e) => store.set(KEY, JSON.stringify(e));
const sandbox = {
  URL, Response, Date, Promise, JSON, setTimeout,
  location: { origin: 'https://box.example', href: 'https://box.example/', assign: (u) => log.assigned.push(u) },
  navigator: { serviceWorker: { addEventListener: (t, fn) => handlers.push(fn), startMessages: () => { log.started = true; } } },
  window: { addEventListener: (t, fn) => { (winHandlers[t] = winHandlers[t] || []).push(fn); } },
  document: { visibilityState: 'hidden', addEventListener: (t, fn) => { (docHandlers[t] = docHandlers[t] || []).push(fn); } },
};
if (scenario !== 'nocache') sandbox.caches = { open: async () => cache };
const CARD = 'https://box.example/sign/9#sign=a&v=1';
const tick = () => new Promise((r) => setTimeout(r, 30));
const send = (data) => handlers.forEach((h) => h({ data, ports: [{ postMessage: (m) => log.acks.push({ m, assignedSoFar: log.assigned.length }) }] }));

(async () => {
  if (scenario === 'load') put({ url: '/sign/9#sign=a&v=1', nonce: 'n', at: Date.now() });   // pending BEFORE the page script runs
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(swOpen, 'utf8'), sandbox);
  await tick();
  if (scenario === 'messages' || scenario === 'nocache') {
    send({ type: 'levain-open', url: '/sign/9#sign=a&v=1' });
    send({ type: 'levain-open', url: 'https://evil.example/x' });
    send({ type: 'levain-open', url: 'javascript:alert(1)' });
    send({ type: 'levain-open' });
    send({ type: 'other', url: '/x' });
    if (scenario === 'nocache') send({ type: 'levain-open', url: '/sign/9#sign=a&v=1', nonce: 'whatever' });
    await tick();
  } else if (scenario === 'claim') {
    put({ url: '/sign/9#sign=a&v=1', nonce: 'n1', at: Date.now() });
    send({ type: 'levain-open', url: '/sign/9#sign=a&v=1', nonce: 'other' });   // wrong nonce: no ack, no move, entry kept
    await tick();
    log.afterWrong = { assigned: log.assigned.length, acks: log.acks.length, pending: store.has(KEY) };
    send({ type: 'levain-open', url: '/sign/9#sign=a&v=1', nonce: 'n1' });      // the right one
    send({ type: 'levain-open', url: '/sign/9#sign=a&v=1', nonce: 'n1' });      // a second claimer of the same nonce
    await tick();
    log.afterRight = { pending: store.has(KEY) };
    send({ type: 'levain-open', url: '/sign/9#sign=a&v=1', nonce: 'n1' });      // the entry is gone
    await tick();
  } else if (scenario === 'load') {
    log.firstLoad = { assigned: log.assigned.slice(), pending: store.has(KEY) };      // fresh target: forwarded, consumed
    const fire = async (t, o) => { for (const fn of (o[t] || [])) fn({}); await tick(); };
    put({ url: '/sign/9#sign=a&v=1', nonce: 'n2', at: Date.now() - 31000 });            // expired
    await fire('pageshow', winHandlers);
    log.expired = { assigned: log.assigned.length, pending: store.has(KEY) };
    sandbox.location.href = CARD;
    put({ url: '/sign/9#sign=a&v=1', nonce: 'n3', at: Date.now() });                    // already here
    await fire('pageshow', winHandlers);
    log.alreadyHere = { assigned: log.assigned.length, pending: store.has(KEY) };
    sandbox.location.href = 'https://box.example/';
    put({ url: '/sign/9#sign=a&v=1', nonce: 'n4', at: Date.now() });                    // shown again after a resume
    sandbox.document.visibilityState = 'hidden';
    await fire('visibilitychange', docHandlers);
    log.hidden = { assigned: log.assigned.length, pending: store.has(KEY) };
    sandbox.document.visibilityState = 'visible';
    await fire('visibilitychange', docHandlers);
    log.visible = { assigned: log.assigned.length, pending: store.has(KEY) };
    put({ url: 'https://evil.example/x', nonce: 'n5', at: Date.now() });                // cross-origin: never followed
    await fire('pageshow', winHandlers);
    log.cross = { assigned: log.assigned.length, pending: store.has(KEY) };
  }
  console.log(JSON.stringify(log));
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
