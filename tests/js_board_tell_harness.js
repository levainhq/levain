// Behavioural harness for board_view.js's tell box (run by tests/test_cockpit_board.py under node): a minimal fake DOM and
// a stub fetch; prints one JSON object of observations.
const fs = require("fs");
const vm = require("vm");
function node(tag) {
  return { tag, children: [], dataset: {}, listeners: {}, hidden: false, disabled: false, value: "", textContent: "",
    className: "", ownerDocument: null, setAttribute() {}, appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); }, replaceChildren() { this.children = []; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    click() { (this.listeners.click || []).forEach((f) => f()); },
    focused: 0, sel: null, focus() { this.focused++; }, setSelectionRange(a, b) { this.sel = [a, b]; } };
}
const doc = { createElement: node, activeElement: null };
// a stub address: replaceState rewrites location.hash the way the browser does; hashchange handlers are kept to be fired
const handlers = [];
globalThis.location = { pathname: "/", search: "", hash: "" };
globalThis.history = { replaceState(_s, _t, url) { const i = url.indexOf("#"); globalThis.location.hash = i < 0 ? "" : url.slice(i); } };
globalThis.addEventListener = (t, f) => { if (t === "hashchange") handlers.push(f); };
globalThis.removeEventListener = (t, f) => { const i = handlers.indexOf(f); if (i >= 0) handlers.splice(i, 1); };
const find = (n, tag) => { if (n.tag === tag) return n; for (const c of n.children) { const r = find(c, tag); if (r) return r; } return null; };
const tick = () => new Promise((r) => setTimeout(r, 5));
let k = 0;
globalThis.window = globalThis;
globalThis.crypto = { getRandomValues: (b) => { k++; for (let i = 0; i < b.length; i++) b[i] = (k * 31 + i) & 255; return b; } };
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
const V = globalThis.LevainBoardView;
const resp = (status, body) => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });

async function scenario(o) {
  const store = o.pending ? { "levain-board-drop-pending": JSON.stringify(o.pending) } : {};
  const keysRead = [];
  const storage = { getItem: (k) => { keysRead.push(k); return k in store ? store[k] : null; }, setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };
  const seen = { posts: [], reads: 0 };
  const fetch = (url, init) => {
    if (url === "/cockpit/board.json" && o.boardStatus) return resp(o.boardStatus, {});
    if (url === "/cockpit/board.json") return resp(200, { columns: [], queue: "ok", tell: o.tell });
    if (url === "/cockpit/coord/drops.json") { seen.reads++; return resp(200, o.listing); }
    seen.posts.push(JSON.parse(init.body));
    return resp(o.postStatus || 200, { created: true, drop: { drop_id: "d1" } });
  };
  const host = node("div"); host.ownerDocument = doc;
  globalThis.location.hash = o.hash || "";
  doc.activeElement = o.active || null;
  handlers.length = 0;
  const h = V.mount(host, { fetch, storage, pollMs: 0, headers: () => ({}), hash: o.hash });
  await tick();
  const hashAfterMount = globalThis.location.hash;
  const ta = find(host, "textarea"), btn = find(host, "button");
  const say = (function f(n) { return n.className === "bd-say" ? n : n.children.map(f).find(Boolean); })(host);
  if (o.later) {       // the operator follows another compose link in the open page (no reload)
    if (o.edit != null) ta.value = o.edit;
    globalThis.location.hash = o.later;
    handlers.slice().forEach((f) => f());
    await tick();
  }
  const pre = { value: ta.value, focused: ta.focused, sel: ta.sel, posts: seen.posts.length, reads: seen.reads };
  if (o.type != null) ta.value = o.type;
  for (let i = 0; i < (o.type == null ? 0 : (o.clicks || 1)); i++) btn.click();
  await tick(); await tick();
  return { posts: seen.posts, reads: seen.reads, say: say.textContent, wordsHidden: ta.hidden, store, pre, keysRead, value: ta.value, hashAfterMount, hash: globalThis.location.hash, handlers: handlers.length };
}
// The grouped list and the six-column body over ONE model: what each draws, and what the mounted tab holds.
const walk = (n, out = []) => { out.push(n); (n.children || []).forEach((c) => walk(c, out)); return out; };
const BODY = { queue: "ok", tell: { offered: true }, columns: [
  { category: "backlog", cards: [{ id: "levain:c", title: "Later C", repo: "levain", tier: "sonnet", held_reason: "depends on levain:a", journal: { who: "desk", iso: "t", to_category: "backlog", why: "queued it." } }] },
  { category: "running", cards: [{ id: "levain:a", title: "Land A", repo: "levain", tier: "sonnet", journal: { who: "seat-9", iso: "t", from_category: "ready", to_category: "running", why: "took it." } }] },
  { category: "needs-you", cards: [{ id: "q-1", title: "Ship the sweep fix", number: 3, band: true }] }] };
async function grouped() {
  const model = V.boardModel(BODY);
  const g = node("div"), c = node("div");
  V.renderGrouped(doc, g, model);
  V.renderBoard(doc, c, model);
  const gAll = walk(g), cAll = walk(c);
  const text = (n) => n.textContent + n.children.map(text).join("");
  const heads = gAll.filter((n) => n.className === "grp-head").map((n) => n.textContent);
  const rows = gAll.filter((n) => n.className === "bd-row");
  const host = node("div"); host.ownerDocument = doc;
  const h = V.mount(host, { fetch: (url) => resp(200, BODY), storage: { getItem: () => null, setItem() {}, removeItem() {} }, pollMs: 0, headers: () => ({}), hash: "" });
  const before = h.model();
  await tick();
  const view = walk(host);
  return {
    heads, rowIds: rows.map((n) => n.dataset.id), rowText: rows.map(text),
    glossGrouped: gAll.filter((n) => n.className === "bd-gloss").map(text), glossBoard: cAll.filter((n) => n.className === "bd-gloss").map(text),
    groupedColsBody: gAll.filter((n) => n.className === "bd-cols" || n.className === "bd-col").length,
    boardCols: cAll.filter((n) => n.className === "bd-col").map((n) => n.dataset.category),
    boardColsBody: cAll.filter((n) => n.className === "bd-cols").length,
    mountedGroupHeads: view.filter((n) => n.className === "grp-head").length,
    mountedColsBody: view.filter((n) => n.className === "bd-cols").length,
    mountedButtons: view.filter((n) => n.tag === "button").length,
    modelBefore: before, modelAfter: h.model() && h.model().columns.map((x) => x.category + ":" + x.cards.length),
  };
}
// The shared bus (L3 r1, session A): the watchers and the last model belong to the page, not to one mounted view.
async function bus() {
  let mode = "ok";
  const fetch = () => {
    if (mode === "net") return Promise.reject(new Error("offline"));
    if (mode === "ok") return resp(200, BODY);
    return resp(mode, {});
  };
  const shared = { model: null, failed: false, watchers: [] };
  const opts = { fetch, storage: { getItem: () => null, setItem() {}, removeItem() {} }, pollMs: 0, headers: () => ({}), hash: "", bus: shared };
  const mk = () => { const host = node("div"); host.ownerDocument = doc; return { host, h: V.mount(host, opts) }; };
  const shape = (m, failed) => (failed ? "FAILED" : m ? m.columns.map((x) => x.category + ":" + x.cards.length).join(",") : "none");
  const calls = [];
  const one = mk();
  one.h.watch((m, failed) => calls.push(shape(m, failed)));
  const thrower = one.h.watch(() => { throw new Error("a watcher that throws"); });
  await tick();
  const afterFirst = calls.slice();
  one.h.stop();                                             // a view is stopped (hide / token toggle) ...
  const watchersAfterStop = shared.watchers.length;
  const two = mk();                                         // ... and a new one is mounted: the same watchers hear it
  await tick();
  const afterRemount = calls.slice();
  const steps = {};
  for (const m of [403, 500, "net", "ok"]) {
    mode = m; await two.h.pull(); await tick();
    steps[m] = { last: calls[calls.length - 1], model: two.h.model() && "set", failed: shared.failed, panel: two.host.children[0].textContent.slice(0, 40), heads: walk(two.host).filter((n) => n.className === "grp-head").length };
  }
  one.h.stop(); two.h.stop();
  mode = 403; const before = calls.length; const seq0 = shared.seq; await two.h.pull();      // a stopped view's failed read tells nobody
  return { afterFirst, watchersAfterStop, afterRemount, steps, stoppedPullCalls: calls.length - before, stoppedPullSeqMoved: shared.seq !== seq0, thrower: typeof thrower };
}
// L3 r1 (codex MED): a failed read retires the Send box, keeps the typed words, and the next good read re-offers it.
async function tellFollowsTheRead() {
  let mode = "ok";
  const fetch = () => (mode === "net" ? Promise.reject(new Error("offline")) : mode === "ok" ? resp(200, BODY) : resp(mode, {}));
  const host = node("div"); host.ownerDocument = doc;
  const h = V.mount(host, { fetch, storage: { getItem: () => null, setItem() {}, removeItem() {} }, pollMs: 0, headers: () => ({}), hash: "" });
  await tick();
  const ta = find(host, "textarea"), btn = find(host, "button");
  const gate = walk(host).find((n) => n.className === "bd-note");
  const snap = () => ({ hidden: ta.hidden, sendHidden: btn.hidden, gate: gate.textContent, gateHidden: gate.hidden, typed: ta.value });
  const out = { good: snap() };
  ta.value = "re levain:a: please hold";                    // words the operator already typed
  for (const m of [403, 500, "net"]) {
    mode = m; await h.pull(); await tick();
    out[m] = snap();
  }
  mode = "ok"; await h.pull(); await tick();
  out.again = snap();
  h.stop();
  return out;
}
// L3 r1 (complement LOW #1): a late response (good or failed) never overwrites a newer one.
async function lateResponses() {
  const BODY2 = { queue: "ok", tell: { offered: true }, columns: [{ category: "ready", cards: [{ id: "n", title: "N" }] }] };
  const shape = (m) => (m ? m.columns.map((x) => x.category + ":" + x.cards.length).join(",") : "none");
  const run = async (older, newer) => {
    const pending = [];
    const fetch = () => new Promise((res) => pending.push(res));
    const shared = { model: null, failed: false, watchers: [] };
    const calls = [];
    const host = node("div"); host.ownerDocument = doc;
    const h = V.mount(host, { fetch, storage: { getItem: () => null, setItem() {}, removeItem() {} }, pollMs: 0, headers: () => ({}), hash: "", bus: shared });
    h.watch((m, failed) => calls.push(failed ? "FAILED" : shape(m)));
    const ta = find(host, "textarea");
    h.pull();                                                  // the second request: the mount's own is pending[0] (the older)
    const resolveWith = async (i, x) => { pending[i](x === 403 || x === 500 ? { ok: false, status: x, json: () => Promise.resolve({}) } : { ok: true, status: 200, json: () => Promise.resolve(x) }); await tick(); };
    await resolveWith(1, newer);                              // the newer answers first ...
    const mid = { model: shape(shared.model), failed: shared.failed, calls: calls.slice(), hidden: ta.hidden };
    await resolveWith(0, older);                              // ... then the older one arrives late
    const end = { model: shape(shared.model), failed: shared.failed, calls: calls.slice(), hidden: ta.hidden, view: host.children[0].textContent };
    h.stop();
    return { mid, end };
  };
  return { lateGood: await run(BODY, BODY2), lateFail403: await run(403, BODY2), lateFail500: await run(500, BODY2), lateGoodAfterFail: await run(BODY, 500) };
}
// L3 r1 (complement LOW #2): the token's removal fails the bus (the open modal says so); its return clears that and tells the watchers.
async function tokenTransitions() {
  if (typeof V.busToken !== "function") return { missing: true };       // the view exports the token step sync() runs
  const shared = { model: null, failed: false, watchers: [] };
  const calls = [];
  const watch = (m, failed) => calls.push(failed ? "FAILED" : m ? "model" : "null");
  shared.watchers.push(watch);
  const fetchOk = () => resp(200, BODY);
  const host = node("div"); host.ownerDocument = doc;
  const h = V.mount(host, { fetch: fetchOk, storage: { getItem: () => null, setItem() {}, removeItem() {} }, pollMs: 0, headers: () => ({}), hash: "", bus: shared });
  await tick(); h.stop();
  const out = { start: { failed: shared.failed, calls: calls.slice() } };
  V.busToken(shared, false);
  out.removed = { failed: shared.failed, model: shared.model, calls: calls.slice() };
  V.busToken(shared, false);                                   // sync() fires often: a second call says nothing new
  out.removedAgain = { calls: calls.slice() };
  V.busToken(shared, true);
  out.returned = { failed: shared.failed, model: shared.model, calls: calls.slice() };
  // a bus failed by a bad READ (token present all along) is not cleared by a sync that sees the token
  shared.failed = true; calls.length = 0;
  V.busToken(shared, true);
  out.readFailureKept = { failed: shared.failed, calls: calls.slice() };
  // L3 r2: a bus ALREADY failed by a bad read is not failed by the token, so the token going and coming back leaves it failed
  shared.failed = true; shared.lacked = false; calls.length = 0;
  V.busToken(shared, false); V.busToken(shared, true);
  out.readFailureSurvivesTokenCycle = { failed: shared.failed, calls: calls.slice() };
  return out;
}
// The filter box's match and count, and the journal line's absence: over BODY (3 cards: backlog, running, needs-you).
function filtering() {
  const m = V.boardModel(BODY);
  const pick = (q) => { const f = V.filterModel(m, q); return { label: V.countLabel(f), ids: f.model.columns.flatMap((c) => c.cards.map((k) => k.id)), cols: f.model.columns.length, same: f.model === m }; };
  return { none: pick(""), blank: pick("   "), folded: pick("LATER  c"), journalWords: pick("took it"), and: pick("land levain"), miss: pick("zzz"), band: pick("sweep"),
    noJournal: V.journalLine(null), journal: V.journalLine({ who: "desk", iso: "t", to_category: "ready", why: "queued it." }) };
}
(async () => {
  const on = { offered: true, why: null };
  const out = {};
  out.filtering = filtering();
  out.grouped = await grouped();
  out.bus = await bus();
  out.tellFollows = await tellFollowsTheRead();
  out.late = await lateResponses();
  out.token = await tokenTransitions();
  out.not_offered = await scenario({ tell: { offered: false, why: "different queue" }, listing: { coord_store_read: true, coord_store_id: "S" }, type: "hi" });
  out.unreadable = await scenario({ tell: on, listing: { coord_store_read: false, coord_store_id: null, coord_note: "store locked" }, type: "hi" });
  out.double_click = await scenario({ tell: on, listing: { coord_store_read: true, coord_store_id: "S" }, type: "hi", clicks: 2 });
  out.no_store_yet = await scenario({ tell: on, listing: { coord_store_read: true, coord_store_id: null }, type: "hi" });
  out.edited_pending = await scenario({ tell: on, listing: { coord_store_read: true, coord_store_id: "S" }, type: "B",
    pending: { words: "A", outbox_id: "ph-old", sent_at: "x", store_id: "S" } });
  out.same_pending = await scenario({ tell: on, listing: { coord_store_read: true, coord_store_id: "S" }, type: "A",
    pending: { words: "A", outbox_id: "ph-old", sent_at: "x", store_id: "S" } });
  const ok = { tell: on, listing: { coord_store_read: true, coord_store_id: "S" } };
  out.prefill = await scenario(Object.assign({ hash: "#board&compose=re%20c-12%3A%20" }, ok));
  out.prefill_edit = await scenario(Object.assign({ hash: "#board&compose=re%20c-12%3A%20", type: "re c-12: do it" }, ok));
  out.prefill_plus = await scenario(Object.assign({ hash: "#board&compose=re+c-12%3A+" }, ok));
  out.prefill_token = await scenario(Object.assign({ hash: "#write_token=SECRET123&compose=re%20x%3A%20" }, ok));
  out.hostile_markup = await scenario(Object.assign({ hash: "#compose=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E" }, ok));
  out.hostile_long = await scenario(Object.assign({ hash: "#compose=" + "a".repeat(5000) }, ok));
  out.hostile_malformed = await scenario(Object.assign({ hash: "#compose=%E0%A4%A%zz%" }, ok));
  out.hostile_control = await scenario(Object.assign({ hash: "#compose=a%00b%0Ac" }, ok));
  out.kept_wins = await scenario({ tell: on, listing: ok.listing, hash: "#compose=new", pending: { words: "A", outbox_id: "ph-old", sent_at: "x", store_id: "S" } });
  out.hash_other_params = await scenario(Object.assign({ hash: "#board&write_token=a%2Bb+c&compose=re%20x%3A%20&z" }, ok));
  out.hash_only_compose = await scenario(Object.assign({ hash: "#compose=re%20x%3A%20" }, ok));
  out.hash_bare_board = await scenario(Object.assign({ hash: "#board&compose=re%20x%3A%20" }, ok));
  out.board_fails = await scenario({ tell: on, listing: ok.listing, boardStatus: 403, hash: "#board&compose=re%20x%3A%20" });
  out.not_offered_hash = await scenario({ tell: { offered: false, why: "different queue" }, listing: ok.listing, hash: "#board&compose=re%20x%3A%20" });
  out.kept_wins_hash = await scenario({ tell: on, listing: ok.listing, hash: "#board&compose=new", pending: { words: "A", outbox_id: "ph-old", sent_at: "x", store_id: "S" } });
  out.focus_elsewhere = await scenario(Object.assign({ hash: "#compose=re%20x%3A%20", active: { tag: "input" } }, ok));
  out.focus_body = await scenario(Object.assign({ hash: "#compose=re%20x%3A%20", active: doc.body = { tag: "body" } }, ok));
  out.surrogate = await scenario(Object.assign({ hash: "#compose=" + "a".repeat(299) + encodeURIComponent("\u{1F600}") }, ok));
  out.hashchange_fill = await scenario(Object.assign({ hash: "#board", later: "#board&compose=re%20y%3A%20" }, ok));
  out.hashchange_edited = await scenario(Object.assign({ hash: "#board", later: "#board&compose=re%20y%3A%20", edit: "mine" }, ok));
  out.hashchange_kept = await scenario({ tell: on, listing: ok.listing, hash: "#board", later: "#board&compose=re%20y%3A%20", pending: { words: "A", outbox_id: "ph-old", sent_at: "x", store_id: "S" } });
  out.no_hash = await scenario(Object.assign({}, ok));
  console.log(JSON.stringify(out));
})();
