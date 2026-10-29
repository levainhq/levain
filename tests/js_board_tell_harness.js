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
    groupedColsBody: gAll.filter((n) => n.className === "bd-cols" || n.className === "bd-col").length,
    boardCols: cAll.filter((n) => n.className === "bd-col").map((n) => n.dataset.category),
    boardColsBody: cAll.filter((n) => n.className === "bd-cols").length,
    mountedGroupHeads: view.filter((n) => n.className === "grp-head").length,
    mountedColsBody: view.filter((n) => n.className === "bd-cols").length,
    mountedButtons: view.filter((n) => n.tag === "button").length,
    modelBefore: before, modelAfter: h.model() && h.model().columns.map((x) => x.category + ":" + x.cards.length),
  };
}
(async () => {
  const on = { offered: true, why: null };
  const out = {};
  out.grouped = await grouped();
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
