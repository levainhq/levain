// Runs levain/templates/web/sw_open.js in a vm with a stub page; prints what the page did.
// usage: node _sw_open_rig.js <path to sw_open.js> [messages|claim|load|first|overlap|nocache]
//   messages (default): the message path with no pending target (message-only), cross-origin and malformed messages
//   claim:   a message deletes only the key its nonce names; a lost or wrong claim does nothing, a second one too
//   load:    a document's first script run claims a fresh 'new' entry only at '/' or at the entry's own target; 'asked' and
//            expired ones, an unrelated page, and any resume or pageshow claim nothing
//   first:   which of several 'new' entries a first-loading page takes (the one for its own url, else the newest)
//   overlap: two taps' entries; the page opened for the first takes only that one, and its initial pageshow takes nothing more
//   nocache: no Cache Storage (an insecure context) -> message-only behaviour even with a nonce
// Each `boot(href)` is a NEW document: a fresh vm context running the page script once, over the one shared cache store.
const fs = require('fs');
const vm = require('vm');
const [swOpen, scenario = 'messages'] = process.argv.slice(2);
let handlers = [], winHandlers = {}, docHandlers = {};
const log = { assigned: [], acks: [], started: false };
const store = new Map();
const PFX = '/__levain_open/';
const keyOf = (r) => (typeof r === 'string' ? r : new URL(r.url).pathname);
const traceStore = new Map();
const traceCache = { put: async (k, r) => { traceStore.set(k, await r.text()); }, delete: async (k) => traceStore.delete(k),
  keys: async () => [...traceStore.keys()].map((k) => ({ url: 'https://box.example' + k })), match: async () => undefined };
const traceSteps = () => [...traceStore.entries()].sort(([a], [b]) => (a < b ? -1 : 1)).map(([, v]) => JSON.parse(v));
const cache = {
  match: async (r) => (store.has(keyOf(r)) ? new Response(store.get(keyOf(r))) : undefined),
  delete: async (r) => store.delete(keyOf(r)),
  keys: async () => [...store.keys()].map((k) => ({ url: 'https://box.example' + k })),
};
const put = (nonce, e) => store.set(PFX + nonce, JSON.stringify({ nonce, ...e }));
const has = (nonce) => store.has(PFX + nonce);
const left = () => [...store.keys()].map((k) => k.slice(PFX.length)).sort();
let sandbox;
const tick = () => new Promise((r) => setTimeout(r, 30));
async function boot(href = 'https://box.example/') {
  handlers = []; winHandlers = {}; docHandlers = {};
  sandbox = {
    URL, Response, Date, Promise, JSON, setTimeout,
    location: { origin: 'https://box.example', href, assign: (u) => log.assigned.push(u) },
    navigator: { standalone: true, serviceWorker: { controller: {}, addEventListener: (t, fn) => handlers.push(fn), startMessages: () => { log.started = true; } } },
    window: { addEventListener: (t, fn) => { (winHandlers[t] = winHandlers[t] || []).push(fn); } },
    document: { visibilityState: 'hidden', addEventListener: (t, fn) => { (docHandlers[t] = docHandlers[t] || []).push(fn); } },
  };
  if (scenario !== 'nocache') sandbox.caches = { open: async (n) => (n === 'levain-trace' ? traceCache : cache) };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(swOpen, 'utf8'), sandbox);
  await tick();
}
const CARD = 'https://box.example/sign/9#sign=a&v=1';
const U = '/sign/9#sign=a&v=1';
const send = (data) => handlers.forEach((h) => h({ data, ports: [{ postMessage: (m) => log.acks.push({ m, assignedSoFar: log.assigned.length }) }] }));
const fire = async (t, o, ev = {}) => { for (const fn of (o[t] || [])) fn(ev); await tick(); };

(async () => {
  if (scenario === 'load') put('n0', { url: U, at: Date.now(), for: 'new' });   // pending BEFORE the page script runs
  if (scenario === 'first') {
    put('old', { url: '/sign/1', at: Date.now() - 5000, for: 'new' });
    put('mid', { url: '/sign/2', at: Date.now() - 1000, for: 'new' });
    put('new', { url: '/sign/3', at: Date.now(), for: 'new' });
  }
  if (scenario === 'overlap') {
    put('tapA', { url: '/sign/1#sign=a', at: Date.now() - 100, for: 'new' });
    put('tapB', { url: '/sign/2#sign=b', at: Date.now(), for: 'new' });
  }
  const here0 = scenario === 'first' ? 'https://box.example/' : scenario === 'overlap' ? 'https://box.example/sign/1#sign=a' : 'https://box.example/';
  await boot(here0);
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
    log.firstLoad = { assigned: log.assigned.slice(), pending: has('n0') };      // fresh 'new' target at '/': forwarded, consumed
    log.noPageshowHandler = !('pageshow' in winHandlers);                         // the scan is not repeated on pageshow
    // 'asked' belongs to the message path, and lives 30 s: 15 s old is alive and untouched; 'new' lives 30 s too
    put('a1', { url: U, at: Date.now() - 15000, for: 'asked' });
    put('x1', { url: U, at: Date.now() - 31000, for: 'new' });                   // expired 'new'
    await boot('https://box.example/');
    log.asked = { assigned: log.assigned.length, askedKept: has('a1'), expiredNewGone: !has('x1') };
    store.clear();
    put('x2', { url: U, at: Date.now() - 31000, for: 'asked' });                 // expired 'asked'
    await boot('https://box.example/');
    log.askedExpired = { assigned: log.assigned.length, gone: !has('x2') };
    store.clear();
    put('h1', { url: U, at: Date.now(), for: 'new' });                          // already here
    await boot(CARD);
    log.alreadyHere = { assigned: log.assigned.length, pending: has('h1') };
    store.clear();
    put('u1', { url: U, at: Date.now(), for: 'new' });                          // an unrelated first load (not '/', not the target)
    await boot('https://box.example/sign/3');
    log.unrelated = { assigned: log.assigned.length, pending: has('u1') };
    await boot('https://box.example/pair');
    log.unrelatedPair = { assigned: log.assigned.length, pending: has('u1') };
    // an already loaded page at '/' that resumes (visibilitychange, bfcache pageshow, any pageshow) claims nothing
    await boot('https://box.example/sign/3');
    put('r1', { url: U, at: Date.now(), for: 'new' });
    sandbox.document.visibilityState = 'hidden';
    await fire('visibilitychange', docHandlers);
    sandbox.document.visibilityState = 'visible';
    await fire('visibilitychange', docHandlers);
    await fire('pageshow', winHandlers, { persisted: true });
    await fire('pageshow', winHandlers, { persisted: false });
    log.resumed = { assigned: log.assigned.length, pending: has('u1') && has('r1') };
    store.clear();
    put('s1', { url: U, at: Date.now() - 29000, for: 'new' });                  // 29 s old: still inside the 30 s window
    await boot('https://box.example/');
    log.nineSeconds = { assigned: log.assigned.length, pending: has('s1') };
    store.clear();
    put('c1', { url: 'https://evil.example/x', at: Date.now(), for: 'new' });   // cross-origin: never followed
    await boot('https://box.example/');
    log.cross = { assigned: log.assigned.length };
    store.clear();
    put('q1', { url: '/?view=needs', at: Date.now(), for: 'new' });             // the query is part of the target: '/' is not there
    await boot('https://box.example/');
    log.query = { assigned: log.assigned.slice(-1), pending: has('q1') };
    store.clear();
    put('q2', { url: '/?view=needs#top', at: Date.now(), for: 'new' });         // only a fragment differs: already here
    const before = log.assigned.length;
    await boot('https://box.example/?view=needs');
    log.fragment = { moved: log.assigned.length - before, pending: has('q2') };
  } else if (scenario === 'first') {
    log.first = { assigned: log.assigned.slice(), left: left() };                // the newest went; two stay
    await boot('https://box.example/sign/1');                                    // a page already at /sign/1 takes its own entry and stays
    log.own = { assigned: log.assigned.length, left: left() };
  } else if (scenario === 'overlap') {
    // the page opened for tap A (at A's target) took A's entry and stayed; B's entry is still there for B's window
    log.afterLoad = { assigned: log.assigned.slice(), left: left() };
    await fire('pageshow', winHandlers, { persisted: false });                   // its initial pageshow: takes nothing
    await fire('pageshow', winHandlers, { persisted: true });
    log.afterPageshow = { assigned: log.assigned.slice(), left: left() };
    await boot('https://box.example/sign/2#sign=b');                             // B's window: takes B's entry, stays
    log.afterB = { assigned: log.assigned.slice(), left: left() };
  }
  log.trace = traceSteps();
  console.log(JSON.stringify(log));
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(1); });
