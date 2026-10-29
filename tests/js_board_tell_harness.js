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
const doc = { createElement: node };
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
    if (url === "/cockpit/board.json") return resp(200, { columns: [], queue: "ok", tell: o.tell });
    if (url === "/cockpit/coord/drops.json") { seen.reads++; return resp(200, o.listing); }
    seen.posts.push(JSON.parse(init.body));
    return resp(o.postStatus || 200, { created: true, drop: { drop_id: "d1" } });
  };
  const host = node("div"); host.ownerDocument = doc;
    V.mount(host, { fetch, storage, pollMs: 0, headers: () => ({}), hash: o.hash });
  await tick();
  const ta = find(host, "textarea"), btn = find(host, "button");
  const say = (function f(n) { return n.className === "bd-say" ? n : n.children.map(f).find(Boolean); })(host);
  const pre = { value: ta.value, focused: ta.focused, sel: ta.sel, posts: seen.posts.length, reads: seen.reads };
  if (o.type != null) ta.value = o.type;
  for (let i = 0; i < (o.type == null ? 0 : (o.clicks || 1)); i++) btn.click();
  await tick(); await tick();
  return { posts: seen.posts, reads: seen.reads, say: say.textContent, wordsHidden: ta.hidden, store, pre, keysRead, value: ta.value };
}
(async () => {
  const on = { offered: true, why: null };
  const out = {};
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
  out.no_hash = await scenario(Object.assign({}, ok));
  console.log(JSON.stringify(out));
})();
