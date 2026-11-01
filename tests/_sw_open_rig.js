// Runs levain/templates/web/sw_open.js in a vm with a stub page; prints what the page did.
// usage: node _sw_open_rig.js <path to sw_open.js> [messages|claim|load|first|nocache]
//   messages (default): the message path with no pending target (message-only), cross-origin and malformed messages
//   claim:   a message deletes only the key its nonce names; a lost or wrong claim does nothing, a second one too
//   load:    a page's FIRST load claims 'new' entries only; 'asked' and expired ones, resumes and bfcache restores claim nothing
//   first:   which of several 'new' entries a first-loading page takes (the one for its own url, else the newest)
//   nocache: no Cache Storage (an insecure context) -> message-only behaviour even with a nonce
const fs = require('fs');
const vm = require('vm');
const [swOpen, scenario = 'messages'] = process.argv.slice(2);
const handlers = [], winHandlers = {}, docHandlers = {};
const log = { assigned: [], acks: [], started: false };
const store = new Map();
const PFX = '/__levain_open/';
const keyOf = (r) => (typeof r === 'string' ? r : new URL(r.url).pathname);
const cache = {
  match: async (r) => (store.has(keyOf(r)) ? new Response(store.get(keyOf(r))) : undefined),
  delete: async (r) => store.delete(keyOf(r)),
  keys: async () => [...store.keys()].map((k) => ({ url: 'https://box.example' + k })),
};
const put = (nonce, e) => store.set(PFX + nonce, JSON.stringify({ nonce, ...e }));
const has = (nonce) => store.has(PFX + nonce);
const sandbox = {
  URL, Response, Date, Promise, JSON, setTimeout,
  location: { origin: 'https://box.example', href: 'https://box.example/', assign: (u) => log.assigned.push(u) },
  navigator: { serviceWorker: { addEventListener: (t, fn) => handlers.push(fn), startMessages: () => { log.started = true; } } },
  window: { addEventListener: (t, fn) => { (winHandlers[t] = winHandlers[t] || []).push(fn); } },
  document: { visibilityState: 'hidden', addEventListener: (t, fn) => { (docHandlers[t] = docHandlers[t] || []).push(fn); } },
};
if (scenario !== 'nocache') sandbox.caches = { open: async () => cache };
const CARD = 'https://box.example/sign/9#sign=a&v=1';
const U = '/sign/9#sign=a&v=1';
const tick = () => new Promise((r) => setTimeout(r, 30));
const send = (data) => handlers.forEach((h) => h({ data, ports: [{ postMessage: (m) => log.acks.push({ m, assignedSoFar: log.assigned.length }) }] }));
const fire = async (t, o, ev = {}) => { for (const fn of (o[t] || [])) fn(ev); await tick(); };

(async () => {
  if (scenario === 'load') put('n0', { url: U, at: Date.now(), for: 'new' });   // pending BEFORE the page script runs
  if (scenario === 'first') {
    put('old', { url: '/sign/1', at: Date.now() - 5000, for: 'new' });
    put('mid', { url: '/sign/2', at: Date.now() - 1000, for: 'new' });
    put('new', { url: '/sign/3', at: Date.now(), for: 'new' });
  }
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(swOpen, 'utf8'), sandbox);
  await tick();
  if (scenario === 'messages' || scenario === 'nocache') {
    send({ type: 'levain-open', url: U });
    send({ type: 'levain-open', url: 'https://evil.example/x' });
    send({ type: 'levain-open', url: 'javascript:alert(1)' });
    send({ type: 'levain-open' });
    send({ type: 'other', url: '/x' });
    if (scenario === 'nocache') send({ type: 'levain-open', url: U, nonce: 'whatever' });
    await tick();
  } else if (scenario === 'claim') {
    put('n1', { url: U, at: Date.now(), for: 'asked' });
    put('n2', { url: '/sign/10', at: Date.now(), for: 'asked' });
    send({ type: 'levain-open', url: U, nonce: 'other' });   // a nonce with no entry: no ack, no move, nothing deleted
    send({ type: 'levain-open', url: U, nonce: '../x/..' }); // not shaped like the worker's: ignored
    await tick();
    log.afterWrong = { assigned: log.assigned.length, acks: log.acks.length, n1: has('n1'), n2: has('n2') };
    send({ type: 'levain-open', url: U, nonce: 'n1' });      // the right one
    send({ type: 'levain-open', url: U, nonce: 'n1' });      // a second claimer of the same nonce
    await tick();
    log.afterRight = { n1: has('n1'), n2: has('n2') };       // n2 is another tap's entry: untouched
    send({ type: 'levain-open', url: U, nonce: 'n1' });      // the entry is gone
    await tick();
  } else if (scenario === 'load') {
    log.firstLoad = { assigned: log.assigned.slice(), pending: has('n0') };      // fresh 'new' target: forwarded, consumed
    put('a1', { url: U, at: Date.now(), for: 'asked' });                        // 'asked' belongs to the message path
    await fire('pageshow', winHandlers, { persisted: false });
    log.asked = { assigned: log.assigned.length, pending: has('a1') };
    put('x1', { url: U, at: Date.now() - 31000, for: 'new' });                  // expired
    await fire('pageshow', winHandlers, { persisted: false });
    log.expired = { assigned: log.assigned.length, pending: has('x1') };
    sandbox.location.href = CARD;
    put('h1', { url: U, at: Date.now(), for: 'new' });                          // already here
    await fire('pageshow', winHandlers, { persisted: false });
    log.alreadyHere = { assigned: log.assigned.length, pending: has('h1') };
    sandbox.location.href = 'https://box.example/';
    put('r1', { url: U, at: Date.now(), for: 'new' });                          // an already loaded page resumes: claims nothing
    sandbox.document.visibilityState = 'hidden';
    await fire('visibilitychange', docHandlers);
    sandbox.document.visibilityState = 'visible';
    await fire('visibilitychange', docHandlers);
    await fire('pageshow', winHandlers, { persisted: true });                   // a bfcache restore: claims nothing
    log.resumed = { assigned: log.assigned.length, pending: has('r1') };
    await fire('pageshow', winHandlers, { persisted: false });                  // a first load does
    log.shown = { assigned: log.assigned.length, pending: has('r1') };
    put('c1', { url: 'https://evil.example/x', at: Date.now(), for: 'new' });   // cross-origin: never followed
    await fire('pageshow', winHandlers, { persisted: false });
    log.cross = { assigned: log.assigned.length };
    store.clear();
    sandbox.location.href = 'https://box.example/';
    put('q1', { url: '/?view=needs', at: Date.now(), for: 'new' });             // the query is part of the target: '/' is not there
    await fire('pageshow', winHandlers, { persisted: false });
    log.query = { assigned: log.assigned.slice(-1), pending: has('q1') };
    sandbox.location.href = 'https://box.example/?view=needs';
    put('q2', { url: '/?view=needs#top', at: Date.now(), for: 'new' });         // only a fragment differs: already here
    const before = log.assigned.length;
    await fire('pageshow', winHandlers, { persisted: false });
    log.fragment = { moved: log.assigned.length - before, pending: has('q2') };
  } else if (scenario === 'first') {
    log.first = { assigned: log.assigned.slice(), left: [...store.keys()].map((k) => k.slice(PFX.length)).sort() };   // the newest went; two stay
    sandbox.location.href = 'https://box.example/sign/1';
    await fire('pageshow', winHandlers, { persisted: false });                 // a page already at /sign/1 takes its own entry and stays
    log.own = { assigned: log.assigned.length, left: [...store.keys()].map((k) => k.slice(PFX.length)).sort() };
  }
  console.log(JSON.stringify(log));
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
